"""Automated grader.  Run after Tasks 1-6:

  python scripts/grade.py            (needs kubectl access to the kind cluster)

Verifies the promotion really happened THROUGH GIT and Argo CD, not by hand.
"""
import json
import subprocess
import sys

import pandas as pd

from common import (ALERTS_DIR, DATA_DIR, MODELS_DIR, REF_STATS_PATH, ROOT, get_active_model,
                    read_json)

results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail and not ok else ""))


def safe(fn, default=None):
    try:
        return fn()
    except Exception:  # noqa: BLE001
        return default


def run(*cmd):
    return subprocess.run(list(cmd), capture_output=True, text=True, check=True).stdout.strip()


def kubectl(*args):
    return run("kubectl", *args)


# ---- Task 1
check("T1 reference_batch.csv has 5000 rows", safe(lambda: len(pd.read_csv(DATA_DIR / "reference_batch.csv"))) == 5000)
check("T1 drifted_batch.csv has 1500 rows", safe(lambda: len(pd.read_csv(DATA_DIR / "drifted_batch.csv"))) == 1500)
check("T1 three model files exist", all((MODELS_DIR / f"fraud_model_v{i}.pkl").exists() for i in (1, 2, 3)))
stats = safe(lambda: read_json(REF_STATS_PATH), {})
check("T1 reference_stats.json has histograms + baseline",
      bool(stats.get("features")) and "precision" in stats.get("baseline", {}))

# ---- Tasks 2-4 (evidence from the alert raised while v2 was live)
alert = safe(lambda: read_json(ALERTS_DIR / "alert.json"), {})
check("T4 alert.json written while v2 was serving", alert.get("serving_model") == "fraud_model_v2.pkl")
check("T4 alert recommends fraud_model_v3", alert.get("recommended_model") == "fraud_model_v3.pkl")
wl = safe(lambda: (ALERTS_DIR / "webhook_log.jsonl").read_text().strip().splitlines(), [])
check("T4 webhook_log.jsonl has valid JSON lines", len(wl) >= 1 and all(safe(lambda l=l: json.loads(l)) for l in wl))
issue = safe(lambda: (ALERTS_DIR / "GENERATED_ISSUE.md").read_text(), "")
check("T4 GENERATED_ISSUE.md points at deploy/config/model.yaml", "deploy/config/model.yaml" in issue and "fraud_model_v3.pkl" in issue)

# ---- Task 5: promotion went through Git + Argo CD
check("T5 deploy/config/model.yaml says v3", safe(get_active_model) == "fraud_model_v3.pkl")
commits = safe(lambda: run("git", "-C", str(ROOT), "log", "--oneline", "--", "deploy/config/model.yaml").splitlines())
check("T5 model.yaml has >= 2 Git commits (initial + promotion)", commits is not None and len(commits) >= 2,
      "not a git repo" if commits is None else f"{len(commits)} commit(s)")

if safe(lambda: run("kubectl", "version", "--client")) is None:
    check("T5 kubectl is available", False, "install kubectl and point it at the kind cluster")
else:
    app = safe(lambda: json.loads(kubectl("-n", "argocd", "get", "application", "fraud-service", "-o", "json")), {})
    st = app.get("status", {})
    check("T5 Argo CD application exists", bool(app), "apply argocd/application.yaml")
    check("T5 Argo CD: Synced and Healthy",
          st.get("sync", {}).get("status") == "Synced" and st.get("health", {}).get("status") == "Healthy",
          f"sync={st.get('sync', {}).get('status')} health={st.get('health', {}).get('status')}")
    head = safe(lambda: run("git", "-C", str(ROOT), "rev-parse", "HEAD"))
    check("T5 Argo CD synced your Git HEAD (deployed from Git, not by hand)",
          head is not None and st.get("sync", {}).get("revision") == head,
          "push your latest commit and refresh the app")
    check("T5 Argo CD has deployed >= 2 revisions", len(st.get("history", [])) >= 2)
    dep = safe(lambda: json.loads(kubectl("-n", "fraud", "get", "deploy", "fraud-service", "-o", "json")), {})
    s, want = dep.get("status", {}), dep.get("spec", {}).get("replicas")
    check("T5 rollout complete (all replicas updated and ready)",
          want and s.get("updatedReplicas") == want and s.get("readyReplicas") == want and s.get("replicas") == want)

# ---- Task 6: monitor re-run against the live service after the rollout
last = safe(lambda: read_json(ALERTS_DIR / "last_check.json"), {})
check("T6 monitor re-run scored the live service serving v3", last.get("serving_model") == "fraud_model_v3.pkl")
check("T6 monitor status is healthy", last.get("status") in ("OK", "OK_WITH_DATA_DRIFT"), last.get("status"))
check("T6 Git and cluster agreed at re-run", last.get("service_in_sync") is True)

passed = sum(results)
print(f"\n{passed}/{len(results)} checks passed")
sys.exit(0 if passed == len(results) else 1)
