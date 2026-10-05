"""Alerting (TASK 4 output side).

Given a drift report, writes:
  alerts/alert.json          machine-readable alert
  alerts/webhook_log.jsonl   simulated webhook deliveries (one JSON object per line, no network)
  alerts/GENERATED_ISSUE.md  ready-to-file issue with a recommended GitOps action
"""
import json

from common import ALERTS_DIR, utc_now, write_json


def build_alert(report: dict) -> dict:
    severity = "critical" if report["current"]["precision"] < report["baseline"]["precision"] - 0.30 \
        or report["max_psi"] >= 0.25 and report["deltas"]["recall_drop"] > 0.10 else "warning"
    return {
        "alert_id": "drift-" + report["timestamp"].replace(":", "").replace("-", "")[:15],
        "timestamp": report["timestamp"],
        "severity": severity,
        "event": "model_performance_degradation",
        "serving_model": report["serving_model"],
        "reasons": report["reasons"],
        "max_psi": report["max_psi"],
        "drifted_features": report["drifted_features"],
        "baseline": report["baseline"],
        "current": report["current"],
        "recommended_model": report["recommended_model"],
    }


def send_webhook(alert: dict) -> dict:
    """Simulated webhook: append the payload we WOULD POST to a log file."""
    delivery = {"sent_at": utc_now(), "target": "https://hooks.example.com/ml-oncall",
                "status": "simulated_200", "payload": alert}
    with open(ALERTS_DIR / "webhook_log.jsonl", "a") as f:
        f.write(json.dumps(delivery) + "\n")
    return delivery


def write_issue(alert: dict, report: dict) -> str:
    top = sorted(report["feature_psi"].items(), key=lambda kv: kv[1]["psi"], reverse=True)[:5]
    rows = "\n".join(f"| {name} | {v['psi']:.3f} | {v['level']} |" for name, v in top)
    cand = "\n".join(f"| {m} | {v['precision']:.3f} | {v['recall']:.3f} |"
                     for m, v in report["candidates"].items())
    rec = alert["recommended_model"]
    md = f"""# [{alert['severity'].upper()}] Fraud model degraded on post-rollout traffic

**Detected:** {alert['timestamp']}  
**Live model (cluster):** `{alert['serving_model']}`  
**Alert id:** `{alert['alert_id']}`

## What happened
{chr(10).join('- ' + r for r in alert['reasons'])}

| Metric | Baseline | Current |
|---|---|---|
| Precision | {report['baseline']['precision']:.3f} | {report['current']['precision']:.3f} |
| Recall | {report['baseline']['recall']:.3f} | {report['current']['recall']:.3f} |

## Most drifted features (PSI: <0.10 stable, 0.10-0.25 moderate, >0.25 major)
| Feature | PSI | Level |
|---|---|---|
{rows}

## Candidate models scored on the drifted batch
| Model | Precision | Recall |
|---|---|---|
{cand}

## Recommended action (GitOps)
Do **not** change the cluster by hand (no `kubectl edit`, no `kubectl set`). Open a pull request that sets
`active_model: {rec}` in `deploy/config/model.yaml`. After it merges to `main`, Argo CD detects the
change, syncs the cluster and performs a rolling update. Wait for
`kubectl -n fraud rollout status deploy/fraud-service`, then re-run `drift_monitor.py` to confirm recovery.
"""
    (ALERTS_DIR / "GENERATED_ISSUE.md").write_text(md)
    return md


def send_alert(report: dict) -> dict:
    ALERTS_DIR.mkdir(exist_ok=True)
    alert = build_alert(report)
    write_json(ALERTS_DIR / "alert.json", alert)
    send_webhook(alert)
    write_issue(alert, report)
    return alert
