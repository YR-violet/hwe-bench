#!/usr/bin/env python3

from __future__ import annotations

import argparse
import concurrent.futures
import json
import subprocess
from pathlib import Path
from typing import Any

from hwe_bench.harness.audit.aggregate import aggregate_run
from hwe_bench.harness.audit.prepare_batches import prepare_batches
from hwe_bench.utils.codex_quota import wait_if_quota_exceeded as _wait_if_quota_exceeded


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
    out: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if part:
            out.add(int(part))
    return out


def _read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _run_codex_exec(batch_dir: Path, prompt: str) -> int:
    cmd = [
        "codex",
        "exec",
        "-p",
        "normal",
        "--full-auto",
        "--ephemeral",
        "--skip-git-repo-check",
        "-C",
        str(batch_dir),
        prompt,
    ]
    return subprocess.run(cmd, stdin=subprocess.DEVNULL).returncode


def _prepare_tasks(
    run_root: Path,
    *,
    prompt_template: str,
    repo_root: Path,
    agent: str,
    dataset_name: str,
    force: bool,
    resume: bool,
) -> tuple[list[dict[str, Any]], int]:
    prepared = _read_json(run_root / "prepared_batches.json")
    skipped = 0
    tasks: list[dict[str, Any]] = []
    schema_path = (repo_root / "hwe_bench" / "harness" / "audit" / "schemas" / "batch_result.schema.json").resolve()

    for batch in prepared.get("batches", []):
        batch_id = batch["batch_id"]
        batch_dir = run_root / "batches" / batch_id
        batch_result_path = batch_dir / "batch_result.json"
        if batch_result_path.exists() and not force and resume:
            try:
                existing = _read_json(batch_result_path)
                if existing.get("batch_id") == batch_id:
                    skipped += 1
                    continue
            except Exception:
                pass

        manifest = _read_json(batch_dir / "batch_manifest.json")
        rendered_prompt = _render_prompt(
            prompt_template,
            {
                "BATCH_ID": batch_id,
                "ORG": manifest["org"],
                "REPO": manifest["repo"],
                "AGENT": agent,
                "DATASET_NAME": dataset_name,
                "CASE_COUNT": str(manifest["case_count"]),
                "BATCH_DIR": str(batch_dir.resolve()),
                "RUN_ROOT": str(run_root.resolve()),
                "REPO_ROOT": str(repo_root.resolve()),
                "SCHEMA_PATH": str(schema_path),
            },
        )
        _write_text(batch_dir / "prompt_rendered.md", rendered_prompt)
        tasks.append(
            {
                "batch_id": batch_id,
                "batch_dir": batch_dir,
                "batch_result_path": batch_result_path,
                "prompt": rendered_prompt,
            }
        )
    return tasks, skipped


def main() -> None:
    parser = argparse.ArgumentParser(description="Batch-run Codex audit after HWE-bench evaluation.")
    parser.add_argument("--dataset", required=True, help="Dataset JSONL path")
    parser.add_argument("--patches", required=True, help="Agent patches.jsonl path")
    parser.add_argument("--jobs", nargs="+", required=True, help="One or more Harbor jobs directories")
    parser.add_argument("--eval-root", required=True, help="run_evaluation output root (contains <org>/<repo>/evals/pr-N/report.json + fix-patch-run.log)")
    parser.add_argument(
        "--final-report",
        default="",
        help="Optional explicit path to aggregate final_report.json. "
             "If not provided, falls back to <eval-root>/output/final_report.json.",
    )
    parser.add_argument("--org", required=True, help="GitHub org")
    parser.add_argument("--repo", required=True, help="GitHub repo")
    parser.add_argument("--agent", required=True, help="Agent label")
    parser.add_argument("--dataset-name", default="", help="Dataset label, e.g. easy74")
    parser.add_argument("--batch-size", type=int, default=20, help="Cases per batch")
    parser.add_argument("--num-workers", type=int, default=1, help="Concurrent codex workers")
    parser.add_argument("--only", default="", help="Comma-separated PR numbers")
    parser.add_argument("--force", action="store_true", help="Re-run all batches")
    parser.add_argument("--resume", action="store_true", help="Skip batches that already have batch_result.json")
    parser.add_argument(
        "--out-root",
        default="artifacts/audit",
        help="Artifacts root directory relative to project root",
    )
    parser.add_argument(
        "--prompt-template",
        default="hwe_bench/harness/audit/prompts/audit.md",
        help="Prompt template markdown path",
    )
    parser.add_argument(
        "--repo-root",
        default=str(Path.cwd()),
        help="Repo root directory passed to prompt rendering",
    )
    args = parser.parse_args()

    dataset_path = Path(args.dataset).resolve()
    patches_path = Path(args.patches).resolve()
    job_dirs = [Path(path).resolve() for path in args.jobs]
    eval_root = Path(args.eval_root).resolve()
    out_root = Path(args.out_root).resolve()
    repo_root = Path(args.repo_root).resolve()
    prompt_template = _load_prompt_template(Path(args.prompt_template).resolve())
    dataset_name = args.dataset_name or dataset_path.stem
    only = _parse_only(args.only) if args.only else None

    final_report_path = Path(args.final_report).resolve() if args.final_report else None

    run_root = prepare_batches(
        dataset_path=dataset_path,
        patches_path=patches_path,
        job_dirs=job_dirs,
        eval_root=eval_root,
        out_root=out_root,
        org=args.org,
        repo=args.repo,
        agent=args.agent,
        dataset_name=dataset_name,
        batch_size=max(1, args.batch_size),
        only=only,
        final_report_path=final_report_path,
    )

    tasks, skipped = _prepare_tasks(
        run_root,
        prompt_template=prompt_template,
        repo_root=repo_root,
        agent=args.agent,
        dataset_name=dataset_name,
        force=args.force,
        resume=args.resume,
    )

    succeeded = 0
    failed = 0
    task_iter = iter(tasks)

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=max(1, args.num_workers),
        thread_name_prefix="audit-codex",
    ) as executor:
        in_flight: dict[concurrent.futures.Future[int], dict[str, Any]] = {}

        for _ in range(min(max(1, args.num_workers), len(tasks))):
            task = next(task_iter, None)
            if task is None:
                break
            _wait_if_quota_exceeded()
            print(f"[RUN] {task['batch_id']}")
            future = executor.submit(_run_codex_exec, task["batch_dir"], task["prompt"])
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
                    print(f"[WARN] codex exec crashed for {task['batch_id']}: {exc}")

                if rc != 0:
                    print(f"[WARN] codex exec exited with code {rc} for {task['batch_id']}")

                batch_result_path = task["batch_result_path"]
                if not batch_result_path.exists():
                    failed += 1
                    print(f"[FAIL] Missing batch_result.json for {task['batch_id']}")
                else:
                    try:
                        result = _read_json(batch_result_path)
                    except Exception as exc:
                        failed += 1
                        print(f"[FAIL] Invalid JSON for {task['batch_id']}: {exc}")
                    else:
                        if result.get("batch_id") != task["batch_id"]:
                            failed += 1
                            print(f"[FAIL] batch_id mismatch in {task['batch_id']}")
                        else:
                            succeeded += 1
                            print(f"[OK] {task['batch_id']} success")

            for _ in range(len(done)):
                task = next(task_iter, None)
                if task is None:
                    continue
                _wait_if_quota_exceeded()
                print(f"[RUN] {task['batch_id']}")
                future = executor.submit(_run_codex_exec, task["batch_dir"], task["prompt"])
                in_flight[future] = task

    final_report = aggregate_run(run_root)
    print(
        "\n".join(
            [
                "[SUMMARY]",
                f"run_root={run_root}",
                f"success={succeeded}",
                f"failed={failed}",
                f"skipped={skipped}",
                f"final_report={run_root / 'final_audit_report.json'}",
                f"audited_cases={final_report['summary']['audited_case_count']}",
            ]
        )
    )


if __name__ == "__main__":
    main()
