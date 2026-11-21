"""Compare primary detection weights using labeled, saved scan responses, without calling a model.

    python -m scripts.evaluate_fusion predictions.jsonl

Use the same JSONL rows as scripts.evaluate_detection, with calibration and test splits.
The chunks need their measured features, judge mode, raw Qwen score and raw confidence.
"""
from __future__ import annotations

import argparse
import gzip
import json
import math
from pathlib import Path

from scripts.evaluate_detection import summarize, validate_rows
from services.aggregate import document_assessment, score_chunk


WEIGHTS = (0.0, 0.5, 0.7, 0.85, 1.0)
METRICS = ("count", "human_count", "ai_count", "assessed_count", "false_positive_count",
           "true_positive_count", "human_false_positive_rate", "ai_recall", "coverage", "binary_ranking_auroc")
FEATURES = ("char_count", "sentence_count", "paragraph_count", "burstiness", "sentence_length_std",
            "lexical_diversity", "repeated_ngram_ratio", "connector_density", "punctuation_density",
            "paragraph_length_variance")


def predict(row: dict, weight: float) -> dict:
    saved = row["prediction"]
    genre = saved["analysis"]["genre"]
    chunks = []
    for chunk in saved["chunks"]:
        features = {key: chunk["features"][key] for key in FEATURES}
        if not all(math.isfinite(x) for x in features.values()) or features["char_count"] <= 0:
            raise ValueError(f"Invalid chunk measurements for {row['id']}")
        mode = chunk["judge_mode"]
        if mode not in {"qwen", "heuristic_skip", "heuristic_fallback"}:
            raise ValueError(f"Unknown judge mode for {row['id']}")
        if weight and mode == "qwen":
            raw = chunk["score_breakdown"]
            if not (0 <= raw["qwen_score"] <= 100 and 0 <= raw["qwen_confidence"] <= 1):
                raise ValueError(f"Invalid Qwen measurements for {row['id']}")
            judge = {"judge_mode": mode, "ai_score": raw["qwen_score"], "confidence": raw["qwen_confidence"]}
        else:
            mode = "heuristic_skip" if weight == 0 else mode
            judge = {"judge_mode": mode, "confidence": 0.38}
        scored = score_chunk(features, judge, genre, qwen_weight=weight)
        chunks.append({**scored, "features": features, "judge_mode": mode})
    score, confidence, _ = document_assessment(chunks, genre, saved["document_features"])
    return {**row, "prediction": {"risk_score": round(score, 2),
                                  "assessment_status": "insufficient_evidence" if confidence == "low" else "assessed"}}


def metrics(rows: list[dict], threshold: float) -> dict:
    all_metrics = summarize(rows, threshold)
    return {key: all_metrics[key] for key in METRICS}


def compare(rows: list[dict], threshold: float = 70, max_fpr: float = .05) -> dict:
    validate_rows(rows)
    if not 0 <= threshold <= 100 or not 0 <= max_fpr <= 1:
        raise ValueError("Invalid score threshold or false-positive limit")
    models = {(r["prediction"]["engine"]["scan_api_source"], r["prediction"]["engine"]["qwen_model"]) for r in rows}
    if len(models) != 1:
        raise ValueError("Compare one detection provider and model at a time")
    results = {}
    for split in ("calibration", "test"):
        source = [r for r in rows if r["split"] == split]
        if not {"human", "ai"}.issubset({r["label"] for r in source}):
            raise ValueError(f"Missing human or AI examples in {split}")
        results[split] = {}
        for weight in WEIGHTS:
            scored = [predict(row, weight) for row in source]
            by_language = {language: metrics([r for r in scored if r["language"] == language], threshold)
                           for language in sorted({r["language"] for r in scored})}
            results[split][weight] = {**metrics(scored, threshold), "by_language": by_language}
    calibration = results["calibration"]
    eligible = [w for w, m in calibration.items() if m["human_false_positive_rate"] <= max_fpr and m["assessed_count"]]
    selected = max(eligible, key=lambda w: (calibration[w]["ai_recall"], calibration[w]["coverage"],
                                            -calibration[w]["human_false_positive_rate"], -abs(w - .7))) if eligible else None
    provider, model = next(iter(models))
    return {"provider": provider, "model": model, "threshold": threshold, "max_calibration_fpr": max_fpr,
            "selected_on_calibration": selected, **results}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--threshold", type=float, default=70)
    parser.add_argument("--max-fpr", type=float, default=.05)
    args = parser.parse_args()
    opener = gzip.open if args.input.suffix == ".gz" else open
    try:
        with opener(args.input, "rt", encoding="utf-8") as stream:
            report = compare([json.loads(line) for line in stream if line.strip()], args.threshold, args.max_fpr)
    except (ValueError, KeyError, TypeError, OSError) as exc:
        parser.error(str(exc))
    print(json.dumps(report, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
