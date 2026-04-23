from __future__ import annotations

import concurrent.futures
import json
import subprocess
from pathlib import Path
from typing import Any, Callable, Iterable

from hwe_bench.utils.codex_quota import wait_if_quota_exceeded as _wait_if_quota_exceeded


Task = dict[str, Any]
RunFn = Callable[[Task], Any]
ResultChecker = Callable[[Task, Any], tuple[str, str]]


def _read_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content or "", encoding="utf-8")


def _safe_str(value: Any) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return str(value)


def _load_prompt_template(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _render_prompt(template: str, replacements: dict[str, str]) -> str:
    rendered = template
    for key, value in replacements.items():
        rendered = rendered.replace("{" + key + "}", value)
    return rendered


def _parse_only_numbers(raw: str) -> set[int]:
    only: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if part:
            only.add(int(part))
    return only


def run_codex_exec(task: Task) -> int:
    cmd = [
        "codex",
        "exec",
        "-p",
        "normal",
        "--full-auto",
        "--ephemeral",
        "--skip-git-repo-check",
        "-C",
        str(task["workdir"]),
        task["prompt"],
    ]
    return subprocess.run(cmd, stdin=subprocess.DEVNULL).returncode


def _task_label(task: Task, label: str) -> str:
    display_name = _safe_str(task.get("display_name", ""))
    if display_name:
        text = display_name
    elif all(key in task for key in ("org", "repo", "number")):
        text = f"{task['org']}/{task['repo']} PR #{task['number']}"
    elif "number" in task:
        text = f"PR #{task['number']}"
    else:
        text = label

    base_sha = _safe_str(task.get("base_sha", ""))
    if base_sha:
        text = f"{text} (base_sha={base_sha})"
    return text


def _print_summary(
    counts: dict[str, int],
    summary_extra: dict[str, Any] | None,
) -> None:
    total = counts["success"] + counts["failed"] + counts["skipped"]
    lines = [
        "[SUMMARY]",
        f"total={total}",
        f"success={counts['success']}",
        f"failed={counts['failed']}",
        f"skipped={counts['skipped']}",
    ]
    if summary_extra:
        for key, value in summary_extra.items():
            lines.append(f"{key}={_safe_str(value)}")
    print("\n".join(lines))


def run_codex_batch(
    tasks: list[Task],
    run_fn: RunFn,
    result_checker: ResultChecker,
    num_workers: int = 1,
    label: str = "batch",
    *,
    initial_skipped: int = 0,
    summary_extra: dict[str, Any] | None = None,
) -> dict[str, int]:
    counts = {"success": 0, "failed": 0, "skipped": initial_skipped}
    if not tasks:
        _print_summary(counts, summary_extra)
        return counts

    task_iter = iter(tasks)
    max_workers = max(1, num_workers)

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=max_workers,
        thread_name_prefix=f"{label}-codex",
    ) as executor:
        in_flight: dict[concurrent.futures.Future[Any], Task] = {}

        for _ in range(max_workers):
            next_task = next(task_iter, None)
            if next_task is None:
                break
            _wait_if_quota_exceeded()
            print(f"[RUN] {_task_label(next_task, label)}")
            future = executor.submit(run_fn, next_task)
            in_flight[future] = next_task

        while in_flight:
            done, _ = concurrent.futures.wait(
                in_flight,
                return_when=concurrent.futures.FIRST_COMPLETED,
            )

            for future in done:
                task = in_flight.pop(future)
                try:
                    run_result = future.result()
                    status, message = result_checker(task, run_result)
                except Exception as e:
                    status = "failed"
                    message = f"codex exec crashed: {e}"

                if status not in counts:
                    raise ValueError(f"Invalid batch status: {status}")

                counts[status] += 1
                prefix = {
                    "success": "[OK]",
                    "failed": "[FAIL]",
                    "skipped": "[SKIP]",
                }[status]
                if message:
                    print(f"{prefix} {_task_label(task, label)}: {message}")
                else:
                    print(f"{prefix} {_task_label(task, label)}")

            for _ in range(len(done)):
                next_task = next(task_iter, None)
                if next_task is None:
                    break
                _wait_if_quota_exceeded()
                print(f"[RUN] {_task_label(next_task, label)}")
                future = executor.submit(run_fn, next_task)
                in_flight[future] = next_task

    _print_summary(counts, summary_extra)
    return counts
