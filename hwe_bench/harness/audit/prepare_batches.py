#!/usr/bin/env python3
"""Prepare audit batch artifacts: collect raw files and split into batches.

All semantic analysis is done by the LLM auditor (via codex exec).
This script only handles file collection, status determination, and batch splitting.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
from collections import Counter
from pathlib import Path
from typing import Any, Iterable


def _read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _read_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)


def _write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content or "", encoding="utf-8")


def _safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()) or "unknown"


def _parse_only(raw: str) -> set[int]:
    out: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if part:
            out.add(int(part))
    return out


def _copy_if_exists(src: Path, dst: Path) -> bool:
    if not src.exists():
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return True


def _report_numbers_from_ids(ids: list[str]) -> set[int]:
    numbers: set[int] = set()
    for item in ids:
        m = re.search(r"pr-(\d+)$", item)
        if m:
            numbers.add(int(m.group(1)))
    return numbers


def _load_final_report(path: Path | None) -> dict[str, Any]:
    if path is None or not path.exists():
        return {}
    return _read_json(path)


def _status_map_from_final_report(final_report: dict[str, Any]) -> dict[int, str]:
    status_map: dict[int, str] = {}
    for key, status in [
        ("resolved_ids", "resolved"),
        ("unresolved_ids", "unresolved"),
        ("incomplete_ids", "incomplete"),
        ("error_ids", "error"),
        ("empty_patch_ids", "empty_patch"),
    ]:
        for number in _report_numbers_from_ids(final_report.get(key, []) or []):
            status_map[number] = status
    return status_map


def _collect_trajectory_paths(job_dirs: list[Path]) -> dict[int, Path]:
    out: dict[int, Path] = {}
    for job_dir in job_dirs:
        for path in job_dir.glob("*/agent/trajectory.json"):
            m = re.search(r"pr-(\d+)", str(path))
            if not m:
                continue
            out[int(m.group(1))] = path
    return out


def _collect_eval_paths(eval_root: Path, org: str, repo: str) -> tuple[dict[int, Path], dict[int, Path]]:
    evals_dir = eval_root / org / repo / "evals"
    report_paths: dict[int, Path] = {}
    log_paths: dict[int, Path] = {}
    if not evals_dir.exists():
        return report_paths, log_paths
    for report_path in evals_dir.glob("pr-*/report.json"):
        m = re.search(r"pr-(\d+)", str(report_path))
        if m:
            report_paths[int(m.group(1))] = report_path
    for log_path in evals_dir.glob("pr-*/fix-patch-run.log"):
        m = re.search(r"pr-(\d+)", str(log_path))
        if m:
            log_paths[int(m.group(1))] = log_path
    return report_paths, log_paths


def _determine_status(
    number: int,
    *,
    patch_present: bool,
    report_path: Path | None,
    final_status_map: dict[int, str],
) -> str:
    if number in final_status_map:
        return final_status_map[number]
    if not patch_present:
        return "empty_patch"
    if report_path and report_path.exists():
        try:
            report = _read_json(report_path)
            return "resolved" if report.get("valid") else "unresolved"
        except Exception:
            return "incomplete"
    return "incomplete"


def prepare_batches(
    *,
    dataset_path: Path,
    patches_path: Path,
    job_dirs: list[Path],
    eval_root: Path,
    out_root: Path,
    org: str,
    repo: str,
    agent: str,
    dataset_name: str,
    batch_size: int,
    only: set[int] | None = None,
    final_report_path: Path | None = None,
) -> Path:
    run_root = out_root / f"{org}__{repo}" / f"{_safe_name(agent)}__{_safe_name(dataset_name)}"
    cases_root = run_root / "cases"
    batches_root = run_root / "batches"
    cases_root.mkdir(parents=True, exist_ok=True)
    batches_root.mkdir(parents=True, exist_ok=True)

    # Load data sources
    dataset_rows = {
        int(row["number"]): row
        for row in _read_jsonl(dataset_path)
        if row.get("org") == org and row.get("repo") == repo
    }
    patches = {
        int(row["number"]): row.get("fix_patch", "")
        for row in _read_jsonl(patches_path)
        if row.get("org") == org and row.get("repo") == repo
    }
    trajectory_paths = _collect_trajectory_paths(job_dirs)
    report_paths, fix_log_paths = _collect_eval_paths(eval_root, org, repo)
    # Final report: use explicit path if provided, else legacy eval_root/output/final_report.json
    resolved_final_report_path = final_report_path
    if resolved_final_report_path is None:
        legacy_path = eval_root / "output" / "final_report.json"
        if legacy_path.exists():
            resolved_final_report_path = legacy_path
    final_report = _load_final_report(resolved_final_report_path)
    final_status_map = _status_map_from_final_report(final_report)

    # Determine case set
    observed_numbers = set(trajectory_paths) | set(patches) | set(report_paths) | set(fix_log_paths) | set(final_status_map)
    if only is not None:
        observed_numbers &= only
    case_numbers = sorted(number for number in observed_numbers if number in dataset_rows)

    # Save run metadata
    _write_json(run_root / "run_meta.json", {
        "org": org,
        "repo": repo,
        "agent": agent,
        "dataset": dataset_name,
        "batch_size": batch_size,
        "case_count": len(case_numbers),
    })

    # Collect raw files for each case
    prepared_cases: list[dict[str, Any]] = []

    for number in case_numbers:
        row = dataset_rows[number]
        case_dir = cases_root / f"pr-{number}"
        case_dir.mkdir(parents=True, exist_ok=True)

        agent_patch = patches.get(number, "")
        golden_patch = str(row.get("fix_patch", "") or "")
        tb_script = str(row.get("tb_script", "") or "")
        problem_statement = str(row.get("problem_statement", "") or "")

        status = _determine_status(
            number,
            patch_present=bool(agent_patch.strip()),
            report_path=report_paths.get(number),
            final_status_map=final_status_map,
        )

        # Write raw files
        _write_text(case_dir / "agent_patch.diff", agent_patch)
        _write_text(case_dir / "golden_patch.diff", golden_patch)
        _write_text(case_dir / "tb_script.sh", tb_script)
        _write_text(case_dir / "problem_statement.md", problem_statement)
        if number in report_paths:
            _copy_if_exists(report_paths[number], case_dir / "report.json")
        if number in fix_log_paths:
            _copy_if_exists(fix_log_paths[number], case_dir / "fix-patch-run.log")
        if number in trajectory_paths:
            _copy_if_exists(trajectory_paths[number], case_dir / "trajectory.json")

        # Write case metadata (commit SHAs for future leakage detection)
        case_meta = {"number": number, "status": status}
        merge_sha = row.get("merge_commit_sha")
        if merge_sha:
            case_meta["merge_commit_sha"] = str(merge_sha)
        commits = row.get("commits")
        if commits:
            case_meta["commit_shas"] = [str(c.get("sha", "")) for c in commits if c.get("sha")]
        _write_json(case_dir / "case_meta.json", case_meta)

        prepared_cases.append({"number": number, "status": status, "case_dir": str(case_dir.resolve())})

    # Split into batches
    num_batches = math.ceil(len(prepared_cases) / batch_size) if prepared_cases else 0
    batch_list: list[dict[str, Any]] = []

    for batch_index in range(num_batches):
        batch_cases = prepared_cases[batch_index * batch_size : (batch_index + 1) * batch_size]
        batch_id = f"batch-{batch_index:03d}"
        batch_dir = batches_root / batch_id
        batch_dir.mkdir(parents=True, exist_ok=True)

        manifest_cases = []
        for case in batch_cases:
            case_dir = Path(case["case_dir"])
            case_rel = os.path.relpath(case_dir, batch_dir.resolve())
            manifest_cases.append({
                "number": case["number"],
                "status": case["status"],
                "case_dir": case_rel,
            })

        manifest = {
            "batch_id": batch_id,
            "org": org,
            "repo": repo,
            "agent": agent,
            "dataset": dataset_name,
            "case_count": len(manifest_cases),
            "status_counts": dict(Counter(c["status"] for c in manifest_cases)),
            "cases": manifest_cases,
        }
        _write_json(batch_dir / "batch_manifest.json", manifest)
        batch_list.append({"batch_id": batch_id, "case_count": len(manifest_cases)})

    _write_json(run_root / "prepared_batches.json", {
        "case_count": len(prepared_cases),
        "batch_count": len(batch_list),
        "batches": batch_list,
    })

    print(f"Prepared {len(prepared_cases)} cases in {len(batch_list)} batches -> {run_root}")
    return run_root


def main() -> None:
    parser = argparse.ArgumentParser(description="Prepare batch artifacts for HWE-bench audit.")
    parser.add_argument("--dataset", required=True, help="Dataset JSONL path")
    parser.add_argument("--patches", required=True, help="Agent patches.jsonl path")
    parser.add_argument("--jobs", nargs="+", required=True, help="Harbor job directories")
    parser.add_argument("--eval-root", required=True, help="run_evaluation output root (contains <org>/<repo>/evals/pr-N/report.json + fix-patch-run.log)")
    parser.add_argument(
        "--final-report",
        default="",
        help="Optional explicit path to aggregate final_report.json. "
             "If not provided, falls back to <eval-root>/output/final_report.json.",
    )
    parser.add_argument("--org", required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--agent", required=True, help="Agent label, e.g. claude-sonnet-4.6")
    parser.add_argument("--dataset-name", default="")
    parser.add_argument("--batch-size", type=int, default=20)
    parser.add_argument(
        "--out-root",
        default="artifacts/audit",
        help="Artifacts root directory relative to project root",
    )
    parser.add_argument("--only", default="", help="Comma-separated PR numbers")
    args = parser.parse_args()

    prepare_batches(
        dataset_path=Path(args.dataset).resolve(),
        patches_path=Path(args.patches).resolve(),
        job_dirs=[Path(p).resolve() for p in args.jobs],
        eval_root=Path(args.eval_root).resolve(),
        out_root=Path(args.out_root).resolve(),
        org=args.org,
        repo=args.repo,
        agent=args.agent,
        dataset_name=args.dataset_name or Path(args.dataset).stem,
        batch_size=max(1, args.batch_size),
        only=_parse_only(args.only) if args.only else None,
        final_report_path=Path(args.final_report).resolve() if args.final_report else None,
    )


if __name__ == "__main__":
    main()
