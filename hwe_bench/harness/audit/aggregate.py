#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any


def _read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def aggregate_run(run_root: Path) -> dict[str, Any]:
    prepared = _read_json(run_root / "prepared_batches.json")
    run_meta = _read_json(run_root / "run_meta.json")

    batch_results: list[dict[str, Any]] = []
    cases: list[dict[str, Any]] = []
    missing_batches: list[str] = []
    duplicate_numbers: list[int] = []
    seen_numbers: set[int] = set()

    for batch in prepared.get("batches", []):
        batch_id = batch["batch_id"]
        batch_result_path = run_root / "batches" / batch_id / "batch_result.json"
        if not batch_result_path.exists():
            missing_batches.append(batch_id)
            continue
        result = _read_json(batch_result_path)
        batch_results.append(result)
        for case in result.get("cases", []):
            number = int(case["number"])
            if number in seen_numbers:
                duplicate_numbers.append(number)
                continue
            seen_numbers.add(number)
            case = dict(case)
            case["batch_id"] = batch_id
            cases.append(case)

    status_counts = Counter(case.get("status", "unknown") for case in cases)
    confidence_counts = Counter(
        (case.get("patch_review") or {}).get("confidence", "unknown") for case in cases
    )
    trajectory_counts = Counter(
        (case.get("trajectory_audit") or {}).get("verdict", "missing") for case in cases
    )
    patch_review_counts = Counter(
        (case.get("patch_review") or {}).get("verdict", "missing") for case in cases
    )

    false_negative_queue: list[dict[str, Any]] = []
    manual_review_queue: list[dict[str, Any]] = []

    for case in cases:
        patch_review = case.get("patch_review") or {}
        trajectory_review = case.get("trajectory_audit") or {}
        verdict = patch_review.get("verdict", "")

        if verdict in {"high_conf_false_negative", "needs_investigation"}:
            false_negative_queue.append(
                {
                    "number": case["number"],
                    "status": case.get("status"),
                    "batch_id": case["batch_id"],
                    "verdict": verdict,
                    "reason": patch_review.get("reason", ""),
                }
            )

        needs_manual = False
        if patch_review.get("confidence") != "high":
            needs_manual = True
        if trajectory_review.get("verdict") != "clean":
            needs_manual = True
        if verdict in {"suspicious", "hack", "high_conf_false_negative", "needs_investigation"}:
            needs_manual = True

        if needs_manual:
            manual_review_queue.append(
                {
                    "number": case["number"],
                    "status": case.get("status"),
                    "batch_id": case["batch_id"],
                    "trajectory_verdict": trajectory_review.get("verdict"),
                    "patch_verdict": verdict,
                    "confidence": patch_review.get("confidence"),
                }
            )

    final_report = {
        "schema_version": "audit.final.v1",
        "org": run_meta["org"],
        "repo": run_meta["repo"],
        "agent": run_meta["agent"],
        "dataset": run_meta["dataset"],
        "run_root": str(run_root.resolve()),
        "source": run_meta,
        "summary": {
            "prepared_case_count": prepared.get("case_count", 0),
            "audited_case_count": len(cases),
            "prepared_batch_count": prepared.get("batch_count", 0),
            "completed_batch_count": len(batch_results),
            "missing_batches": missing_batches,
            "duplicate_numbers": sorted(set(duplicate_numbers)),
            "status_counts": dict(status_counts),
            "confidence_counts": dict(confidence_counts),
            "trajectory_verdict_counts": dict(trajectory_counts),
            "patch_review_verdict_counts": dict(patch_review_counts),
            "false_negative_queue_count": len(false_negative_queue),
            "manual_review_queue_count": len(manual_review_queue),
        },
        "batches": [
            {
                "batch_id": result.get("batch_id"),
                "case_count": len(result.get("cases", [])),
                "summary": result.get("summary", {}),
            }
            for result in batch_results
        ],
        "cases": cases,
    }

    _write_json(run_root / "final_audit_report.json", final_report)
    _write_jsonl(run_root / "false_negative_queue.jsonl", false_negative_queue)
    _write_jsonl(run_root / "manual_review_queue.jsonl", manual_review_queue)
    return final_report


def main() -> None:
    parser = argparse.ArgumentParser(description="Aggregate HWE-bench audit batch results.")
    parser.add_argument("--run-root", required=True, help="Prepared audit run root")
    args = parser.parse_args()

    final_report = aggregate_run(Path(args.run_root).resolve())
    print(json.dumps(final_report["summary"], indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()

