#!/usr/bin/env python3
"""Prepare tbgen input by filtering S8 scores and merging with raw PR data.

Reads the S8 scored file, applies configurable filtering criteria, then joins
with the raw dataset (S5) to produce a self-contained JSONL that run_batch.py
can consume directly.

Typical usage:

    python -m hwe_bench.harness.tbgen.prepare_input \
        --scored  datasets/pipeline/lowRISC/lowRISC__opentitan_s08_scored.jsonl \
        --raw     datasets/pipeline/lowRISC/lowRISC__opentitan_s05_raw_dataset.jsonl \
        --output  datasets/pipeline/lowRISC/lowRISC__opentitan_s09_tbgen_input.jsonl \
        --min-bv 2 --min-rs 1 --max-sc 1 \
        --min-lines 10 --max-lines 1000 --max-files 30 \
        --exclude-path full_chip_sw \
        --min-sw-cld 1
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PrKey = tuple[str, str, int]  # (org, repo, number)


def _pr_key(rec: dict[str, Any]) -> _PrKey:
    return (rec.get("org", ""), rec.get("repo", ""), int(rec["number"]))


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def _write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------

def _passes_filter(rec: dict[str, Any], args: argparse.Namespace,
                   raw_info: dict[str, Any]) -> bool:
    """Return True if this S8 record should be kept."""

    lines_changed = (raw_info.get("lines_added", 0)
                     + raw_info.get("lines_removed", 0))
    files_changed = len(raw_info.get("modified_files") or [])

    # Size filters
    if args.min_lines is not None and lines_changed < args.min_lines:
        return False
    if args.max_lines is not None and lines_changed > args.max_lines:
        return False
    if args.max_files is not None and files_changed > args.max_files:
        return False

    # Score filters
    if args.min_bv is not None and rec.get("benchmark_value", 0) < args.min_bv:
        return False
    if args.min_rs is not None and rec.get("reproducer_signal", 0) < args.min_rs:
        return False
    if args.max_sc is not None and rec.get("simulation_cost", 0) > args.max_sc:
        return False

    # Reproducer path exclusion
    if args.exclude_path:
        if rec.get("reproducer_path") in args.exclude_path:
            return False

    # SW cross-layer depth floor
    if args.min_sw_cld is not None:
        if (rec.get("level1") == "SW_BUG_FIX"
                and rec.get("cross_layer_depth", 0) < args.min_sw_cld):
            return False

    return True


# ---------------------------------------------------------------------------
# Merge
# ---------------------------------------------------------------------------

# S8 fields to carry over into the merged output
S8_FIELDS = [
    "level1", "level2",
    "benchmark_value", "cross_layer_depth", "reproducer_signal",
    "simulation_cost", "reproducer_path", "priority_score",
]


def _merge(raw: dict[str, Any], s8: dict[str, Any]) -> dict[str, Any]:
    """Build a merged record: raw PR data + S8 scores (incl. level1/level2)."""
    merged = dict(raw)
    for field in S8_FIELDS:
        if field in s8:
            merged[field] = s8[field]
    return merged


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(
        description="Filter S8 scores and merge with raw PR data for tbgen.",
    )
    ap.add_argument("--scored", required=True,
                    help="S8 scored JSONL path")
    ap.add_argument("--raw", required=True,
                    help="Raw dataset (S5) JSONL path")
    ap.add_argument("--output", required=True,
                    help="Output JSONL path for run_batch.py")

    # Size filters
    ap.add_argument("--min-lines", type=int, default=None,
                    help="Min lines changed (added+removed)")
    ap.add_argument("--max-lines", type=int, default=None,
                    help="Max lines changed (added+removed)")
    ap.add_argument("--max-files", type=int, default=None,
                    help="Max files changed")

    # Score filters
    ap.add_argument("--min-bv", type=int, default=None,
                    help="Min benchmark_value (inclusive)")
    ap.add_argument("--min-rs", type=int, default=None,
                    help="Min reproducer_signal (inclusive)")
    ap.add_argument("--max-sc", type=int, default=None,
                    help="Max simulation_cost (inclusive)")

    # Path exclusion
    ap.add_argument("--exclude-path", nargs="*", default=[],
                    help="Reproducer paths to exclude (e.g. full_chip_sw)")

    # SW-specific
    ap.add_argument("--min-sw-cld", type=int, default=None,
                    help="Min cross_layer_depth for SW_BUG_FIX PRs")

    args = ap.parse_args()

    # Load data — index by (org, repo, number) to avoid cross-repo collisions
    scored = _read_jsonl(Path(args.scored))
    raw_records = _read_jsonl(Path(args.raw))
    raw_by_key: dict[_PrKey, dict[str, Any]] = {
        _pr_key(r): r for r in raw_records
    }

    # Filter and merge
    output: list[dict[str, Any]] = []
    skipped_no_raw = 0
    skipped_status = 0
    seen: set[_PrKey] = set()

    for rec in scored:
        if rec.get("status") != "ok":
            skipped_status += 1
            continue

        key = _pr_key(rec)
        if key in seen:
            continue
        seen.add(key)

        if key not in raw_by_key:
            skipped_no_raw += 1
            continue

        raw = raw_by_key[key]
        if not _passes_filter(rec, args, raw):
            continue

        merged = _merge(raw, rec)
        output.append(merged)

    _write_jsonl(Path(args.output), output)

    # Summary
    print(f"[PREPARE] scored={len(scored)}  raw={len(raw_records)}")
    print(f"[FILTER]  passed={len(output)}  "
          f"rejected={len(scored) - len(output) - skipped_no_raw - skipped_status}  "
          f"no_raw_match={skipped_no_raw}  bad_status={skipped_status}")
    print(f"[OUTPUT]  {args.output}")


if __name__ == "__main__":
    main()
