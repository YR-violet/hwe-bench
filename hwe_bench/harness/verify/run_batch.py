#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from hwe_bench.harness.codex_batch import (
    _load_prompt_template,
    _parse_only_numbers,
    _read_jsonl,
    _render_prompt,
    _safe_str,
    _write_text,
    run_codex_batch,
    run_codex_exec,
)


def _prepare_task(
    pr: dict[str, Any],
    *,
    args: argparse.Namespace,
    out_root: Path,
    repo_root: Path,
    template: str,
    run_case_script: Path,
) -> dict[str, Any] | None:
    number = int(pr["number"])
    pr_dir = out_root / f"pr-{number}"
    pr_dir.mkdir(parents=True, exist_ok=True)
    (pr_dir / "logs").mkdir(exist_ok=True)

    result_path = pr_dir / "result.json"
    if not args.force and result_path.exists():
        try:
            existing = json.loads(result_path.read_text(encoding="utf-8"))
            if existing.get("status") == "success":
                return None
        except Exception:
            pass

    base = pr.get("base") or {}
    base_sha = _safe_str((base.get("sha") if isinstance(base, dict) else ""))
    pr_meta = {
        "org": pr.get("org", args.org),
        "repo": pr.get("repo", args.repo),
        "number": number,
        "base": base,
        "base_sha": base_sha,
        "title": pr.get("title", ""),
        "body": pr.get("body", ""),
        "paths": {
            "pr_dir": str(pr_dir),
            "case_json": str((pr_dir / "case.json").resolve()),
            "tb_script": str((pr_dir / "tb_script.sh").resolve()),
            "prepare_script": str((pr_dir / "prepare_script.sh").resolve()),
            "fix_patch": str((pr_dir / "fix.patch").resolve()),
            "test_patch": str((pr_dir / "test.patch").resolve()),
            "result_json": str(result_path.resolve()),
            "run_case": str(run_case_script.resolve()),
        },
    }

    _write_text(pr_dir / "case.json", json.dumps(pr, ensure_ascii=False, indent=2))
    _write_text(pr_dir / "pr_meta.json", json.dumps(pr_meta, ensure_ascii=False, indent=2))
    _write_text(pr_dir / "fix.patch", _safe_str(pr.get("fix_patch", "")))
    _write_text(pr_dir / "test.patch", _safe_str(pr.get("test_patch", "")))

    tb_script_path = pr_dir / "tb_script.sh"
    if args.force or not tb_script_path.exists():
        _write_text(tb_script_path, _safe_str(pr.get("tb_script", "")))

    prepare_script_path = pr_dir / "prepare_script.sh"
    if args.force or not prepare_script_path.exists():
        _write_text(prepare_script_path, _safe_str(pr.get("prepare_script", "")))

    rendered_prompt = _render_prompt(
        template,
        {
            "PR_DIR": str(pr_dir),
            "REPO_ROOT": str(repo_root),
            "ORG": args.org,
            "REPO": args.repo,
            "NUMBER": str(number),
            "BASE_SHA": base_sha,
            "BASE_IMAGE": _safe_str(pr.get("base_image", "")),
            "RUN_CASE": str(run_case_script.resolve()),
            "PYTHON_BIN": sys.executable,
        },
    )
    _write_text(pr_dir / "prompt_rendered.md", rendered_prompt)

    return {
        "org": args.org,
        "repo": args.repo,
        "number": number,
        "base_sha": base_sha,
        "workdir": pr_dir,
        "pr_dir": pr_dir,
        "result_path": result_path,
        "prompt": rendered_prompt,
    }


def _sync_scripts_from_result(pr_dir: Path, result: dict[str, Any]) -> None:
    tb_script = _safe_str(result.get("tb_script", ""))
    prepare_script = _safe_str(result.get("prepare_script", ""))
    if tb_script:
        _write_text(pr_dir / "tb_script.sh", tb_script)
    if prepare_script:
        _write_text(pr_dir / "prepare_script.sh", prepare_script)


def _check_task_result(task: dict[str, Any], run_result: int) -> tuple[str, str]:
    warnings: list[str] = []
    if run_result != 0:
        warnings.append(
            f"codex exec exited with code {run_result}; judging by result.json"
        )

    result_path = task["result_path"]
    pr_dir = task["pr_dir"]
    if not result_path.exists():
        warnings.append(f"missing result.json: {result_path}")
        return "failed", "; ".join(warnings)

    try:
        result = json.loads(result_path.read_text(encoding="utf-8"))
    except Exception as e:
        warnings.append(f"invalid JSON in result.json: {e}")
        return "failed", "; ".join(warnings)

    _sync_scripts_from_result(pr_dir, result)
    status = result.get("status")
    if status == "success":
        warnings.append("success")
        return "success", "; ".join(warnings)

    stage = ""
    failure = result.get("failure")
    if isinstance(failure, dict):
        stage = _safe_str(failure.get("stage", ""))
    warnings.append(f"status={status!r} stage={stage!r}")
    return "failed", "; ".join(warnings)


def main() -> None:
    ap = argparse.ArgumentParser(description="Batch-run codex verify/repair per PR.")
    ap.add_argument("--input", required=True, help="Input s09 verify candidate jsonl path")
    ap.add_argument("--org", required=True, help="GitHub org, e.g. lowRISC")
    ap.add_argument("--repo", required=True, help="GitHub repo, e.g. ibex")
    ap.add_argument(
        "--prompt-template",
        default="",
        help="Prompt template markdown path (default: auto-select based on --org/--repo)",
    )
    ap.add_argument(
        "--out-root",
        default="artifacts/s10_verify",
        help="Artifacts root directory relative to project root",
    )
    ap.add_argument(
        "--repo-root",
        default=str(Path.cwd()),
        help="Repo root directory passed to prompt rendering",
    )
    ap.add_argument("--force", action="store_true", help="Re-run even if already succeeded")
    ap.add_argument(
        "--only",
        default="",
        help="Comma-separated PR numbers to run, e.g. 2261,1446",
    )
    ap.add_argument(
        "--num-workers",
        type=int,
        default=1,
        help="Number of concurrent codex exec workers (default: 1).",
    )
    args = ap.parse_args()

    input_path = Path(args.input).resolve()
    repo_root = Path(args.repo_root).resolve()
    out_root = Path(args.out_root).resolve() / f"{args.org}__{args.repo}"
    out_root.mkdir(parents=True, exist_ok=True)

    prompt_path = args.prompt_template
    if not prompt_path:
        prompt_path = f"hwe_bench/harness/verify/prompts/{args.org}__{args.repo}.md"
    prompt_template = _load_prompt_template(Path(prompt_path))
    run_case_script = repo_root / "hwe_bench" / "harness" / "verify" / "run_case.py"
    only_numbers = _parse_only_numbers(args.only) if args.only else None

    skipped = 0
    tasks_to_run: list[dict[str, Any]] = []

    for pr in _read_jsonl(input_path):
        if pr.get("org") != args.org or pr.get("repo") != args.repo:
            continue

        number = int(pr["number"])
        if only_numbers is not None and number not in only_numbers:
            continue

        task = _prepare_task(
            pr,
            args=args,
            out_root=out_root,
            repo_root=repo_root,
            template=prompt_template,
            run_case_script=run_case_script,
        )
        if task is None:
            skipped += 1
            continue
        tasks_to_run.append(task)

    run_codex_batch(
        tasks_to_run,
        run_codex_exec,
        _check_task_result,
        num_workers=args.num_workers,
        label="verify",
        initial_skipped=skipped,
        summary_extra={"artifacts": out_root},
    )


if __name__ == "__main__":
    main()
