#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Iterable


def _read_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            yield json.loads(line)


def _safe_str(value: Any) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return str(value)


def main() -> None:
    ap = argparse.ArgumentParser(description="Merge tb_script/prepare_script from artifacts into dataset jsonl.")
    ap.add_argument("--input", required=True, help="Input dataset jsonl")
    ap.add_argument(
        "--artifacts-root",
        default="artifacts/s09_tbgen",
        help="Artifacts root directory relative to project root",
    )
    ap.add_argument("--output", required=True, help="Output dataset jsonl")
    ap.add_argument("--success-only", action="store_true",
                    help="Only output PRs with successful tbgen results")
    args = ap.parse_args()

    input_path = Path(args.input).resolve()
    artifacts_root = Path(args.artifacts_root).resolve()
    output_path = Path(args.output).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    merged = 0
    missing = 0
    failed = 0
    total = 0

    with output_path.open("w", encoding="utf-8") as out:
        for pr in _read_jsonl(input_path):
            total += 1
            org = _safe_str(pr.get("org", ""))
            repo = _safe_str(pr.get("repo", ""))
            number = int(pr["number"])

            pr.setdefault("tb_script", "")
            pr.setdefault("prepare_script", "")
            pr["tb_script"] = _safe_str(pr.get("tb_script", ""))
            pr["prepare_script"] = _safe_str(pr.get("prepare_script", ""))

            result_path = artifacts_root / f"{org}__{repo}" / f"pr-{number}" / "result.json"
            if not result_path.exists():
                missing += 1
                if not args.success_only:
                    out.write(json.dumps(pr, ensure_ascii=False) + "\n")
                continue

            try:
                result = json.loads(result_path.read_text(encoding="utf-8"))
            except Exception:
                failed += 1
                if not args.success_only:
                    out.write(json.dumps(pr, ensure_ascii=False) + "\n")
                continue

            if result.get("status") != "success":
                failed += 1
                if not args.success_only:
                    out.write(json.dumps(pr, ensure_ascii=False) + "\n")
                continue

            pr["tb_script"] = _safe_str(result.get("tb_script", ""))
            pr["prepare_script"] = _safe_str(result.get("prepare_script", ""))
            merged += 1
            out.write(json.dumps(pr, ensure_ascii=False) + "\n")

    print(
        "\n".join(
            [
                "[MERGE SUMMARY]",
                f"total={total}",
                f"merged_success={merged}",
                f"missing_result={missing}",
                f"failed_or_non_success={failed}",
                f"output={output_path}",
            ]
        )
    )


if __name__ == "__main__":
    main()
