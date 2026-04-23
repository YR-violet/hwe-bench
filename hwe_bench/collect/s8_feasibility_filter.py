#!/usr/bin/env python3
"""Step 8: Benchmark feasibility filter using Codex CLI.

Scores each RTL_BUG_FIX / SW_BUG_FIX PR on four dimensions (benchmark_value,
cross_layer_depth, reproducer_signal, simulation_cost) and computes a
deterministic priority_score for downstream ranking.

Input:  raw_dataset JSONL merged with s7 classification (needs level1/level2 fields).
Output: JSONL with per-PR scoring results and priority_score.

Supports interrupt/restart (append mode + dedup) and concurrent execution.
"""

import argparse
import concurrent.futures
import json
import random
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from tqdm import tqdm

from hwe_bench.collect.s8_instruction import (
    SYSTEM_PROMPT,
    build_user_prompt,
    compute_priority_score,
    parse_scoring,
)
from hwe_bench.utils.codex_quota import wait_if_quota_exceeded

DEFAULT_MODEL = "gpt-5.4"
INCLUDE_LEVEL1 = {"RTL_BUG_FIX", "SW_BUG_FIX"}


def get_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Step 8: Benchmark feasibility filter (Codex CLI)."
    )
    parser.add_argument(
        "--input_file", type=Path, required=True,
        help="Input JSONL — raw_dataset (with fix_patch, test_patch, etc.).",
    )
    parser.add_argument(
        "--classification_file", type=Path, required=True,
        help="s7 classification JSONL (with level1/level2). "
             "Merged with input_file by (org, repo, number).",
    )
    parser.add_argument(
        "--output_file", type=Path, required=True,
        help="Output JSONL with scoring results.",
    )
    parser.add_argument(
        "--num_workers", type=int, default=3,
        help="Concurrent Codex CLI processes (default: 3).",
    )
    parser.add_argument(
        "--model", type=str, default=DEFAULT_MODEL,
        help=f"Model to use (default: {DEFAULT_MODEL}).",
    )
    parser.add_argument(
        "--max_cases", type=int, default=None,
        help="Max PRs to process (default: all).",
    )
    parser.add_argument(
        "--max_retries", type=int, default=2,
        help="Max retries per PR on failure (default: 2).",
    )
    return parser


# ---------------------------------------------------------------------------
# Resume helpers
# ---------------------------------------------------------------------------

def _pr_id(pr: dict) -> tuple[str, str, int]:
    return (pr.get("org", ""), pr.get("repo", ""), int(pr.get("number", 0)))


def _load_processed(output_file: Path) -> set[tuple[str, str, int]]:
    """Load already-scored PRs and compact the output file."""
    processed: set[tuple[str, str, int]] = set()
    if not output_file.exists():
        return processed

    good: list[str] = []
    discarded = 0

    with open(output_file, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                if "benchmark_value" in rec and rec.get("status") != "PARSE_ERROR":
                    org = rec.get("org", "")
                    repo = rec.get("repo", "")
                    number = rec.get("number", 0)
                    if org and repo and number:
                        processed.add((org, repo, int(number)))
                    good.append(line)
                else:
                    discarded += 1
            except (json.JSONDecodeError, KeyError, ValueError):
                discarded += 1

    if discarded > 0:
        tmp_fd, tmp_path = tempfile.mkstemp(
            dir=output_file.parent, suffix=".tmp", prefix=output_file.stem
        )
        try:
            with open(tmp_fd, "w", encoding="utf-8") as f:
                for line in good:
                    f.write(line + "\n")
            Path(tmp_path).replace(output_file)
        except Exception:
            Path(tmp_path).unlink(missing_ok=True)
            raise
        print(f"Compacted output: kept {len(good)}, discarded {discarded} stale entries.")

    return processed


# ---------------------------------------------------------------------------
# Single PR scoring
# ---------------------------------------------------------------------------

def _score_single_pr(
    pr_data: dict[str, Any],
    model: str,
    max_retries: int = 2,
) -> dict[str, Any]:
    """Score a single PR by invoking Codex CLI."""
    org = pr_data.get("org", "")
    repo = pr_data.get("repo", "")
    number = pr_data.get("number", "")

    user_prompt = build_user_prompt(pr_data)
    full_prompt = f"{SYSTEM_PROMPT}\n\n---\n\n{user_prompt}"

    retry_delays = [2, 5, 10]

    for attempt in range(max_retries + 1):
        try:
            result = subprocess.run(
                [
                    "codex", "exec",
                    "-p", "normal",
                    "-m", model,
                    "--skip-git-repo-check",
                    "--sandbox", "read-only",
                    "--ephemeral",
                    full_prompt,
                ],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=600,
            )

            raw_output = result.stdout.strip()

            if result.returncode != 0:
                stderr_snippet = (result.stderr or "")[:500]
                if attempt < max_retries:
                    delay = retry_delays[min(attempt, len(retry_delays) - 1)]
                    print(f"  Codex error for PR #{number} (attempt {attempt + 1}): "
                          f"{stderr_snippet}. Retrying in {delay}s...")
                    time.sleep(delay + random.uniform(0, 1))
                    continue
                return _error_record(pr_data, f"exit code {result.returncode}: {stderr_snippet}", raw_output)

            scoring = parse_scoring(raw_output)
            if scoring is not None:
                # RTL_BUG_FIX: cross_layer_depth is always 0
                if pr_data.get("level1") == "RTL_BUG_FIX":
                    scoring["cross_layer_depth"] = 0
            if scoring is None:
                if attempt < max_retries:
                    delay = retry_delays[min(attempt, len(retry_delays) - 1)]
                    print(f"  Parse failed for PR #{number} (attempt {attempt + 1}), retrying...")
                    time.sleep(delay + random.uniform(0, 1))
                    continue
                return _error_record(pr_data, "Failed to parse scoring JSON", raw_output)

            return {
                "org": org,
                "repo": repo,
                "number": number,
                "pr_url": f"https://github.com/{org}/{repo}/pull/{number}",
                "level1": pr_data.get("level1", ""),
                "level2": pr_data.get("level2", ""),
                **scoring,
                "priority_score": compute_priority_score(scoring),
                "model": model,
                "status": "ok",
                "analysis_timestamp": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()),
                "raw_output": raw_output,
            }

        except subprocess.TimeoutExpired:
            if attempt < max_retries:
                delay = retry_delays[min(attempt, len(retry_delays) - 1)]
                print(f"  Timeout for PR #{number} (attempt {attempt + 1}), retrying...")
                time.sleep(delay + random.uniform(0, 1))
                continue
            return _error_record(pr_data, "Codex timed out after 600s")

        except Exception as e:
            if attempt < max_retries:
                delay = retry_delays[min(attempt, len(retry_delays) - 1)]
                print(f"  Error for PR #{number} (attempt {attempt + 1}): {e}. Retrying...")
                time.sleep(delay + random.uniform(0, 1))
                continue
            return _error_record(pr_data, str(e))

    return _error_record(pr_data, "Exhausted all retries")


def _error_record(pr_data: dict, error_msg: str, raw_output: str = "") -> dict:
    return {
        "org": pr_data.get("org", ""),
        "repo": pr_data.get("repo", ""),
        "number": pr_data.get("number", ""),
        "pr_url": f"https://github.com/{pr_data.get('org', '')}/{pr_data.get('repo', '')}/pull/{pr_data.get('number', '')}",
        "level1": pr_data.get("level1", ""),
        "level2": pr_data.get("level2", ""),
        "benchmark_value": 0,
        "cross_layer_depth": 0,
        "reproducer_signal": 0,
        "simulation_cost": 0,
        "reproducer_path": "unclear",
        "reasoning": "",
        "priority_score": 0,
        "model": "",
        "status": "PARSE_ERROR",
        "error_message": error_msg,
        "raw_output": raw_output[:2000] if raw_output else "",
        "analysis_timestamp": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()),
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(
    input_file: Path,
    classification_file: Path,
    output_file: Path,
    num_workers: int = 3,
    model: str = DEFAULT_MODEL,
    max_cases: int | None = None,
    max_retries: int = 2,
) -> None:
    input_path = input_file.resolve()
    output_path = output_file.resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Load s7 classification (authoritative for level1/level2)
    cls_path = classification_file.resolve()
    cls_map: dict[tuple[str, str, int], dict] = {}
    with open(cls_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            key = (rec.get("org", ""), rec.get("repo", ""), int(rec.get("number", 0)))
            cls_map[key] = rec

    # Load raw dataset and merge classification
    raw_prs: list[dict[str, Any]] = []
    with open(input_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                raw_prs.append(json.loads(line))

    for pr in raw_prs:
        key = _pr_id(pr)
        if key in cls_map:
            cr = cls_map[key]
            pr["level1"] = cr.get("level1")
            pr["level2"] = cr.get("level2")

    print(f"Merged {len(cls_map)} classification records into {len(raw_prs)} PRs.")

    all_prs = [pr for pr in raw_prs if pr.get("level1") in INCLUDE_LEVEL1]

    if not all_prs:
        print(f"No RTL_BUG_FIX / SW_BUG_FIX PRs found in {input_path}")
        return

    # Resume: skip already-processed PRs
    processed = _load_processed(output_path)
    pending = [pr for pr in all_prs if _pr_id(pr) not in processed]

    if max_cases is not None:
        pending = pending[:max_cases]

    total = len(all_prs)
    skip_count = len(processed)
    run_count = len(pending)
    print(f"Total eligible: {total}, already scored: {skip_count}, to process: {run_count}")

    if not pending:
        print("Nothing to do.")
        return

    # Process with thread pool
    success = 0
    errors = 0

    with open(output_path, "a", encoding="utf-8") as out_f:
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=max(1, num_workers)
        ) as executor:
            futures = {}
            task_iter = iter(pending)

            # Initial batch
            for _ in range(num_workers):
                pr = next(task_iter, None)
                if pr is None:
                    break
                wait_if_quota_exceeded()
                fut = executor.submit(_score_single_pr, pr, model, max_retries)
                futures[fut] = pr

            pbar = tqdm(total=run_count, desc="s8-scoring", unit="pr")

            while futures:
                done, _ = concurrent.futures.wait(
                    futures, return_when=concurrent.futures.FIRST_COMPLETED
                )
                for fut in done:
                    pr = futures.pop(fut)
                    try:
                        result = fut.result()
                    except Exception as e:
                        result = _error_record(pr, str(e))

                    out_f.write(json.dumps(result, ensure_ascii=False) + "\n")
                    out_f.flush()

                    if result.get("status") == "PARSE_ERROR":
                        errors += 1
                        pbar.write(f"[ERR] PR #{result.get('number')}: {result.get('error_message', '')[:80]}")
                    else:
                        success += 1
                        ps = result.get("priority_score", 0)
                        rs = result.get("reproducer_signal", 0)
                        pbar.write(f"[OK]  PR #{result.get('number')}: score={ps} rs={rs} path={result.get('reproducer_path')}")

                    pbar.update(1)

                # Refill
                for _ in range(len(done)):
                    pr = next(task_iter, None)
                    if pr is None:
                        break
                    wait_if_quota_exceeded()
                    fut = executor.submit(_score_single_pr, pr, model, max_retries)
                    futures[fut] = pr

            pbar.close()

    print(f"\n[SUMMARY] success={success}, errors={errors}, output={output_path}")


if __name__ == "__main__":
    args = get_parser().parse_args()
    main(
        args.input_file,
        args.classification_file,
        args.output_file,
        args.num_workers,
        args.model,
        args.max_cases,
        args.max_retries,
    )
