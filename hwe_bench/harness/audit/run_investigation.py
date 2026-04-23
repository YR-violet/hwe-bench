#!/usr/bin/env python3

from __future__ import annotations

import argparse
import concurrent.futures
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from hwe_bench.utils.codex_quota import wait_if_quota_exceeded as _wait_if_quota_exceeded

PROMPTS_DIR = Path(__file__).parent / "prompts"
PROMPT_PATHS = {
    "investigate": PROMPTS_DIR / "fine_investigation.md",
    "fix": PROMPTS_DIR / "fix_tb_script.md",
    "review": PROMPTS_DIR / "fix_review.md",
}
OUTPUT_FILES = {
    "investigate": "detailed_verdict.json",
    "fix": "fix_report.json",
    "review": "review_result.json",
}
PROMPT_OUTPUT_FILES = {
    "investigate": "prompt_investigate.md",
    "fix": "prompt_fix.md",
    "review": "prompt_review.md",
}
REVIEW_VERDICTS = {"approve", "reject", "conditional_approve"}


def _read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content or "", encoding="utf-8")


def _load_prompt_template(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _render_prompt(template: str, replacements: dict[str, str]) -> str:
    rendered = template
    for key, value in replacements.items():
        rendered = rendered.replace("{" + key + "}", value)
    return rendered


def _parse_only(raw: str) -> set[int]:
    only: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if part:
            only.add(int(part))
    return only


def _resolve_bundle_root(path: Path) -> Path:
    if any(child.is_dir() and child.name.startswith("pr-") for child in path.iterdir()):
        return path

    candidates = [
        child
        for child in path.iterdir()
        if child.is_dir() and any(grandchild.is_dir() and grandchild.name.startswith("pr-") for grandchild in child.iterdir())
    ]
    if len(candidates) == 1:
        return candidates[0]
    raise ValueError(f"Could not resolve bundle root from {path}")


def _case_number(case_dir: Path, manifest: dict[str, Any]) -> int:
    if "number" in manifest:
        return int(manifest["number"])
    return int(case_dir.name.removeprefix("pr-"))


def _load_case_manifest(case_dir: Path) -> dict[str, Any]:
    return _read_json(case_dir / "case_manifest.json")


def _ensure_case_runtime_inputs(case_dir: Path, manifest: dict[str, Any]) -> None:
    case_record = manifest.get("case_record") or {}
    if not case_record:
        return

    tb_script = str(case_record.get("tb_script", "") or "")
    fix_patch = str(case_record.get("fix_patch", "") or "")
    test_patch = str(case_record.get("test_patch", "") or "")
    prepare_script = str(case_record.get("prepare_script", "") or "")

    _write_json(case_dir / "case.json", case_record)
    _write_text(case_dir / "prepare_script.sh", prepare_script)
    _write_text(case_dir / "fix.patch", fix_patch)
    _write_text(case_dir / "test.patch", test_patch)

    tb_script_path = case_dir / "tb_script.sh"
    if not tb_script_path.exists() or not tb_script_path.read_text(encoding="utf-8").strip():
        _write_text(tb_script_path, tb_script)

    golden_patch_path = case_dir / "golden_patch.diff"
    if golden_patch_path.exists():
        _write_text(case_dir / "fix.patch", golden_patch_path.read_text(encoding="utf-8"))


def _stage_output_path(case_dir: Path, stage: str) -> Path:
    return case_dir / OUTPUT_FILES[stage]


def _stage_is_complete(case_dir: Path, stage: str) -> bool:
    output_path = _stage_output_path(case_dir, stage)
    if not output_path.exists():
        return False

    try:
        data = _read_json(output_path)
    except Exception:
        return False

    if stage == "investigate":
        return bool(data.get("fixability")) and bool(data.get("final_verdict"))
    if stage == "fix":
        status = data.get("status")
        if status == "fixed":
            return (case_dir / "tb_script_fixed.sh").exists()
        return status == "unfixable"
    if stage == "review":
        return str(data.get("review_verdict", "")) in REVIEW_VERDICTS
    return False


def _cleanup_stage_outputs(case_dir: Path, stage: str) -> None:
    _stage_output_path(case_dir, stage).unlink(missing_ok=True)
    if stage == "fix":
        (case_dir / "tb_script_fixed.sh").unlink(missing_ok=True)


def _stage_eligibility(case_dir: Path, stage: str) -> tuple[bool, str]:
    if stage == "investigate":
        return True, ""

    if stage == "fix":
        detailed_path = case_dir / "detailed_verdict.json"
        if not detailed_path.exists():
            return False, "missing detailed_verdict.json"
        try:
            detailed = _read_json(detailed_path)
        except Exception as exc:
            return False, f"invalid detailed_verdict.json: {exc}"
        verdict = str(detailed.get("final_verdict", ""))
        if verdict == "genuine_fix":
            return False, f"verdict=genuine_fix (no fix needed)"
        fixability = str(detailed.get("fixability", ""))
        if fixability != "case_local_fix":
            return False, f"fixability={fixability or 'missing'}"
        return True, ""

    if stage == "review":
        fix_report_path = case_dir / "fix_report.json"
        if not fix_report_path.exists():
            return False, "missing fix_report.json"
        try:
            fix_report = _read_json(fix_report_path)
        except Exception as exc:
            return False, f"invalid fix_report.json: {exc}"
        status = str(fix_report.get("status", ""))
        if status != "fixed":
            return False, f"fix_status={status or 'missing'}"
        if not (case_dir / "tb_script_fixed.sh").exists():
            return False, "missing tb_script_fixed.sh"
        return True, ""

    raise ValueError(f"Unknown stage: {stage}")


def _prompt_replacements(case_dir: Path, manifest: dict[str, Any], run_case_script: Path) -> dict[str, str]:
    case_record = manifest.get("case_record") or {}
    org = str(manifest.get("org") or case_record.get("org") or "")
    repo = str(manifest.get("repo") or case_record.get("repo") or "")
    number = str(manifest.get("number") or case_record.get("number") or case_dir.name.removeprefix("pr-"))
    docker_image = str(
        manifest.get("docker_image") or f"hwebench/{org.lower()}_m_{repo.lower()}:pr-{number}"
    )

    return {
        "CASE_DIR": str(case_dir.resolve()),
        "ORG": org,
        "REPO": repo,
        "NUMBER": number,
        "DOCKER_IMAGE": docker_image,
        "PYTHON_BIN": sys.executable,
        "RUN_CASE": str(run_case_script.resolve()),
        "TB_SCRIPT_PATH": str((case_dir / "tb_script.sh").resolve()),
        "TB_SCRIPT_FIXED_PATH": str((case_dir / "tb_script_fixed.sh").resolve()),
        "GOLDEN_PATCH_PATH": str((case_dir / "golden_patch.diff").resolve()),
        "CASE_JSON_PATH": str((case_dir / "case.json").resolve()),
        "PREPARE_SCRIPT_PATH": str((case_dir / "prepare_script.sh").resolve()),
        "FIX_PATCH_PATH": str((case_dir / "fix.patch").resolve()),
        "TEST_PATCH_PATH": str((case_dir / "test.patch").resolve()),
    }


def _run_codex_exec(case_dir: Path, prompt: str) -> int:
    cmd = [
        "codex",
        "exec",
        "-p",
        "normal",
        "--full-auto",
        "--ephemeral",
        "--skip-git-repo-check",
        "-C",
        str(case_dir),
        prompt,
    ]
    return subprocess.run(cmd, stdin=subprocess.DEVNULL).returncode


def _prepare_tasks(
    bundle_root: Path,
    *,
    stage: str,
    only: set[int] | None,
    force: bool,
    run_case_script: Path,
) -> tuple[list[dict[str, Any]], int]:
    prompt_template = _load_prompt_template(PROMPT_PATHS[stage])
    tasks: list[dict[str, Any]] = []
    skipped = 0

    for case_dir in sorted(path for path in bundle_root.iterdir() if path.is_dir() and path.name.startswith("pr-")):
        manifest_path = case_dir / "case_manifest.json"
        if not manifest_path.exists():
            skipped += 1
            continue

        manifest = _load_case_manifest(case_dir)
        number = _case_number(case_dir, manifest)
        if only is not None and number not in only:
            continue

        eligible, reason = _stage_eligibility(case_dir, stage)
        if not eligible:
            print(f"[SKIP] PR #{number}: {reason}")
            skipped += 1
            continue

        if not force and _stage_is_complete(case_dir, stage):
            print(f"[SKIP] PR #{number}: existing {OUTPUT_FILES[stage]}")
            skipped += 1
            continue

        _ensure_case_runtime_inputs(case_dir, manifest)
        _cleanup_stage_outputs(case_dir, stage)

        rendered_prompt = _render_prompt(
            prompt_template,
            _prompt_replacements(case_dir, manifest, run_case_script),
        )
        _write_text(case_dir / PROMPT_OUTPUT_FILES[stage], rendered_prompt)

        tasks.append(
            {
                "number": number,
                "case_dir": case_dir,
                "output_path": _stage_output_path(case_dir, stage),
                "prompt": rendered_prompt,
            }
        )

    return tasks, skipped


def _run_stage(
    bundle_root: Path,
    *,
    stage: str,
    only: set[int] | None,
    num_workers: int,
    force: bool,
    run_case_script: Path,
) -> dict[str, int]:
    tasks, skipped = _prepare_tasks(
        bundle_root,
        stage=stage,
        only=only,
        force=force,
        run_case_script=run_case_script,
    )
    succeeded = 0
    failed = 0

    if not tasks:
        print(f"[SUMMARY] stage={stage} success=0 failed=0 skipped={skipped}")
        return {"success": 0, "failed": 0, "skipped": skipped}

    task_iter = iter(tasks)
    max_workers = max(1, num_workers)

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=max_workers,
        thread_name_prefix=f"audit-{stage}",
    ) as executor:
        in_flight: dict[concurrent.futures.Future[int], dict[str, Any]] = {}

        for _ in range(min(max_workers, len(tasks))):
            task = next(task_iter, None)
            if task is None:
                break
            _wait_if_quota_exceeded()
            print(f"[RUN] stage={stage} PR #{task['number']}")
            future = executor.submit(_run_codex_exec, task["case_dir"], task["prompt"])
            in_flight[future] = task

        while in_flight:
            done, _ = concurrent.futures.wait(
                in_flight,
                return_when=concurrent.futures.FIRST_COMPLETED,
            )

            for future in done:
                task = in_flight.pop(future)
                try:
                    rc = future.result()
                except Exception as exc:
                    rc = -1
                    print(f"[WARN] codex exec crashed for PR #{task['number']}: {exc}")

                if rc != 0:
                    print(f"[WARN] codex exec exited with code {rc} for PR #{task['number']}")

                if _stage_is_complete(task["case_dir"], stage):
                    succeeded += 1
                    print(f"[OK] stage={stage} PR #{task['number']}")
                else:
                    failed += 1
                    print(f"[FAIL] stage={stage} PR #{task['number']}: missing or invalid {OUTPUT_FILES[stage]}")

            for _ in range(len(done)):
                task = next(task_iter, None)
                if task is None:
                    continue
                _wait_if_quota_exceeded()
                print(f"[RUN] stage={stage} PR #{task['number']}")
                future = executor.submit(_run_codex_exec, task["case_dir"], task["prompt"])
                in_flight[future] = task

    print(f"[SUMMARY] stage={stage} success={succeeded} failed={failed} skipped={skipped}")
    return {"success": succeeded, "failed": failed, "skipped": skipped}


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Stage 2-4 audit investigation prompts over bundled cases.")
    parser.add_argument("--bundle-dir", required=True, help="Stage 1.5 bundle directory")
    parser.add_argument("--stage", required=True, choices=["investigate", "fix", "review", "all"])
    parser.add_argument("--only", default="", help="Comma-separated PR numbers")
    parser.add_argument("--num-workers", type=int, default=1, help="Concurrent codex workers")
    parser.add_argument("--force", action="store_true", help="Force rerun even if output files already exist")
    parser.add_argument("--repo-root", default=str(Path.cwd()), help="Repo root used to resolve run_case.py")
    args = parser.parse_args()

    bundle_root = _resolve_bundle_root(Path(args.bundle_dir).resolve())
    only = _parse_only(args.only) if args.only else None
    repo_root = Path(args.repo_root).resolve()
    run_case_script = repo_root / "hwe_bench" / "harness" / "verify" / "run_case.py"

    if args.stage == "all":
        investigate_summary = _run_stage(
            bundle_root,
            stage="investigate",
            only=only,
            num_workers=args.num_workers,
            force=args.force,
            run_case_script=run_case_script,
        )
        fix_summary = _run_stage(
            bundle_root,
            stage="fix",
            only=only,
            num_workers=args.num_workers,
            force=args.force,
            run_case_script=run_case_script,
        )
        review_summary = _run_stage(
            bundle_root,
            stage="review",
            only=only,
            num_workers=args.num_workers,
            force=args.force,
            run_case_script=run_case_script,
        )
        print(
            "\n".join(
                [
                    "[ALL]",
                    f"bundle_root={bundle_root}",
                    f"investigate={investigate_summary}",
                    f"fix={fix_summary}",
                    f"review={review_summary}",
                ]
            )
        )
        return

    _run_stage(
        bundle_root,
        stage=args.stage,
        only=only,
        num_workers=args.num_workers,
        force=args.force,
        run_case_script=run_case_script,
    )


if __name__ == "__main__":
    main()
