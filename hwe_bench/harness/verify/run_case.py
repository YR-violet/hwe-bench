#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any

from hwe_bench.harness.base import (
    BUILD_IMAGE_LOG_FILE,
    BUILD_IMAGE_WORKDIR,
    FIX_PATCH_RUN_LOG_FILE,
    INSTANCE_WORKDIR,
    REPORT_FILE,
    RUN_LOG_FILE,
    TEST_PATCH_RUN_LOG_FILE,
)
from hwe_bench.harness.docker_runner import CliArgs as BuildCliArgs


def _read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _read_text_if_exists(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def _build_case_record(case: dict[str, Any], case_dir: Path) -> dict[str, Any]:
    base = case.get("base") or {}
    return {
        "org": case.get("org", ""),
        "repo": case.get("repo", ""),
        "number": int(case.get("number", 0)),
        "state": case.get("state", "") or "",
        "title": case.get("title", "") or "",
        "body": case.get("body", "") or "",
        "base": {
            "label": base.get("label", "") or "",
            "ref": base.get("ref", "") or "",
            "sha": base.get("sha", "") or "",
        },
        # Resolved issues are not needed by the verify harness at verify time.
        "resolved_issues": [],
        "fix_patch": _read_text_if_exists(case_dir / "fix.patch") or case.get("fix_patch", "") or "",
        "test_patch": _read_text_if_exists(case_dir / "test.patch") or case.get("test_patch", "") or "",
        "prepare_script": _read_text_if_exists(case_dir / "prepare_script.sh")
        or case.get("prepare_script", "")
        or "",
        "tb_script": _read_text_if_exists(case_dir / "tb_script.sh") or case.get("tb_script", "") or "",
        "tag": case.get("tag", "") or "",
        "number_interval": case.get("number_interval", "") or "",
        "lang": case.get("lang", "") or "",
    }


def _write_case_jsonl(case_dir: Path, record: dict[str, Any]) -> Path:
    case_jsonl = case_dir / "case.jsonl"
    with case_jsonl.open("w", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False))
        f.write("\n")
    return case_jsonl


def _build_cli(case_jsonl: Path, workdir: Path, logs_dir: Path, force_build: bool) -> BuildCliArgs:
    return BuildCliArgs.from_dict(
        {
            "mode": "instance",
            "workdir": workdir,
            "raw_dataset_files": [str(case_jsonl)],
            "force_build": force_build,
            "output_dir": None,
            "specifics": None,
            "skips": None,
            "global_env": None,
            "clear_env": True,
            "stop_on_error": False,
            "max_workers": 1,
            "max_workers_build_image": 1,
            "max_workers_run_instance": 1,
            "run_cmd": "",
            "test_patch_run_cmd": "",
            "fix_patch_run_cmd": "",
            "log_dir": logs_dir,
            "log_level": "INFO",
            "log_to_console": False,
            "parse_log": True,
            "run_log": True,
        }
    )


def _remove_previous_instance_dir(workdir: Path, record: dict[str, Any]) -> None:
    instance_dir = (
        workdir
        / record["org"]
        / record["repo"]
        / INSTANCE_WORKDIR
        / f"pr-{record['number']}"
    )
    if instance_dir.exists():
        shutil.rmtree(instance_dir)


def _summary_paths(workdir: Path, record: dict[str, Any]) -> dict[str, str]:
    image_dir = (
        workdir
        / record["org"]
        / record["repo"]
        / BUILD_IMAGE_WORKDIR
        / f"pr-{record['number']}"
    )
    instance_dir = (
        workdir
        / record["org"]
        / record["repo"]
        / INSTANCE_WORKDIR
        / f"pr-{record['number']}"
    )
    return {
        "image_dir": str(image_dir),
        "build_image_log": str(image_dir / BUILD_IMAGE_LOG_FILE),
        "instance_dir": str(instance_dir),
        "run_log": str(instance_dir / RUN_LOG_FILE),
        "test_patch_run_log": str(instance_dir / TEST_PATCH_RUN_LOG_FILE),
        "fix_patch_run_log": str(instance_dir / FIX_PATCH_RUN_LOG_FILE),
        "report_path": str(instance_dir / REPORT_FILE),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Run standardized verify flow for a single case directory.")
    ap.add_argument("--case-dir", required=True, help="Per-case directory prepared by s10 run_batch.py")
    ap.add_argument("--force-build", action="store_true", help="Force rebuild the per-PR image")
    args = ap.parse_args()

    case_dir = Path(args.case_dir).resolve()
    case_json = case_dir / "case.json"
    if not case_json.exists():
        print(f"[ERROR] Missing case.json: {case_json}", file=sys.stderr)
        return 2
    workdir = case_dir / "workdir"
    logs_dir = case_dir / "logs"
    workdir.mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)

    original_case = _read_json(case_json)
    record = _build_case_record(original_case, case_dir)
    case_jsonl = _write_case_jsonl(case_dir, record)

    _remove_previous_instance_dir(workdir, record)

    cli = _build_cli(case_jsonl, workdir, logs_dir, args.force_build)

    run_error: str | None = None
    try:
        cli.run_mode_instance()
    except SystemExit as e:
        run_error = f"SystemExit({e.code})"
    except Exception as e:  # pragma: no cover - best-effort wrapper
        run_error = str(e)

    paths = _summary_paths(workdir, record)
    summary: dict[str, Any] = {
        "org": record["org"],
        "repo": record["repo"],
        "number": record["number"],
        "force_build": args.force_build,
        "paths": paths,
    }

    report_path = Path(paths["report_path"])
    if report_path.exists():
        try:
            report = _read_json(report_path)
        except Exception as e:
            summary["status"] = "failure"
            summary["error"] = f"invalid report.json: {e}"
            print(json.dumps(summary, ensure_ascii=False, indent=2))
            return 1

        summary["status"] = "success" if report.get("valid") else "failure"
        summary["report_valid"] = bool(report.get("valid"))
        summary["report_error_msg"] = report.get("error_msg", "")
        if run_error:
            summary["run_error"] = run_error
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0 if report.get("valid") else 1

    summary["status"] = "failure"
    summary["error"] = run_error or "report.json not found"
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
