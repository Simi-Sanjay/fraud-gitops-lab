"""Fraud scoring service (runs inside the Kubernetes pod).

The model to serve is chosen by `active_model` in a YAML file that Kubernetes mounts from a
ConfigMap generated from deploy/config/model.yaml (the GitOps source of truth).
The image contains ALL candidate models; configuration, not the image, decides which is live.
A missing/corrupt model makes the pod crash on startup, so Kubernetes never routes traffic to it.
"""
import hashlib
import os
import socket
from datetime import datetime, timezone
from pathlib import Path

import joblib
import pandas as pd
import yaml
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

FEATURES = ["amount", "hour", "merchant_risk", "txn_velocity",
            "distance_from_home_km", "account_age_days", "is_foreign"]
MODELS_DIR = Path(os.getenv("MODELS_DIR", "models"))
MODEL_CONFIG = Path(os.getenv("MODEL_CONFIG", "deploy/config/model.yaml"))


def _load():
    name = yaml.safe_load(MODEL_CONFIG.read_text())["active_model"]
    path = MODELS_DIR / name
    if not path.exists():
        raise RuntimeError(f"active_model '{name}' not found in {MODELS_DIR}")
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    return joblib.load(path), name, sha


MODEL, MODEL_NAME, MODEL_SHA = _load()   # fail fast at startup
LOADED_AT = datetime.now(timezone.utc).isoformat(timespec="seconds")
POD = socket.gethostname()

app = FastAPI(title="fraud-scoring-service")


class Batch(BaseModel):
    records: list[dict]


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/status")
def status():
    return {"serving_model": MODEL_NAME, "sha256": MODEL_SHA, "loaded_at": LOADED_AT, "pod": POD}


@app.post("/score")
def score(record: dict):
    try:
        row = pd.DataFrame([record])[FEATURES]
    except KeyError as e:
        raise HTTPException(422, f"missing feature: {e}")
    return {"model": MODEL_NAME, "pod": POD, "fraud": bool(MODEL.predict(row)[0]),
            "fraud_probability": round(float(MODEL.predict_proba(row)[0][1]), 4)}


@app.post("/score_batch")
def score_batch(batch: Batch):
    try:
        df = pd.DataFrame(batch.records)[FEATURES]
    except KeyError as e:
        raise HTTPException(422, f"missing feature: {e}")
    return {"model": MODEL_NAME, "pod": POD, "predictions": [int(p) for p in MODEL.predict(df)]}
