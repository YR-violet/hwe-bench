#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import logging
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
from hwe_bench.harness.base import Base, Config, Image, PullRequest
from hwe_bench.harness.repos.chisel.rocketchip.rocketchip import RocketChipImageBase
from hwe_bench.harness.repos.chisel.xiangshan.xiangshan import XiangShanImageBase
from hwe_bench.harness.repos.verilog.caliptra.caliptra import CaliptraImageBase
from hwe_bench.harness.repos.verilog.cva6.cva6 import Cva6ImageBase
from hwe_bench.harness.repos.verilog.ibex.ibex import IbexImageBase
from hwe_bench.harness.repos.verilog.opentitan.opentitan import OpenTitanImageBase
from hwe_bench.utils import docker_util

BASE_IMAGE_CONTEXT_DIR = "_base_image_ctx"


def _logger() -> logging.Logger:
    logger = logging.getLogger("tbgen.run_batch")
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger


def _default_prompt_template(org: str, repo: str) -> str:
    mapping = {
        ("chipsalliance", "caliptra-rtl"): "hwe_bench/harness/tbgen/prompts/chipsalliance__caliptra-rtl.md",
        ("chipsalliance", "rocket-chip"): "hwe_bench/harness/tbgen/prompts/chipsalliance__rocket-chip.md",
        ("OpenXiangShan", "XiangShan"): "hwe_bench/harness/tbgen/prompts/OpenXiangShan__XiangShan.md",
        ("openhwgroup", "cva6"): "hwe_bench/harness/tbgen/prompts/openhwgroup__cva6.md",
        ("lowRISC", "ibex"): "hwe_bench/harness/tbgen/prompts/lowRISC__ibex.md",
        ("lowRISC", "opentitan"): "hwe_bench/harness/tbgen/prompts/lowRISC__opentitan.md",
    }
    try:
        return mapping[(org, repo)]
    except KeyError as exc:
        raise ValueError(f"Unsupported repo: {org}/{repo}") from exc


def _make_base_image(org: str, repo: str) -> Image:
    pr = PullRequest(
        org=org,
        repo=repo,
        number=0,
        state="open",
        title="",
        body="",
        base=Base(label="", ref="", sha=""),
        resolved_issues=[],
        fix_patch="",
        test_patch="",
    )
    config = Config(global_env=None, clear_env=True)
    if org == "chipsalliance" and repo == "caliptra-rtl":
        return CaliptraImageBase(pr, config)
    if org == "chipsalliance" and repo == "rocket-chip":
        return RocketChipImageBase(pr, config)
    if org == "OpenXiangShan" and repo == "XiangShan":
        return XiangShanImageBase(pr, config)
    if org == "openhwgroup" and repo == "cva6":
        return Cva6ImageBase(pr, config)
    if org == "lowRISC" and repo == "ibex":
        return IbexImageBase(pr, config)
    if org == "lowRISC" and repo == "opentitan":
        return OpenTitanImageBase(pr, config)
    raise ValueError(f"Unsupported repo: {org}/{repo}")


def _ensure_base_image(base_image: Image, out_root: Path) -> str:
    image_full_name = base_image.image_full_name()
    if docker_util.exists(image_full_name):
        _logger().info("Base image already exists: %s", image_full_name)
        return image_full_name

    context_dir = out_root / BASE_IMAGE_CONTEXT_DIR
    context_dir.mkdir(parents=True, exist_ok=True)

    dockerfile_path = context_dir / base_image.dockerfile_name()
    dockerfile_path.write_text(base_image.dockerfile(), encoding="utf-8")

    for file in base_image.files():
        file_path = context_dir / file.dir / file.name
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(file.content, encoding="utf-8")

    _logger().info(
        "Building shared base image once for this batch: %s", image_full_name
    )
    docker_util.build(
        context_dir,
        base_image.dockerfile_name(),
        image_full_name,
        _logger(),
    )
    return image_full_name


def _prepare_task(
    pr: dict[str, Any],
    *,
    args: argparse.Namespace,
    out_root: Path,
    repo_root: Path,
    template: str,
    base_image_full_name: str,
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
        "resolved_issues": pr.get("resolved_issues", []),
        "base_image": base_image_full_name,
        "paths": {
            "pr_dir": str(pr_dir),
            "fix_patch": str((pr_dir / "fix.patch").resolve()),
            "result_json": str(result_path.resolve()),
            "tb_script": str((pr_dir / "tb_script.sh").resolve()),
            "prepare_script": str((pr_dir / "prepare_script.sh").resolve()),
            "logs_dir": str((pr_dir / "logs").resolve()),
        },
    }
    _write_text(
        pr_dir / "pr_meta.json",
        json.dumps(pr_meta, ensure_ascii=False, indent=2),
    )
    _write_text(pr_dir / "fix.patch", _safe_str(pr.get("fix_patch", "")))

    rendered_prompt = _render_prompt(
        template,
        {
            "PR_DIR": str(pr_dir),
            "REPO_ROOT": str(repo_root),
            "ORG": args.org,
            "REPO": args.repo,
            "NUMBER": str(number),
            "BASE_SHA": base_sha,
            "BASE_IMAGE": base_image_full_name,
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

    status = result.get("status")
    if status == "success":
        tb_script = _safe_str(result.get("tb_script", ""))
        prepare_script = _safe_str(result.get("prepare_script", ""))
        if tb_script and not (pr_dir / "tb_script.sh").exists():
            _write_text(pr_dir / "tb_script.sh", tb_script)
        if prepare_script and not (pr_dir / "prepare_script.sh").exists():
            _write_text(pr_dir / "prepare_script.sh", prepare_script)
        warnings.append("success")
        return "success", "; ".join(warnings)

    stage = ""
    failure = result.get("failure")
    if isinstance(failure, dict):
        stage = _safe_str(failure.get("stage", ""))
    warnings.append(f"status={status!r} stage={stage!r}")
    return "failed", "; ".join(warnings)


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Batch-run codex exec per PR to generate tb_script."
    )
    ap.add_argument("--input", required=True, help="Input PR jsonl path")
    ap.add_argument("--org", required=True, help="GitHub org, e.g. lowRISC")
    ap.add_argument("--repo", required=True, help="GitHub repo, e.g. ibex")
    ap.add_argument(
        "--prompt-template",
        default="hwe_bench/harness/tbgen/prompts/lowRISC__ibex.md",
        help="Prompt template markdown path",
    )
    ap.add_argument(
        "--out-root",
        default="artifacts/s09_tbgen",
        help="Artifacts root directory relative to project root",
    )
    ap.add_argument(
        "--repo-root",
        default=str(Path.cwd()),
        help="Repo root directory passed to prompt rendering",
    )
    ap.add_argument(
        "--force", action="store_true", help="Re-run even if already succeeded"
    )
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
    if args.prompt_template == ap.get_default("prompt_template"):
        args.prompt_template = _default_prompt_template(args.org, args.repo)

    input_path = Path(args.input).resolve()
    repo_root = Path(args.repo_root).resolve()
    out_root = Path(args.out_root).resolve() / f"{args.org}__{args.repo}"
    out_root.mkdir(parents=True, exist_ok=True)

    base_image_full_name = _ensure_base_image(
        _make_base_image(args.org, args.repo),
        out_root,
    )

    template = _load_prompt_template(Path(args.prompt_template))
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
            template=template,
            base_image_full_name=base_image_full_name,
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
        label="tbgen",
        initial_skipped=skipped,
        summary_extra={"artifacts": out_root},
    )


if __name__ == "__main__":
    main()
