"""Offline evaluation of saved detector results; never calls model providers.

Usage: python scripts/evaluate_detection.py samples.jsonl --split test --threshold 70
Each JSONL row: id, group_id, split, label (human/ai/mixed), language, genre,
char_count, prediction (the /api/scan response). Related rewrites share group_id.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
import json
import math
from pathlib import Path


def validate_rows(rows: list[dict]) -> None:
    if not rows:
        raise ValueError("Dataset is empty")
    ids, group_splits = set(), {}
    for row in rows:
        identifier, group, split = row["id"], row["group_id"], row["split"]
        if not identifier or not group or not split:
            raise ValueError("id, group_id and split must be nonempty")
        if identifier in ids:
            raise ValueError(f"Duplicate sample id: {identifier}")
        ids.add(identifier)
        if group in group_splits and group_splits[group] != split:
            raise ValueError(f"Related source/rewrites cross dataset splits: {group}")
        group_splits[group] = split
        if row["label"] not in {"human", "ai", "mixed"}:
            raise ValueError(f"Invalid label for {identifier}")
        score = risk_score(row)
        if not math.isfinite(score) or not 0 <= score <= 100:
            raise ValueError(f"Invalid score for {identifier}")
        if row["prediction"].get("assessment_status") not in {"assessed", "insufficient_evidence"}:
            raise ValueError(f"Missing/invalid assessment_status for {identifier}")


def risk_score(row: dict) -> float:
    prediction = row["prediction"]
    return float(prediction["risk_score"] if "risk_score" in prediction else prediction["ai_probability"])


def ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def summarize(rows: list[dict], threshold: float) -> dict:
    assessed = [r for r in rows if r["prediction"]["assessment_status"] == "assessed"]
    human = [r for r in rows if r["label"] == "human"]
    ai = [r for r in rows if r["label"] == "ai"]
    predicted_ai = [r for r in assessed if risk_score(r) >= threshold]
    fp = sum(r["label"] == "human" for r in predicted_ai)
    tp = sum(r["label"] == "ai" for r in predicted_ai)
    # Ranking ignores abstention; operational FPR/recall above include abstentions in denominators.
    # Compute AUROC with tied ranks in O(n log n).
    ranked = sorted(human + ai, key=risk_score)
    positive_rank_sum = 0.0
    index = 0
    while index < len(ranked):
        end = index + 1
        while end < len(ranked) and risk_score(ranked[end]) == risk_score(ranked[index]):
            end += 1
        mean_rank = (index + 1 + end) / 2
        positive_rank_sum += mean_rank * sum(r["label"] == "ai" for r in ranked[index:end])
        index = end
    auc = ((positive_rank_sum - len(ai) * (len(ai) + 1) / 2) / (len(ai) * len(human))) if ai and human else None
    bins = []
    for lower in range(0, 100, 10):
        bucket = [r for r in ranked if lower <= risk_score(r) < lower + 10 or (lower == 90 and risk_score(r) == 100)]
        if bucket:
            bins.append({"range": [lower, lower + 10], "count": len(bucket),
                         "mean_risk_score": sum(map(risk_score, bucket)) / len(bucket),
                         "observed_ai_fraction": sum(r["label"] == "ai" for r in bucket) / len(bucket)})
    return {
        "count": len(rows), "human_count": len(human), "ai_count": len(ai),
        "mixed_count": len(rows) - len(human) - len(ai),
        "assessed_count": len(assessed), "coverage": ratio(len(assessed), len(rows)),
        "false_positive_count": fp, "true_positive_count": tp,
        "human_false_positive_rate": ratio(fp, len(human)),
        "ai_recall": ratio(tp, len(ai)),
        "human_abstention_rate": ratio(sum(r["prediction"]["assessment_status"] != "assessed" for r in human), len(human)),
        "ai_abstention_rate": ratio(sum(r["prediction"]["assessment_status"] != "assessed" for r in ai), len(ai)),
        "binary_ranking_auroc": auc,
        "risk_bins_not_calibrated_probabilities": bins,
    }


def evaluate(rows: list[dict], split: str = "test", threshold: float = 70) -> dict:
    validate_rows(rows)  # Check leakage across ALL splits before filtering.
    if not math.isfinite(threshold) or not 0 <= threshold <= 100:
        raise ValueError("Threshold must be between 0 and 100")
    selected = [r for r in rows if r["split"] == split]
    if not selected:
        raise ValueError(f"No samples for split {split}")
    slices = {}
    for field in ("language", "genre", "length"):
        groups = defaultdict(list)
        for row in selected:
            if field == "length":
                count = row.get("char_count")
                value = "unknown" if count is None else "short" if count < 120 else "medium" if count < 1000 else "long"
            else:
                value = row.get(field, "unknown")
            groups[value].append(row)
        slices[field] = {value: summarize(group, threshold) for value, group in sorted(groups.items())}
    return {"split": split, "threshold": threshold, "score_semantics": "uncalibrated_risk_score",
            "notes": "Mixed labels are excluded from binary metrics. Abstentions remain in FPR/recall denominators. Choose thresholds on calibration data, never on this test split.",
            "overall": summarize(selected, threshold), "slices": slices}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--split", default="test")
    parser.add_argument("--threshold", type=float, default=70)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        rows = [json.loads(line) for line in args.input.read_text().splitlines() if line.strip()]
        report = evaluate(rows, args.split, args.threshold)
    except (ValueError, KeyError, TypeError) as exc:
        parser.error(str(exc))
    output = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    if args.output:
        args.output.write_text(output)
    else:
        print(output, end="")


if __name__ == "__main__":
    main()
