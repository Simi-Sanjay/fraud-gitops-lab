"""Shared helpers for the drift-monitoring GitOps lab."""
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import yaml
from sklearn.metrics import precision_score, recall_score, f1_score

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
MODELS_DIR = ROOT / "models"
CONFIG_PATH = ROOT / "deploy" / "config" / "model.yaml"
REF_STATS_PATH = ROOT / "reference_stats.json"
ALERTS_DIR = ROOT / "alerts"

FEATURES = [
    "amount",
    "hour",
    "merchant_risk",
    "txn_velocity",
    "distance_from_home_km",
    "account_age_days",
    "is_foreign",
]
LABEL = "is_fraud"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def read_json(path: Path):
    with open(path) as f:
        return json.load(f)


def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(obj, f, indent=2)
        f.write("\n")


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def get_active_model() -> str:
    """Desired state: active_model declared in deploy/config/model.yaml (Git)."""
    with open(CONFIG_PATH) as f:
        cfg = yaml.safe_load(f)
    return cfg["active_model"]


def bin_counts(values, edges) -> np.ndarray:
    """Assign values to bins defined by interior edges (len(edges)+1 bins)."""
    idx = np.searchsorted(np.asarray(edges), np.asarray(values), side="left")
    return np.bincount(idx, minlength=len(edges) + 1)


def psi(expected, actual, eps: float = 1e-4) -> float:
    """Population Stability Index between two proportion vectors."""
    e = np.clip(np.asarray(expected, dtype=float), eps, None)
    a = np.clip(np.asarray(actual, dtype=float), eps, None)
    return float(np.sum((a - e) * np.log(a / e)))


def metrics_from_preds(y, pred) -> dict:
    return {
        "precision": round(float(precision_score(y, pred, zero_division=0)), 4),
        "recall": round(float(recall_score(y, pred, zero_division=0)), 4),
        "f1": round(float(f1_score(y, pred, zero_division=0)), 4),
    }


def evaluate(model, df) -> dict:
    return metrics_from_preds(df[LABEL], model.predict(df[FEATURES]))
