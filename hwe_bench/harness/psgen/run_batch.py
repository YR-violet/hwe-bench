#!/usr/bin/env python3
"""Batch-generate and review problem_statement for verified instances.

No Docker required — works purely from artifact files + gh CLI.

Usage:
    # ibex
    python -m hwe_bench.harness.psgen.run_batch generate \
        --org lowRISC --repo ibex \
        --input datasets/pipeline/lowRISC/lowRISC__ibex_s10_verified.jsonl \
        --artifacts-root artifacts/s10_verify/lowRISC__ibex \
        --num-workers 4

    # opentitan
    python -m hwe_bench.harness.psgen.run_batch generate \
        --org lowRISC --repo opentitan \
        --input datasets/pipeline/lowRISC/lowRISC__opentitan_s10_verified.jsonl \
        --artifacts-root artifacts/s10_verify/lowRISC__opentitan \
        --num-workers 4

    # Merge results back into dataset
    python -m hwe_bench.harness.psgen.run_batch merge \
        --org lowRISC --repo opentitan \
        --input datasets/pipeline/lowRISC/lowRISC__opentitan_s10_verified.jsonl \
        --artifacts-root artifacts/s10_verify/lowRISC__opentitan \
        --output datasets/pipeline/lowRISC/lowRISC__opentitan_s11_eval_ready.jsonl
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import subprocess
from pathlib import Path
from typing import Any, Iterable

from hwe_bench.utils.codex_quota import wait_if_quota_exceeded as _wait_if_quota_exceeded

PROMPTS_DIR = Path(__file__).parent / "prompts"


def _read_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)


def _prompt_path(org: str, repo: str, stage: str) -> Path:
    """Return prompt file path: {org}__{repo}_{stage}.md"""
    name = f"{org}__{repo}_{stage}.md"
    path = PROMPTS_DIR / name
    if not path.exists():
        raise FileNotFoundError(
            f"Prompt template not found: {path}\n"
            f"Please create {name} in {PROMPTS_DIR}"
        )
    return path


def _load_prompt(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _safe_str(value: Any) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return str(value)


def _render(template: str, replacements: dict[str, str]) -> str:
    rendered = template
    for key, value in replacements.items():
        rendered = rendered.replace("{" + key + "}", value)
    return rendered


def _valid_ps_json(path: Path) -> bool:
    """Check if a problem_statement JSON file exists, parses, and has non-empty content."""
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return bool(data.get("problem_statement", "").strip())
    except Exception:
        return False


def _run_codex(pr_dir: Path, prompt: str) -> int:
    cmd = [
        "codex", "exec",
        "-p", "normal",
        "--full-auto",
        "--ephemeral",
        "--skip-git-repo-check",
        "-C", str(pr_dir),
        prompt,
    ]
    return subprocess.run(cmd, stdin=subprocess.DEVNULL).returncode


def _run_generate(
    pr: dict[str, Any],
    artifacts_root: Path,
    template: str,
    repo_root: Path,
    force: bool = False,
) -> dict[str, Any]:
    number = int(pr["number"])
    pr_dir = artifacts_root / f"pr-{number}"
    base = pr.get("base") or {}
    base_sha = _safe_str((base.get("sha") if isinstance(base, dict) else ""))

    if not pr_dir.exists():
        return {"number": number, "status": "skip", "reason": "no artifact dir"}

    result_path = pr_dir / "problem_statement.json"
    if not force and _valid_ps_json(result_path):
        return {"number": number, "status": "skip", "reason": "already exists"}

    # Remove stale output before running
    result_path.unlink(missing_ok=True)

    rendered = _render(template, {
        "PR_DIR": str(pr_dir),
        "REPO_ROOT": str(repo_root),
        "ORG": _safe_str(pr.get("org", "")),
        "REPO": _safe_str(pr.get("repo", "")),
        "NUMBER": str(number),
        "BASE_SHA": base_sha,
        "BASE_IMAGE": _safe_str(pr.get("base_image", "")),
    })
    (pr_dir / "prompt_psgen.md").write_text(rendered, encoding="utf-8")

    rc = _run_codex(pr_dir, rendered)

    if _valid_ps_json(result_path):
        return {"number": number, "status": "success", "rc": rc}
    else:
        return {"number": number, "status": "fail", "reason": "no valid output", "rc": rc}


def _run_review(
    pr: dict[str, Any],
    artifacts_root: Path,
    template: str,
    repo_root: Path,
    force: bool = False,
) -> dict[str, Any]:
    number = int(pr["number"])
    pr_dir = artifacts_root / f"pr-{number}"
    base = pr.get("base") or {}
    base_sha = _safe_str((base.get("sha") if isinstance(base, dict) else ""))

    ps_path = pr_dir / "problem_statement.json"
    if not _valid_ps_json(ps_path):
        return {"number": number, "status": "skip", "reason": "no valid problem_statement.json"}

    reviewed_path = pr_dir / "problem_statement_reviewed.json"
    if not force and _valid_ps_json(reviewed_path):
        return {"number": number, "status": "skip", "reason": "already reviewed"}

    # Remove stale output before running
    reviewed_path.unlink(missing_ok=True)

    rendered = _render(template, {
        "PR_DIR": str(pr_dir),
        "REPO_ROOT": str(repo_root),
        "ORG": _safe_str(pr.get("org", "")),
        "REPO": _safe_str(pr.get("repo", "")),
        "NUMBER": str(number),
        "BASE_SHA": base_sha,
        "BASE_IMAGE": _safe_str(pr.get("base_image", "")),
    })
    (pr_dir / "prompt_psreview.md").write_text(rendered, encoding="utf-8")

    rc = _run_codex(pr_dir, rendered)

    if _valid_ps_json(reviewed_path):
        return {"number": number, "status": "success", "rc": rc}
    else:
        return {"number": number, "status": "fail", "reason": "no valid output", "rc": rc}


def _load_verify_report(artifacts_root: Path, org: str, repo: str, number: int) -> dict[str, Any] | None:
    """Load s10 verify report.json for a given PR.

    Returns the report dict if found, None otherwise.
    Path: artifacts_root/pr-{N}/workdir/{org}/{repo}/instances/pr-{N}/report.json
    """
    report_path = (
        artifacts_root / f"pr-{number}" / "workdir" / org / repo
        / "instances" / f"pr-{number}" / "report.json"
    )
    if not report_path.exists():
        return None
    try:
        return json.loads(report_path.read_text(encoding="utf-8"))
    except Exception:
        return None


# Fields to extract from report.json into eval-ready JSONL
_REPORT_FIELDS = [
    "run_result", "test_patch_result", "fix_patch_result",
    "fixed_tests", "p2p_tests", "f2p_tests", "s2p_tests", "n2p_tests",
]


def _run_merge(input_path: Path, artifacts_root: Path, output_path: Path, org: str = "lowRISC", repo: str = "ibex") -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    merged = 0
    merged_report = 0
    missing_report = 0
    total = 0
    skipped = 0

    with output_path.open("w", encoding="utf-8") as out:
        for pr in _read_jsonl(input_path):
            if pr.get("org") != org or pr.get("repo") != repo:
                skipped += 1
                continue
            total += 1
            number = int(pr["number"])

            # Prefer reviewed, fallback to generated
            reviewed = artifacts_root / f"pr-{number}" / "problem_statement_reviewed.json"
            generated = artifacts_root / f"pr-{number}" / "problem_statement.json"

            ps_data = None
            for path in [reviewed, generated]:
                if _valid_ps_json(path):
                    try:
                        ps_data = json.loads(path.read_text(encoding="utf-8"))
                        break
                    except Exception:
                        continue

            if ps_data and ps_data.get("problem_statement", "").strip():
                pr["problem_statement"] = ps_data["problem_statement"]
                merged += 1
            else:
                pr.setdefault("problem_statement", "")

            # Merge s10 verify baseline results from report.json
            report = _load_verify_report(artifacts_root, org, repo, number)
            if report:
                for field in _REPORT_FIELDS:
                    if field in report:
                        pr[field] = report[field]
                merged_report += 1
            else:
                missing_report += 1

            out.write(json.dumps(pr, ensure_ascii=False) + "\n")

    print(f"[MERGE] total={total} merged_ps={merged} merged_report={merged_report} missing_report={missing_report} skipped={skipped} output={output_path}")


def _batch_run(
    input_path: Path,
    artifacts_root: Path,
    runner,
    template: str,
    repo_root: Path,
    num_workers: int,
    only: set[int] | None,
    force: bool,
    org: str = "lowRISC",
    repo: str = "ibex",
) -> None:
    tasks = []
    for pr in _read_jsonl(input_path):
        if pr.get("org") != org or pr.get("repo") != repo:
            continue
        number = int(pr["number"])
        if only and number not in only:
            continue
        tasks.append(pr)

    succeeded = 0
    failed = 0
    skipped = 0
    task_iter = iter(tasks)

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=max(1, num_workers),
        thread_name_prefix="psgen",
    ) as executor:
        in_flight: dict[concurrent.futures.Future, dict] = {}

        for _ in range(min(num_workers, len(tasks))):
            t = next(task_iter, None)
            if t is None:
                break
            _wait_if_quota_exceeded()
            print(f"[RUN] PR #{t['number']}")
            future = executor.submit(runner, t, artifacts_root, template, repo_root, force)
            in_flight[future] = t

        while in_flight:
            done, _ = concurrent.futures.wait(
                in_flight, return_when=concurrent.futures.FIRST_COMPLETED
            )
            for future in done:
                task = in_flight.pop(future)
                try:
                    result = future.result()
                except Exception as e:
                    result = {"number": task["number"], "status": "error", "reason": str(e)}

                status = result.get("status", "error")
                if status == "success":
                    succeeded += 1
                    print(f"[OK] PR #{result['number']}")
                elif status == "skip":
                    skipped += 1
                    print(f"[SKIP] PR #{result['number']}: {result.get('reason', '')}")
                else:
                    failed += 1
                    print(f"[FAIL] PR #{result['number']}: {result.get('reason', '')}")

            for _ in range(len(done)):
                t = next(task_iter, None)
                if t is None:
                    break
                _wait_if_quota_exceeded()
                print(f"[RUN] PR #{t['number']}")
                future = executor.submit(runner, t, artifacts_root, template, repo_root, force)
                in_flight[future] = t

    print(f"[SUMMARY] success={succeeded} failed={failed} skipped={skipped}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Generate/review problem_statement for verified instances.")
    ap.add_argument("stage", choices=["generate", "review", "merge", "all"])
    ap.add_argument("--org", default="lowRISC", help="GitHub org (default: lowRISC)")
    ap.add_argument("--repo", default="ibex", help="GitHub repo (default: ibex)")
    ap.add_argument("--input", required=True, help="Input JSONL (s10 verified)")
    ap.add_argument(
        "--artifacts-root",
        required=True,
        help="Artifacts directory relative to project root (for example: artifacts/s10_verify/org__repo)",
    )
    ap.add_argument(
        "--repo-root",
        default=str(Path.cwd()),
        help="Repository root path rendered into prompts (default: current working directory)",
    )
    ap.add_argument("--output", default="", help="Output JSONL (for merge stage)")
    ap.add_argument("--num-workers", type=int, default=4)
    ap.add_argument("--only", default="", help="Comma-separated PR numbers")
    ap.add_argument("--force", action="store_true", help="Re-run even if output exists")
    args = ap.parse_args()

    input_path = Path(args.input).resolve()
    artifacts_root = Path(args.artifacts_root).resolve()
    repo_root = Path(args.repo_root).resolve()
    only = {int(x.strip()) for x in args.only.split(",") if x.strip()} if args.only else None

    if args.stage in ("generate", "all"):
        print(f"\n=== Stage: generate ({args.org}/{args.repo}) ===")
        template = _load_prompt(_prompt_path(args.org, args.repo, "generate"))
        _batch_run(input_path, artifacts_root, _run_generate, template, repo_root, args.num_workers, only, args.force, args.org, args.repo)

    if args.stage in ("review", "all"):
        print(f"\n=== Stage: review ({args.org}/{args.repo}) ===")
        template = _load_prompt(_prompt_path(args.org, args.repo, "review"))
        _batch_run(input_path, artifacts_root, _run_review, template, repo_root, args.num_workers, only, args.force, args.org, args.repo)

    if args.stage in ("merge", "all"):
        print(f"\n=== Stage: merge ({args.org}/{args.repo}) ===")
        output = args.output or str(input_path).replace("s10_verified", "s11_eval_ready")
        _run_merge(input_path, artifacts_root, Path(output).resolve(), args.org, args.repo)


if __name__ == "__main__":
    main()
