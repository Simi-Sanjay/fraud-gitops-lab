# GitOps Drift Monitor Lab (real Argo CD on Kubernetes)

A fraud-scoring service runs on a local Kubernetes cluster (kind). **Argo CD** keeps the cluster in sync with this Git repo. A drift monitor scores live traffic against the running pods and raises an alert when the model degrades. You fix it by changing one line in Git, never by touching the cluster.

```
fraud-gitops-lab/
├── app/main.py                    FastAPI scoring service (runs in the pods)
├── Dockerfile                     image = service code + all three models
├── deploy/                        what Argo CD watches (Kustomize)
│   ├── config/model.yaml          GitOps source of truth: active_model
│   ├── kustomization.yaml         turns model.yaml into a ConfigMap
│   ├── deployment.yaml            3 replicas, rolling update, health probes
│   └── service.yaml
├── argocd/application.yaml        tells Argo CD: watch this repo, path deploy/, auto-sync + self-heal
├── data/                          reference_batch.csv (5,000), drifted_batch.csv (1,500)
├── models/                        fraud_model_v1/v2/v3.pkl (real trained models)
├── reference_stats.json           baseline histograms + baseline precision/recall
├── alerts/                        alert.json, webhook_log.jsonl, GENERATED_ISSUE.md, last_check.json
├── scripts/                       task1_setup.py, drift_monitor.py, alerting.py, grade.py, bootstrap.sh
└── docs/DOCUMENTATION.md          full documentation
```

## Prerequisites

Docker, [kind](https://kind.sigs.k8s.io), kubectl, git, a free GitHub account, and Python 3.11+ with `pip install -r requirements.txt`.

## Setup

```bash
git init -b main && git add -A && git commit -m "initial: v2 live"
# create an empty PUBLIC repo on GitHub named fraud-gitops-lab, then:
git remote add origin https://github.com/<you>/fraud-gitops-lab.git
git push -u origin main

./scripts/bootstrap.sh https://github.com/<you>/fraud-gitops-lab.git   # kind + image + Argo CD + Application
kubectl -n fraud port-forward svc/fraud-service 9000:80                 # keep running in its own terminal
```

Open the Argo CD UI (`kubectl -n argocd port-forward svc/argocd-server 8080:443`, then https://localhost:8080, user `admin`, password printed by the bootstrap script) and wait for **Synced / Healthy**.

## Tasks

**Task 1: Generate data and models.** The generated files are already included; read `scripts/task1_setup.py` and run it only if you want to regenerate them (regenerating changes the model files, so you must rebuild and reload the image afterwards). Confirm that `deploy/config/model.yaml` names `fraud_model_v2.pkl` and that `curl localhost:9000/status` reports the same model.

**Task 2: Measure data drift.** Run `python scripts/drift_monitor.py --no-alert` and study the PSI table comparing `drifted_batch.csv` with the histograms in `reference_stats.json`. Explain which features shifted most and why a high PSI alone should not page anyone.

**Task 3: Measure performance drift.** The same run sends the drifted batch to the live pods and compares precision and recall with the baseline. Identify which metric collapsed and which candidate model in `models/` handles the new traffic best.

**Task 4: Raise the alert.** Run `python scripts/drift_monitor.py` without `--no-alert`. It exits with code 2 and writes `alerts/alert.json`, `alerts/webhook_log.jsonl` and `alerts/GENERATED_ISSUE.md`. Read the generated issue.

**Task 5: Promote through Git, let Argo CD deploy.** Following the issue, edit `active_model` in `deploy/config/model.yaml` on a branch, open a pull request, merge it to `main` (or commit and push directly), then watch `kubectl -n argocd get application fraud-service -w` and `kubectl -n fraud rollout status deploy/fraud-service`. Use no `kubectl edit`, `set` or `apply` on the workload. Argo CD checks Git about every 3 minutes; click Refresh in the UI or run `kubectl -n argocd annotate application fraud-service argocd.argoproj.io/refresh=hard --overwrite` to speed it up.

**Task 6: Verify recovery.** Once the rollout finishes, re-run `python scripts/drift_monitor.py` and confirm the status is no longer `ALERT` even though feature drift remains. Explain why, then run `python scripts/grade.py`.

**Task 7: Roll back through Git.** Run `git revert` on your promotion commit and push. Watch Argo CD roll the cluster back to v2 and re-run the monitor to confirm the alert returns. Explain what the Git history now tells an auditor.

**Task 8: Cause drift and watch it heal.** Run `kubectl -n fraud scale deploy fraud-service --replicas=10` and watch Argo CD set it back to 3. Then set `selfHeal: false` in `argocd/application.yaml`, apply it, repeat, and explain why the app now stays OutOfSync.

## Bonus

Set `active_model` to a model file that does not exist and push: the new pods crash on startup, the old pods keep serving because `maxUnavailable: 0`, and Argo CD reports Degraded. Revert in Git to recover.

## Clean up

`kind delete cluster --name gitops-lab`
