# GitOps Drift Monitor Lab: Documentation (Argo CD edition)

## 1. Overview

This lab runs a real GitOps loop on a local Kubernetes cluster:

1. A **fraud-scoring service** runs as 3 pods on kind (Kubernetes in Docker).
2. **Argo CD** watches this Git repository and keeps the cluster identical to `deploy/`.
3. A **drift monitor** sends recent labelled traffic to the live pods, measures data drift (PSI) and performance drift (precision/recall), and raises an alert when the model has degraded.
4. You respond by changing `active_model` in `deploy/config/model.yaml` through Git. Argo CD notices, syncs, and Kubernetes performs a rolling update.
5. You re-run the monitor to verify recovery.

Nothing in the cluster is ever changed by hand. Git is the only input; Argo CD is the only actor.

### Learning objectives

- Explain GitOps: declarative desired state in Git, a reconciling agent, drift correction, audit and rollback.
- Operate Argo CD: read Sync and Health status, trigger a refresh, roll back, and observe self-healing.
- Tell data drift apart from performance drift, and compute and interpret PSI.
- Change production behaviour safely through a commit and verify the outcome.

### What is real and what is synthetic

| Real | Synthetic |
|---|---|
| Kubernetes (kind), Argo CD, Kustomize, rolling updates | The transaction data |
| A live HTTP scoring service in pods | The fraud patterns and the "rollout" scenario |
| Trained scikit-learn models | The webhook (payload is logged to a file, no network call) |
| Git history as the audit trail | Alert thresholds (tuned for the lab, not production guidance) |

---

## 2. Concepts

### 2.1 How GitOps works here

| GitOps idea | In this lab |
|---|---|
| Desired state, in Git | `deploy/` (manifests) and `deploy/config/model.yaml` (which model) |
| Actual state | The Deployment, ConfigMap and pods in namespace `fraud` |
| Reconciler | Argo CD (`argocd/application.yaml`) |
| Change mechanism | Commit and push to `main` (optionally through a pull request) |
| Rollback | `git revert`, push |
| Drift correction | Argo CD `selfHeal: true` reverts manual cluster changes |

### 2.2 How a one-line YAML change becomes a new model in production

1. You change `active_model` in `deploy/config/model.yaml` and push.
2. Argo CD renders `deploy/` with Kustomize. The `configMapGenerator` in `kustomization.yaml` creates a ConfigMap whose **name includes a hash of the file's content** (for example `model-config-fd296dbd95`).
3. The content changed, so the hash and therefore the ConfigMap name changed. Kustomize rewrites the Deployment's volume to reference the new name.
4. The pod template changed, so Kubernetes starts a **rolling update**: it starts a new pod, waits for its readiness probe, then retires an old one (`maxSurge: 1`, `maxUnavailable: 0`).
5. Each new pod reads `/etc/fraud/model.yaml` at startup and loads the named model from the image.

The image (`fraud-service:1.0`) contains code and all three models and never changes during the lab. Configuration alone selects the live model.

### 2.3 Data drift vs. performance drift

- **Data drift**: input distributions changed (more night traffic, newer accounts). Measured with PSI. Common and not always harmful.
- **Performance drift**: precision or recall on recent labelled traffic fell below baseline. This is what hurts the business.

The monitor alerts only on **performance drift**; data drift is context.

### 2.4 PSI

```
PSI = Σ (actual_i − expected_i) × ln(actual_i / expected_i)
```

| PSI | Meaning |
|---|---|
| < 0.10 | Stable |
| 0.10 to 0.25 | Moderate |
| ≥ 0.25 | Major |

Bins are quantile bins from the reference batch, stored in `reference_stats.json`. Proportions are clipped at 0.0001.

### 2.5 The scenario

Pre-rollout fraud is driven by merchant risk, foreign transactions, velocity, distance and amount. Post-rollout fraud follows a new pattern driven by **night-time activity** and **new accounts**, and traffic shifts toward larger amounts and newer accounts. Model v2 (trained on the old world) keeps applying old rules and its precision collapses; v3 has seen post-rollout data and recovers.

---

## 3. Architecture

```
 GitHub repo (main)  <--- you commit / push / PR
        |
        | Argo CD polls (~3 min) or is refreshed
        v
   Argo CD (ns: argocd) --renders deploy/ with Kustomize--> applies to cluster
                                                                |
                                                                v
                         ns: fraud   Deployment (3 pods) + Service + ConfigMap(model.yaml)
                                                                ^
                                                                | POST /score_batch
   data/drifted_batch.csv --> scripts/drift_monitor.py ---------+
                                     |  compares with reference_stats.json
                                     v
                    alerts/alert.json, webhook_log.jsonl, GENERATED_ISSUE.md
                                     |
                       human reads the issue --> edits deploy/config/model.yaml --> (loop)
```

---

## 4. Repository reference

| Path | Purpose |
|---|---|
| `app/main.py` | FastAPI service. Loads the model named in `MODEL_CONFIG` at startup; exits with an error if the file is missing |
| `Dockerfile`, `.dockerignore` | Builds `fraud-service:1.0` with code and `models/` (scikit-learn pinned to 1.8.0 to match the pickles) |
| `deploy/kustomization.yaml` | Lists resources and generates the `model-config` ConfigMap from `config/model.yaml` |
| `deploy/deployment.yaml` | 3 replicas, rolling update, readiness and liveness probes on `/healthz`, ConfigMap mounted at `/etc/fraud` |
| `deploy/service.yaml` | ClusterIP service, port 80 to container port 8000 |
| `deploy/config/model.yaml` | **The GitOps source of truth.** One field: `active_model` |
| `argocd/application.yaml` | Argo CD Application: repo, branch `main`, path `deploy`, automated sync with `prune` and `selfHeal`, `CreateNamespace` |
| `scripts/bootstrap.sh` | Creates the kind cluster, builds and loads the image, installs Argo CD, applies the Application |
| `scripts/task1_setup.py` | Generates data and models, writes baselines and the initial `model.yaml`; clears `alerts/` |
| `scripts/drift_monitor.py` | Tasks 2 to 4 and the Task 6 re-run |
| `scripts/alerting.py` | Writes alert files |
| `scripts/grade.py` | Automated grader |
| `scripts/common.py` | Shared paths, PSI, metrics helpers |

### Service API

| Endpoint | Description |
|---|---|
| `GET /healthz` | Readiness and liveness probe |
| `GET /status` | `serving_model`, model `sha256`, `loaded_at`, `pod` name |
| `POST /score` | Score one transaction (JSON with the 7 feature fields) |
| `POST /score_batch` | `{"records": [...]}` returns `{"model", "pod", "predictions"}` |

Example:

```bash
curl -s localhost:9000/status
curl -s -X POST localhost:9000/score -H 'content-type: application/json' \
  -d '{"amount":250,"hour":3,"merchant_risk":0.6,"txn_velocity":6,"distance_from_home_km":40,"account_age_days":30,"is_foreign":0}'
```

Try the same request before and after the promotion.

### Datasets and models

Both CSVs have columns `amount`, `hour`, `merchant_risk`, `txn_velocity`, `distance_from_home_km`, `account_age_days`, `is_foreign`, `is_fraud`.

| Model | Type | Trained on | Role |
|---|---|---|---|
| `fraud_model_v1.pkl` | Scaled logistic regression | 4,000 reference rows | High recall, poor precision |
| `fraud_model_v2.pkl` | Random forest | Same 4,000 rows | **Live at start**, degrades on new traffic |
| `fraud_model_v3.pkl` | Random forest | 4,000 reference rows plus 3,000 separate post-rollout rows | **The fix.** Never sees `drifted_batch.csv`, so no leakage |

The remaining 1,000 reference rows are held out to compute the baseline metrics.

---

## 5. Drift monitor reference

`python scripts/drift_monitor.py [--url http://localhost:9000] [--no-alert]`

It sends `drifted_batch.csv` to `/score_batch` in chunks, so it measures the **pods that are actually running**, and also reports whether Git and the cluster agree.

| Exit code | Meaning |
|---|---|
| 0 | Healthy |
| 2 | Alert raised |
| 3 | Rollout in progress: pods are serving different models, so no verdict is given. Wait and re-run |
| 4 | Service unreachable: start the port-forward |

| Status | Meaning |
|---|---|
| `ALERT` | Precision fell by more than 0.20 or recall by more than 0.10 vs. baseline |
| `OK_WITH_DATA_DRIFT` | Performance within tolerance, but a feature has PSI ≥ 0.25 |
| `OK` | Performance within tolerance and no major feature drift |

Thresholds are constants at the top of `drift_monitor.py`. `last_check.json` is written on every run and contains `desired_model` (Git), `serving_model` (cluster), `service_in_sync`, per-feature PSI, current vs. baseline metrics, and candidate model scores.

### Alert files

| File | Contents |
|---|---|
| `alerts/alert.json` | id, severity, `serving_model`, reasons, PSI summary, baseline vs. current metrics, `recommended_model` |
| `alerts/webhook_log.jsonl` | Simulated webhook deliveries, one JSON object per line |
| `alerts/GENERATED_ISSUE.md` | Ready-to-file issue with a recommended GitOps action |
| `alerts/last_check.json` | Full report from the latest run |

---

## 6. Step-by-step walkthrough

```bash
# Setup (once)
git init -b main && git add -A && git commit -m "initial: v2 live"
git remote add origin https://github.com/<you>/fraud-gitops-lab.git && git push -u origin main
./scripts/bootstrap.sh https://github.com/<you>/fraud-gitops-lab.git
kubectl -n fraud port-forward svc/fraud-service 9000:80        # separate terminal

# Detect and alert (Tasks 2-4)
python scripts/drift_monitor.py                                  # ALERT, exit code 2
cat alerts/GENERATED_ISSUE.md

# Promote through Git (Task 5)
git checkout -b promote-v3
sed -i 's/fraud_model_v2.pkl/fraud_model_v3.pkl/' deploy/config/model.yaml
git commit -am "promote v3: v2 precision collapsed on post-rollout traffic"
git push -u origin promote-v3      # open a PR on GitHub and merge it (or merge locally and push main)
git checkout main && git pull
kubectl -n argocd annotate application fraud-service argocd.argoproj.io/refresh=hard --overwrite
kubectl -n fraud rollout status deploy/fraud-service

# Verify (Task 6)
# (if your port-forward was attached to an old pod, stop it and start it again)
python scripts/drift_monitor.py                                  # OK_WITH_DATA_DRIFT
python scripts/grade.py
```

Note: `kubectl port-forward svc/...` attaches to one pod. After a rollout that pod is gone, so restart the port-forward before re-running the monitor.

### Expected numbers

| | Precision | Recall |
|---|---|---|
| Baseline (v2 on held-out reference) | 0.656 | 0.504 |
| v2 on the drifted batch | 0.122 | 0.449 |
| v3 on the drifted batch | 0.529 | 0.461 |

---

## 7. Grader

`python scripts/grade.py` needs `kubectl` pointed at the kind cluster and the repo pushed to GitHub. It checks: data and models exist; the alert was raised while v2 was serving and recommends v3; `deploy/config/model.yaml` says v3 with at least 2 Git commits; the Argo CD application is **Synced and Healthy**; Argo CD's synced revision equals your local Git `HEAD` (the change was deployed from Git); Argo CD has at least 2 deployed revisions; all replicas updated and ready; and the final monitor run scored a live v3 service in sync with Git.

---

## 8. Troubleshooting

| Symptom | Cause and fix |
|---|---|
| Argo CD app shows `Unknown` or a repo error | Repo URL wrong, branch not `main`, repo not pushed, or repo private (register credentials in Argo CD or make it public) |
| Pods `ErrImageNeverPull` / `ImagePullBackOff` | Image not loaded into kind: `kind load docker-image fraud-service:1.0 --name gitops-lab` |
| Pods `CrashLoopBackOff` | Check `kubectl -n fraud logs deploy/fraud-service`; usually `active_model` names a file that is not in the image |
| Pushed a change but nothing happens | Argo CD polls about every 3 minutes; click Refresh or use the `refresh=hard` annotation |
| Monitor exit code 3 | Rollout still in progress; wait for `rollout status` and re-run |
| Monitor exit code 4 | Port-forward not running or attached to a deleted pod; restart it |
| Monitor says cluster differs from Git | Change not pushed, or Argo CD has not synced yet |
| Pickle or scikit-learn version errors locally | `pip install -r requirements.txt` (scikit-learn is pinned to 1.8.0) |
| Regenerated models but the cluster serves old ones | Rebuild the image, `kind load docker-image ...`, then restart the deployment |
| Grader `revision` check fails | Push your latest commit, then refresh the Argo CD app |

## 9. Extending the lab

- Replace the simulated webhook in `alerting.py` with a real HTTP POST.
- Add Task 9: have the alert step open a pull request automatically.
- Use Argo CD sync waves or Argo Rollouts for a canary promotion instead of a plain rolling update.
- Add a GitHub webhook to Argo CD so syncs are instant instead of polled.
- Run the monitor as a Kubernetes CronJob.

## 10. Instructor notes

- **Assessment:** `grade.py` verifies the end state and that it came through Git and Argo CD.
- **Discussion 1:** PSI is still high after the fix. Why is the status healthy? (The model was trained on the new traffic; input drift is not the same as output harm.)
- **Discussion 2:** Why must the fix go through `deploy/config/model.yaml` and not `kubectl`? (Audit trail, review, rollback, and Argo CD would revert a manual change anyway.)
- **Discussion 3:** What protects users if a bad config is pushed? (Readiness probes and `maxUnavailable: 0` keep old pods serving while new ones crash.)
- **Validation note:** the manifests render correctly with Kustomize and the service and monitor were tested end to end locally. The kind and Argo CD bootstrap was not executed in the authoring environment, so do one dry run before teaching.
