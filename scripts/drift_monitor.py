"""drift_monitor.py - TASKS 2-4 (and the Task 6 verification re-run).

Scores the drifted batch against the LIVE scoring service (the pods Argo CD deployed), so it
measures what is really running, not what the YAML says.

Task 2  Data drift: PSI per feature vs the histograms in reference_stats.json.
Task 3  Performance drift: precision/recall of the live service on drifted_batch.csv vs baseline.
Task 4  Decide + alert: if performance is out of tolerance, call alerting.send_alert().

Needs the service reachable, e.g.:
  kubectl -n fraud port-forward svc/fraud-service 9000:80
Run:  python scripts/drift_monitor.py [--url http://localhost:9000] [--no-alert]
Exit codes: 0 healthy | 2 alert raised | 3 rollout in progress (mixed models, no verdict) | 4 service unreachable
"""
import argparse
import hashlib
import json
import subprocess
import sys
import urllib.error
import urllib.request

import joblib
import pandas as pd

import alerting
from common import (ALERTS_DIR, DATA_DIR, FEATURES, LABEL, MODELS_DIR, REF_STATS_PATH,
                    bin_counts, evaluate, get_active_model, metrics_from_preds, psi,
                    read_json, utc_now, write_json)

PSI_WARN, PSI_MAJOR = 0.10, 0.25
MAX_PRECISION_DROP = 0.20
MAX_RECALL_DROP = 0.10


def level(v: float) -> str:
    return "major" if v >= PSI_MAJOR else "moderate" if v >= PSI_WARN else "stable"


def feature_drift(ref_stats: dict, batch: pd.DataFrame) -> dict:
    out = {}
    for f in FEATURES:
        spec = ref_stats["features"][f]
        counts = bin_counts(batch[f], spec["bin_edges"])
        v = psi(spec["ref_proportions"], counts / counts.sum())
        out[f] = {"psi": round(v, 4), "level": level(v)}
    return out


def _post(url: str, payload: dict) -> dict:
    body = json.dumps(payload, default=lambda o: o.item() if hasattr(o, "item") else str(o)).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def score_via_service(base_url: str, df: pd.DataFrame, chunk: int = 250):
    """Send the batch to /score_batch in chunks. Returns (predictions, models_seen, pods_seen)."""
    preds, models, pods = [], set(), set()
    for i in range(0, len(df), chunk):
        resp = _post(base_url.rstrip("/") + "/score_batch",
                     {"records": df[FEATURES].iloc[i:i + chunk].to_dict("records")})
        preds.extend(resp["predictions"])
        models.add(resp["model"])
        pods.add(resp["pod"])
    return preds, sorted(models), sorted(pods)


HISTORY_PATH = ALERTS_DIR / "monitor_history.jsonl"


def _git_head():
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, timeout=5).stdout.strip() or None
    except Exception:
        return None


def record_run(entry: dict) -> None:
    """Append one line per run to alerts/monitor_history.jsonl.
    Each line carries the hash of the previous line, so editing an earlier line breaks the chain."""
    ALERTS_DIR.mkdir(exist_ok=True)
    prev = "GENESIS"
    if HISTORY_PATH.exists():
        lines = [l for l in HISTORY_PATH.read_text().splitlines() if l.strip()]
        if lines:
            prev = json.loads(lines[-1]).get("hash", "BROKEN")
    entry = {"timestamp": utc_now(), "git_head": _git_head(), **entry, "prev_hash": prev}
    entry["hash"] = hashlib.sha256((prev + json.dumps(entry, sort_keys=True)).encode()).hexdigest()
    with HISTORY_PATH.open("a") as f:
        f.write(json.dumps(entry, sort_keys=True) + "\n")


def finish(code: int, args, **fields) -> None:
    """Record this run in the history, then exit with the documented exit code."""
    try:
        record_run({"no_alert": bool(args.no_alert), "url": args.url, "exit_code": code, **fields})
    except Exception as e:          # history problems must never change the monitor's verdict
        print(f"(warning: could not write {HISTORY_PATH.name}: {e})")
    sys.exit(code)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:9000", help="scoring service base URL")
    ap.add_argument("--no-alert", action="store_true")
    args = ap.parse_args()

    ref_stats = read_json(REF_STATS_PATH)
    batch = pd.read_csv(DATA_DIR / "drifted_batch.csv")
    desired = get_active_model()  # Git working copy: deploy/config/model.yaml

    # Task 2
    fpsi = feature_drift(ref_stats, batch)
    max_psi = max(v["psi"] for v in fpsi.values())
    drifted = [f for f, v in fpsi.items() if v["level"] == "major"]

    # Task 3 (live service)
    try:
        preds, models_seen, pods_seen = score_via_service(args.url, batch)
    except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
        print(f"Cannot reach the scoring service at {args.url}: {e}\n"
              f"Start a port-forward:  kubectl -n fraud port-forward svc/fraud-service 9000:80")
        finish(4, args, status="UNREACHABLE", desired_model=desired)

    if len(models_seen) > 1:
        print(f"ROLLOUT IN PROGRESS: pods are serving different models {models_seen}. "
              f"Wait for 'kubectl -n fraud rollout status deploy/fraud-service' and re-run.")
        finish(3, args, status="ROLLOUT_IN_PROGRESS", desired_model=desired, models_seen=models_seen)
    serving = models_seen[0]

    base = ref_stats["baseline"]
    cur = metrics_from_preds(batch[LABEL], preds)
    p_drop = round(base["precision"] - cur["precision"], 4)
    r_drop = round(base["recall"] - cur["recall"], 4)

    candidates = {p.name: evaluate(joblib.load(p), batch) for p in sorted(MODELS_DIR.glob("*.pkl"))}
    recommended = max(candidates, key=lambda m: candidates[m]["f1"])

    # Task 4
    reasons = []
    if p_drop > MAX_PRECISION_DROP:
        reasons.append(f"precision fell {base['precision']:.3f} -> {cur['precision']:.3f} (drop {p_drop:.3f} > {MAX_PRECISION_DROP})")
    if r_drop > MAX_RECALL_DROP:
        reasons.append(f"recall fell {base['recall']:.3f} -> {cur['recall']:.3f} (drop {r_drop:.3f} > {MAX_RECALL_DROP})")
    status = "ALERT" if reasons else ("OK_WITH_DATA_DRIFT" if drifted else "OK")

    report = {
        "timestamp": utc_now(), "status": status,
        "desired_model": desired, "serving_model": serving, "service_in_sync": desired == serving,
        "service_url": args.url, "pods_scored": pods_seen,
        "batch": {"file": "drifted_batch.csv", "rows": len(batch)},
        "feature_psi": fpsi, "max_psi": max_psi, "drifted_features": drifted,
        "baseline": {"model": base["model"], "precision": base["precision"], "recall": base["recall"]},
        "current": cur, "deltas": {"precision_drop": p_drop, "recall_drop": r_drop},
        "thresholds": {"psi_warn": PSI_WARN, "psi_major": PSI_MAJOR,
                       "max_precision_drop": MAX_PRECISION_DROP, "max_recall_drop": MAX_RECALL_DROP},
        "reasons": reasons, "candidates": candidates, "recommended_model": recommended,
    }
    ALERTS_DIR.mkdir(exist_ok=True)
    write_json(ALERTS_DIR / "last_check.json", report)

    print(f"Git says (deploy/config/model.yaml): {desired} | cluster serving: {serving}"
          + ("" if report["service_in_sync"] else "  <-- Argo CD has not synced this yet (push your commit? refresh the app?)"))
    print("Feature PSI:")
    for f, v in sorted(fpsi.items(), key=lambda kv: kv[1]["psi"], reverse=True):
        print(f"  {f:24s} {v['psi']:.3f}  {v['level']}")
    print(f"Precision {base['precision']:.3f} -> {cur['precision']:.3f} | "
          f"Recall {base['recall']:.3f} -> {cur['recall']:.3f}")
    print(f"STATUS: {status}")

    summary = dict(status=status, desired_model=desired, serving_model=serving, in_sync=desired == serving,
                   precision=cur["precision"], recall=cur["recall"], max_psi=max_psi, pods=pods_seen,
                   recommended_model=recommended)
    if status == "ALERT" and not args.no_alert:
        alert = alerting.send_alert(report)
        print(f"Alert written to alerts/. Recommended model: {recommended}")
        finish(2, args, alert_id=alert.get("alert_id"), **summary)
    finish(0, args, **summary)


if __name__ == "__main__":
    main()
