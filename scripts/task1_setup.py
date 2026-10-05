"""TASK 1 - Generate data, train three real models, write baselines and initial state.

Outputs (all under the project root):
  data/reference_batch.csv   5,000 rows  pre-rollout traffic
  data/drifted_batch.csv     1,500 rows  post-rollout traffic (what you monitor)
  models/fraud_model_v1.pkl  logistic regression, trained pre-rollout only
  models/fraud_model_v2.pkl  random forest, trained pre-rollout only  (CURRENTLY LIVE)
  models/fraud_model_v3.pkl  random forest, retrained WITH post-rollout data (the fix)
  reference_stats.json       baseline histograms + baseline precision/recall of v2
  deploy/config/model.yaml   active_model: fraud_model_v2.pkl  (the file Argo CD watches)

Run:  python scripts/task1_setup.py
"""
import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from common import (ALERTS_DIR, CONFIG_PATH, DATA_DIR, FEATURES, LABEL,
                    MODELS_DIR, REF_STATS_PATH, bin_counts, evaluate,
                    utc_now, write_json)

SEED = 42
N_BINS = 10


def _norm(w):
    w = np.asarray(w, dtype=float)
    return w / w.sum()


HOUR_REF = _norm([1] * 6 + [3] * 3 + [6] * 10 + [4] * 3 + [2] * 2)   # daytime heavy
HOUR_DRIFT = _norm([5] * 6 + [3] * 3 + [4] * 10 + [3] * 3 + [4] * 2)  # more night traffic


def sigmoid(z):
    return 1.0 / (1.0 + np.exp(-z))


def make_batch(n: int, rng: np.random.Generator, drifted: bool) -> pd.DataFrame:
    if not drifted:
        # Pre-rollout world
        amount = rng.lognormal(3.8, 0.9, n)
        hour = rng.choice(24, n, p=HOUR_REF)
        merchant_risk = rng.beta(2, 5, n)
        velocity = rng.poisson(2.0, n)
        distance = rng.exponential(20, n)
        age = rng.gamma(4.0, 200, n)
        foreign = rng.binomial(1, 0.08, n)
        logit = (-7.6 + 1.5 * (np.log(amount) - 3.8) + 7.0 * merchant_risk
                 + 0.6 * velocity + 0.04 * distance + 3.5 * foreign - 0.0004 * age)
    else:
        # Post-rollout world: new channel -> different traffic mix AND a new fraud pattern
        amount = rng.lognormal(4.3, 1.0, n)
        hour = rng.choice(24, n, p=HOUR_DRIFT)
        merchant_risk = rng.beta(3, 3.5, n)
        velocity = rng.poisson(3.8, n)
        distance = rng.exponential(32, n)
        age = rng.gamma(2.0, 180, n)
        foreign = rng.binomial(1, 0.14, n)
        night = (hour <= 5).astype(float)
        new_acct = (age < 120).astype(float)
        logit = (-7.0 + 0.2 * (np.log(amount) - 4.3) + 1.0 * merchant_risk
                 + 0.20 * velocity + 4.5 * night + 4.5 * new_acct + 0.2 * foreign)
    is_fraud = (rng.random(n) < sigmoid(logit)).astype(int)
    return pd.DataFrame({
        "amount": amount.round(2), "hour": hour, "merchant_risk": merchant_risk.round(4),
        "txn_velocity": velocity, "distance_from_home_km": distance.round(2),
        "account_age_days": age.round(1), "is_foreign": foreign, LABEL: is_fraud,
    })


def rf():
    return RandomForestClassifier(n_estimators=120, min_samples_leaf=3, max_depth=12,
                                  class_weight={0: 1, 1: 4}, n_jobs=-1,
                                  random_state=SEED)


def main():
    for d in (DATA_DIR, MODELS_DIR, ALERTS_DIR, CONFIG_PATH.parent):
        d.mkdir(parents=True, exist_ok=True)
    # Reset artefacts from previous runs so the lab starts clean
    for p in ALERTS_DIR.iterdir():
        if p.name != ".gitkeep":
            p.unlink()

    rng = np.random.default_rng(SEED)
    ref = make_batch(5000, rng, drifted=False)
    drifted = make_batch(1500, rng, drifted=True)
    post_train = make_batch(3000, np.random.default_rng(SEED + 1), drifted=True)  # v3 only, no leakage
    ref.to_csv(DATA_DIR / "reference_batch.csv", index=False)
    drifted.to_csv(DATA_DIR / "drifted_batch.csv", index=False)

    ref_train, ref_hold = ref.iloc[:4000], ref.iloc[4000:]

    v1 = make_pipeline(StandardScaler(), LogisticRegression(max_iter=500, class_weight="balanced"))
    v1.fit(ref_train[FEATURES], ref_train[LABEL])
    v2 = rf().fit(ref_train[FEATURES], ref_train[LABEL])
    both = pd.concat([ref_train, post_train])
    v3 = rf().fit(both[FEATURES], both[LABEL])

    for name, m in (("v1", v1), ("v2", v2), ("v3", v3)):
        joblib.dump(m, MODELS_DIR / f"fraud_model_{name}.pkl", compress=3)

    # Baseline histograms (quantile bins from the reference batch)
    features = {}
    for f in FEATURES:
        edges = np.unique(np.quantile(ref[f], np.linspace(0, 1, N_BINS + 1)[1:-1]))
        counts = bin_counts(ref[f], edges)
        features[f] = {"bin_edges": [float(e) for e in edges],
                       "ref_proportions": [round(float(c) / len(ref), 6) for c in counts]}

    live = evaluate(v2, ref_hold)
    write_json(REF_STATS_PATH, {
        "generated_at": utc_now(),
        "reference_rows": len(ref),
        "features": features,
        "baseline": {"model": "fraud_model_v2.pkl", "eval_rows": len(ref_hold), **live},
    })

    CONFIG_PATH.write_text(
        "# GitOps source of truth for the scoring service.\n"
        "# Change active_model through a commit + push; Argo CD makes the cluster match.\n"
        "active_model: fraud_model_v2.pkl\n")

    print("Task 1 complete.")
    print(f"  reference fraud rate : {ref[LABEL].mean():.3f}")
    print(f"  drifted   fraud rate : {drifted[LABEL].mean():.3f}")
    print(f"  baseline (v2, held-out reference): precision={live['precision']} recall={live['recall']}")
    for n, m in (("v1", v1), ("v2", v2), ("v3", v3)):
        r = evaluate(m, drifted)
        print(f"  {n} on drifted batch: precision={r['precision']} recall={r['recall']}")


if __name__ == "__main__":
    main()
