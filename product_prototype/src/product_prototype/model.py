"""Serializable models with explicit scoring and fee-proxy definitions."""
from __future__ import annotations

import warnings
import hashlib
from dataclasses import dataclass

import numpy as np
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.exceptions import ConvergenceWarning
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression, Ridge
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, OrdinalEncoder, StandardScaler


def proxy_fee(frame: pd.DataFrame, proxy: dict) -> np.ndarray:
    fallback = float(proxy["fallback_fee_yen"])
    if fallback < 0:
        raise ValueError("fallback_fee_yen cannot be negative")
    if proxy["kind"] == "constant":
        return np.full(len(frame), fallback)
    if proxy["kind"] not in ("salary_midpoint", "delta_salary"):
        raise ValueError("Unknown fee proxy kind")
    low = pd.to_numeric(frame[proxy["lower_column"]], errors="raise").to_numpy(dtype=float)
    high = pd.to_numeric(frame[proxy["upper_column"]], errors="raise").to_numpy(dtype=float)
    if np.any((np.isfinite(low) & (low < 0)) | (np.isfinite(high) & (high < 0))) or np.any(np.isfinite(low) & np.isfinite(high) & (low > high)):
        raise ValueError("Salary lower bounds must be nonnegative and <= upper bounds")
    estimated = (low + high) / 2
    if proxy["kind"] == "delta_salary":
        desired = pd.to_numeric(frame[proxy["desired_column"]], errors="raise").to_numpy(dtype=float)
        weight = float(proxy.get("desired_weight", 0.5))
        if not 0 <= weight <= 1:
            raise ValueError("desired_weight must be between zero and one")
        desired = np.where(np.isfinite(desired), desired, estimated)
        estimated = (1 - weight) * estimated + weight * np.clip(desired, low, high)
    rate = np.full(len(frame), float(proxy.get("fee_rate", 1 / 3)))
    if proxy.get("rate_column"):
        observed = pd.to_numeric(frame[proxy["rate_column"]], errors="raise").to_numpy(dtype=float)
        rate = np.where(np.isfinite(observed), observed, rate)
    if np.any(rate < 0) or float(proxy.get("salary_unit_yen", 10000)) <= 0:
        raise ValueError("Fee rates must be nonnegative and salary_unit_yen positive")
    result = estimated * float(proxy.get("salary_unit_yen", 10000)) * rate
    return np.where(np.isfinite(result), result, fallback)


def feature_frame(frame: pd.DataFrame, numeric: list[str], categorical: list[str]) -> pd.DataFrame:
    result = frame[numeric + categorical].copy()
    for column in numeric:
        result[column] = pd.to_numeric(result[column], errors="raise").astype(float)
        if np.isinf(result[column]).any():
            raise ValueError(f"Infinite feature value: {column}")
    for column in categorical:
        result[column] = result[column].astype("string").fillna("__missing__").astype(object)
    return result


def preprocessor(numeric: list[str], categorical: list[str], *, histogram: bool) -> ColumnTransformer:
    numeric_steps = [("impute", SimpleImputer(strategy="median", keep_empty_features=True))]
    if not histogram:
        numeric_steps.append(("scale", StandardScaler()))
    if histogram:
        encoding = OrdinalEncoder(handle_unknown="use_encoded_value", unknown_value=-1, max_categories=128)
    else:
        encoding = OneHotEncoder(handle_unknown="ignore", min_frequency=5, max_categories=128)
    transformers = []
    if numeric:
        transformers.append(("numeric", Pipeline(numeric_steps), numeric))
    if categorical:
        transformers.append(("categorical", encoding, categorical))
    return ColumnTransformer(transformers, sparse_threshold=0 if histogram else 0.3)


@dataclass
class RankingModel:
    classifier: Pipeline
    fee_model: Pipeline | None
    numeric: list[str]
    categorical: list[str]
    scorer: dict
    proxy: dict
    metadata: dict

    def predict(self, frame: pd.DataFrame) -> pd.DataFrame:
        features = feature_frame(frame, self.numeric, self.categorical)
        probability = self.classifier.predict_proba(features)[:, 1]
        fees = proxy_fee(frame, self.proxy)
        kind = self.scorer["kind"]
        if kind == "probability":
            score = probability
        elif kind == "probability_times_proxy":
            score = probability * fees
        elif kind == "odds_times_proxy":
            # Avoid overflow for numerically saturated probabilities.
            bounded = np.clip(probability, 1e-9, 1 - 1e-9)
            score = bounded / (1 - bounded) * fees
        else:
            assert self.fee_model is not None
            fees = np.maximum(0, self.fee_model.predict(features))
            score = probability * fees
        if not np.isfinite(score).all():
            raise ValueError("The model produced a non-finite ranking score")
        return pd.DataFrame({"probability": probability, "score": score,
                             "predicted_fee_if_positive": fees}, index=frame.index)


def fit_model(frame: pd.DataFrame, features: dict, model: dict, scorer: dict, proxy: dict,
              fee_column: str | None, seed: int, fee_frame: pd.DataFrame | None = None) -> RankingModel:
    numeric, categorical = features["numeric"], features["categorical"]
    x = feature_frame(frame, numeric, categorical)
    y = frame["_label"].to_numpy(dtype=int)
    if len(np.unique(y)) != 2:
        raise ValueError("Training data requires both positive and negative observations")
    kind = model["kind"]
    parameters = dict(model.get("parameters", {}))
    parameters.setdefault("random_state", seed)
    if kind == "logistic":
        parameters.setdefault("max_iter", 500)
        classifier = LogisticRegression(**parameters)
    elif kind == "hist_gradient_boosting":
        parameters.setdefault("max_iter", 150)
        parameters.setdefault("early_stopping", False)
        parameters.setdefault("categorical_features", list(range(len(numeric), len(numeric) + len(categorical))))
        classifier = HistGradientBoostingClassifier(**parameters)
    else:
        raise ValueError(f"Unknown classifier kind: {kind}")
    pipeline = Pipeline([("features", preprocessor(numeric, categorical, histogram=kind != "logistic")),
                         ("model", classifier)])
    weights = None
    if model.get("sample_weight", "none") == "inverse_sampling":
        weights = 1 / frame["_sampling_probability"].to_numpy(dtype=float)
        weights /= weights.mean()
    elif model.get("sample_weight", "none") != "none":
        raise ValueError("sample_weight must be none or inverse_sampling")
    with warnings.catch_warnings():
        warnings.simplefilter("error", ConvergenceWarning)
        pipeline.fit(x, y, model__sample_weight=weights)
    fee_model = None
    fee_rows = 0
    if scorer["kind"] == "two_stage_fee":
        assert fee_column is not None
        amount_data = frame if fee_frame is None else fee_frame
        fees = pd.to_numeric(amount_data[fee_column], errors="raise")
        amount_data = amount_data.loc[(amount_data["_label"] == 1) & fees.notna()].copy()
        if scorer.get("fee_max_rows"):
            amount_data["_fee_hash"] = amount_data["_row_id"].map(
                lambda v: hashlib.sha256(f"{v}:seed={seed}".encode()).hexdigest())
            amount_data = amount_data.sort_values(["_fee_hash", "_row_id"]).head(scorer["fee_max_rows"])
        fees = pd.to_numeric(amount_data[fee_column], errors="raise")
        mask = fees.notna()
        fee_rows = int(mask.sum())
        if fee_rows < 2 or (fees[mask] < 0).any() or not np.isfinite(fees[mask]).all():
            raise ValueError("The fee model needs at least two positive rows with nonnegative observed fees")
        fee_model = Pipeline([("features", preprocessor(numeric, categorical, histogram=False)),
                              ("model", Ridge(alpha=scorer.get("ridge_alpha", 1.0), solver="lsqr"))])
        fee_model.fit(feature_frame(amount_data, numeric, categorical), fees.to_numpy(dtype=float))
    metadata = {"training_rows": len(frame), "fee_training_rows": fee_rows,
                "component_training_rows": len(frame) + fee_rows, "positive_rows": int(y.sum()),
                "kind": kind, "parameters": parameters, "sample_weight": model.get("sample_weight", "none"),
                "n_iter": int(classifier.n_iter_) if kind != "logistic" else classifier.n_iter_.tolist(),
                "latest_feature_leakage_accepted": True, "probability_calibrated": False}
    if fee_model is not None:
        metadata["fee_row_ids_sha256"] = hashlib.sha256("\n".join(sorted(amount_data["_row_id"])).encode()).hexdigest()
        metadata["fee_dataset"] = scorer.get("fee_dataset", "classifier_dataset")
    return RankingModel(pipeline, fee_model, numeric, categorical, scorer, proxy, metadata)
