#!/usr/bin/env python3
"""
Step 7: LLM RTL relevance filter using Codex CLI.

Classifies each PR from the raw dataset into RTL_BUG_FIX, SW_BUG_FIX, or
OTHER using OpenAI Codex CLI.  Supports interrupt/restart (append mode +
processed-PR dedup) and concurrent execution via subprocess pool.
"""

import argparse
import concurrent.futures
import itertools
import json
import random
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from tqdm import tqdm

from hwe_bench.collect.s7_instruction import (
    SYSTEM_PROMPT,
    build_user_prompt,
    parse_classification,
)
from hwe_bench.utils.codex_quota import wait_if_quota_exceeded

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
DEFAULT_MODEL = "gpt-5.4"


def get_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Step 7: LLM RTL relevance filter (Codex CLI)."
    )
    parser.add_argument(
        "--input_file", type=Path, required=True,
        help="Input JSONL (raw_dataset or filtered_patches).",
    )
    parser.add_argument(
        "--output_file", type=Path, required=True,
        help="Output JSONL with classification results.",
    )
    parser.add_argument(
        "--num_workers", type=int, default=3,
        help="Number of concurrent Codex CLI processes (default: 3).",
    )
    parser.add_argument(
        "--model", type=str, default=DEFAULT_MODEL,
        help=f"Codex model to use (default: {DEFAULT_MODEL}).",
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

def load_and_compact_output(output_file: Path) -> set[tuple[str, str, int]]:
    """Load successfully classified PRs and compact the output file.

    Reads the existing output, keeps only successfully classified records
    (valid level1), and rewrites the file to discard stale PARSE_ERROR
    entries from previous runs.  Returns the set of processed PR IDs.
    """
    processed = set()
    if not output_file.exists():
        return processed

    good_records: list[str] = []
    discarded = 0

    with open(output_file, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
                if record.get("level1") in ("RTL_BUG_FIX", "SW_BUG_FIX", "OTHER"):
                    org = record.get("org", "")
                    repo = record.get("repo", "")
                    number = record.get("number", 0)
                    if org and repo and number:
                        processed.add((org, repo, int(number)))
                    good_records.append(line)
                else:
                    discarded += 1
            except (json.JSONDecodeError, KeyError, ValueError):
                discarded += 1
                continue

    # Atomic rewrite: write to temp file then replace, to avoid data loss on crash
    if discarded > 0:
        tmp_fd, tmp_path = tempfile.mkstemp(
            dir=output_file.parent, suffix=".tmp", prefix=output_file.stem
        )
        try:
            with open(tmp_fd, "w", encoding="utf-8") as f:
                for line in good_records:
                    f.write(line + "\n")
            Path(tmp_path).replace(output_file)
        except Exception:
            Path(tmp_path).unlink(missing_ok=True)
            raise
        print(f"Compacted output: kept {len(good_records)} records, discarded {discarded} stale error entries.")

    return processed


def pr_id(pr_data: dict) -> tuple[str, str, int]:
    return (
        pr_data.get("org", ""),
        pr_data.get("repo", ""),
        int(pr_data.get("number", 0)),
    )


# ---------------------------------------------------------------------------
# Single PR processing
# ---------------------------------------------------------------------------

def classify_single_pr(
    pr_data: dict[str, Any],
    system_prompt: str,
    model: str,
    max_retries: int = 2,
) -> dict[str, Any]:
    """Classify a single PR by invoking Codex CLI.

    Returns a result dict ready to be written as a JSONL line.
    """
    org = pr_data.get("org", "")
    repo = pr_data.get("repo", "")
    number = pr_data.get("number", "")

    user_prompt = build_user_prompt(pr_data)
    full_prompt = f"{system_prompt}\n\n---\n\n{user_prompt}"

    retry_delays = [2, 5, 10]  # seconds between retries

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
                timeout=600,  # 10 min per PR
            )

            raw_output = result.stdout.strip()

            if result.returncode != 0:
                stderr_snippet = (result.stderr or "")[:500]
                if attempt < max_retries:
                    delay = retry_delays[min(attempt, len(retry_delays) - 1)]
                    print(f"  Codex error for PR #{number} (attempt {attempt + 1}): {stderr_snippet}. Retrying in {delay}s...")
                    time.sleep(delay + random.uniform(0, 1))
                    continue
                return _error_record(pr_data, f"Codex exit code {result.returncode}: {stderr_snippet}", raw_output)

            classification = parse_classification(raw_output)

            if classification is None:
                if attempt < max_retries:
                    delay = retry_delays[min(attempt, len(retry_delays) - 1)]
                    print(f"  JSON parse failed for PR #{number} (attempt {attempt + 1}), retrying in {delay}s...")
                    time.sleep(delay + random.uniform(0, 1))
                    continue
                return _error_record(pr_data, "Failed to parse classification JSON", raw_output)

            # Build success record
            return {
                "org": org,
                "repo": repo,
                "number": number,
                "pr_url": f"https://github.com/{org}/{repo}/pull/{number}",
                "level1": classification["level1"],
                "level2": classification["level2"],
                "confidence": classification["confidence"],
                "reasoning": classification["reasoning"],
                "model": model,
                "analysis_timestamp": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()),
                "raw_output": raw_output,
            }

        except subprocess.TimeoutExpired:
            if attempt < max_retries:
                delay = retry_delays[min(attempt, len(retry_delays) - 1)]
                print(f"  Timeout for PR #{number} (attempt {attempt + 1}), retrying in {delay}s...")
                time.sleep(delay + random.uniform(0, 1))
                continue
            return _error_record(pr_data, "Codex timed out after 600s")

        except Exception as e:
            if attempt < max_retries:
                delay = retry_delays[min(attempt, len(retry_delays) - 1)]
                print(f"  Error for PR #{number} (attempt {attempt + 1}): {e}. Retrying in {delay}s...")
                time.sleep(delay + random.uniform(0, 1))
                continue
            return _error_record(pr_data, str(e))

    # Should not reach here, but just in case
    return _error_record(pr_data, "Exhausted all retries")


def _error_record(pr_data: dict, error_msg: str, raw_output: str = "") -> dict:
    return {
        "org": pr_data.get("org", ""),
        "repo": pr_data.get("repo", ""),
        "number": pr_data.get("number", ""),
        "pr_url": f"https://github.com/{pr_data.get('org', '')}/{pr_data.get('repo', '')}/pull/{pr_data.get('number', '')}",
        "level1": "PARSE_ERROR",
        "level2": "PARSE_ERROR",
        "confidence": 0.0,
        "reasoning": "",
        "error_message": error_msg,
        "raw_output": raw_output[:2000] if raw_output else "",
        "analysis_timestamp": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()),
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(
    input_file: Path,
    output_file: Path,
    num_workers: int = 3,
    model: str = DEFAULT_MODEL,
    max_cases: int | None = None,
    max_retries: int = 2,
):
    print(f"Step 7: LLM RTL relevance filter (Codex CLI)")
    print(f"Input: {input_file}")
    print(f"Output: {output_file}")
    print(f"Workers: {num_workers}")
    print(f"Model: {model}")

    # Check codex CLI is available
    try:
        ver = subprocess.run(["codex", "--version"], capture_output=True, text=True, check=True)
        print(f"Codex version: {ver.stdout.strip()}")
    except FileNotFoundError:
        print("Error: 'codex' CLI not found. Install it: npm install -g @openai/codex")
        sys.exit(1)
    except subprocess.CalledProcessError as e:
        print(f"Error: 'codex --version' failed: {e}")
        sys.exit(1)

    system_prompt = SYSTEM_PROMPT
    print(f"System prompt loaded ({len(system_prompt)} chars)")

    # Load input
    print(f"Loading PRs from {input_file}...")
    all_prs = []
    with open(input_file, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    all_prs.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    print(f"Loaded {len(all_prs)} PRs.")

    if max_cases and len(all_prs) > max_cases:
        all_prs = all_prs[:max_cases]
        print(f"Limiting to first {max_cases} PRs.")

    # Load existing output for resume (compacts stale error entries)
    output_file.parent.mkdir(parents=True, exist_ok=True)
    processed = load_and_compact_output(output_file)
    prs_to_process = [pr for pr in all_prs if pr_id(pr) not in processed]

    if not prs_to_process:
        print("All PRs already processed. Nothing to do.")
        return
    print(f"{len(prs_to_process)} PRs to process ({len(processed)} already done).")

    # Process with sliding window + quota-aware refill
    written_count = 0
    error_count = 0
    total = len(prs_to_process)

    with ThreadPoolExecutor(max_workers=num_workers, thread_name_prefix="S7-Filter") as executor, \
         open(output_file, "a", encoding="utf-8") as out_f:

        pr_iter = iter(prs_to_process)
        in_flight: dict = {}  # future -> pr_number

        # Seed the pool
        for pr in itertools.islice(pr_iter, num_workers):
            wait_if_quota_exceeded()
            future = executor.submit(classify_single_pr, pr, system_prompt, model, max_retries)
            in_flight[future] = pr.get("number", "?")

        with tqdm(total=total, desc="Classifying PRs") as pbar:
            while in_flight:
                # Wait for at least one task to complete
                done, _ = concurrent.futures.wait(
                    in_flight, return_when=concurrent.futures.FIRST_COMPLETED,
                )

                # Harvest completed results
                for future in done:
                    pr_num = in_flight.pop(future)
                    try:
                        record = future.result()
                        out_f.write(json.dumps(record, ensure_ascii=False) + "\n")
                        out_f.flush()

                        if record.get("level1") == "PARSE_ERROR":
                            error_count += 1
                        else:
                            written_count += 1
                    except Exception as e:
                        print(f"Unexpected error for PR #{pr_num}: {e}")
                        error_count += 1
                    pbar.update(1)

                # Refill: for each completed slot, submit a new task
                for _ in range(len(done)):
                    next_pr = next(pr_iter, None)
                    if next_pr is None:
                        break
                    # Check quota before submitting (main thread only, no contention)
                    wait_if_quota_exceeded()
                    future = executor.submit(
                        classify_single_pr, next_pr, system_prompt, model, max_retries,
                    )
                    in_flight[future] = next_pr.get("number", "?")

    # Summary
    print(f"\nFinished. Classified {written_count} PRs, {error_count} errors "
          f"(total in file: {len(processed) + written_count + error_count}).")

    # Print distribution
    _print_stats(output_file)


def _print_stats(output_file: Path):
    """Print classification distribution from output file."""
    stats: dict[str, int] = {}
    total = 0
    with open(output_file, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
                total += 1
                key = f"{record.get('level1', '?')}/{record.get('level2', '?')}"
                stats[key] = stats.get(key, 0) + 1
            except json.JSONDecodeError:
                continue

    if not stats:
        return

    print(f"\nClassification distribution ({total} total):")
    for key in sorted(stats, key=lambda k: -stats[k]):
        count = stats[key]
        pct = count / total * 100
        print(f"  {key}: {count} ({pct:.1f}%)")


if __name__ == "__main__":
    parser = get_parser()
    args = parser.parse_args()
    main(
        args.input_file,
        args.output_file,
        args.num_workers,
        args.model,
        args.max_cases,
        args.max_retries,
    )
