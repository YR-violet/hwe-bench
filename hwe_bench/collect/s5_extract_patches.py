# Copyright (c) 2024 Bytedance Ltd. and/or its affiliates

#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at

#      http://www.apache.org/licenses/LICENSE-2.0

#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.

import argparse
import json
import re
import sys
import time
import os
from pathlib import Path
from typing import Optional

import requests
from tqdm import tqdm
from unidiff import PatchSet
from concurrent.futures import ThreadPoolExecutor, as_completed

from hwe_bench.collect.util import get_tokens, optional_int


def get_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="A command-line tool for processing repositories."
    )
    parser.add_argument(
        "--out_dir", type=Path, required=True, help="Output directory path."
    )
    parser.add_argument(
        "--tokens",
        type=str,
        nargs="*",
        default=None,
        help="API token(s) or path to token file.",
    )
    parser.add_argument(
        "--filtered-prs-with-issues-file",
        type=Path,
        required=True,
        help="Filtered PRs with issues file.",
    )
    parser.add_argument(
        "--delay-on-error",
        type=optional_int,
        default=300,
        help="Delay in seconds before retrying on error. If none, exit on error.",
    )
    parser.add_argument(
        "--retry-attempts",
        type=int,
        default=3,
        help="Number of attempts to retry on error.",
    )
    parser.add_argument(
        "--num_workers",
        type=int,
        default=min(32, os.cpu_count() + 4 if os.cpu_count() else 10),
        help="Number of worker threads for processing pull request data after fetching.",
    )

    return parser


def extract_patches_from_compare(pull: dict, token: str) -> tuple[str, str, list[str], int, int]:
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3.diff",
    }

    org = pull.get("org")
    repo = pull.get("repo")
    base_sha = pull.get("base", {}).get("sha")
    commits = pull.get("commits", [])
    test_patch = ""
    fix_patch = ""
    modified_files = []
    total_lines_added = 0
    total_lines_removed = 0

    if not all([org, repo, base_sha]) or not commits:
        return fix_patch, test_patch, modified_files, total_lines_added, total_lines_removed

    head_sha = commits[-1].get("sha")
    if not head_sha:
        raise ValueError("Missing head SHA in last commit")

    compare_url = (
        f"https://api.github.com/repos/{org}/{repo}/compare/{base_sha}...{head_sha}"
    )
    response = requests.get(compare_url, headers=headers)
    if response.status_code != 200:
        raise Exception(
            f"Failed to fetch patch: {response.status_code} - {response.text[:300]}"
        )

    patch = response.text

    patch_set = PatchSet(patch)
    for hunk in patch_set:
        if hunk.path not in modified_files:
            modified_files.append(hunk.path)
        total_lines_added += hunk.added
        total_lines_removed += hunk.removed
        
        if any(
                test_word in hunk.path.lower() for test_word in ["test", "tests", "e2e", "testing", "tb", "tbs", "testbench"]
        ):
            test_patch += str(hunk)
        else:
            fix_patch += str(hunk)
    return fix_patch, test_patch, modified_files, total_lines_added, total_lines_removed


def process_pr_for_patches(pr_data: dict, token: str, retry_attempts: int, delay_on_error: Optional[int]) -> Optional[dict]:
    """Processes a single PR to extract patches, with retries."""
    for attempt in range(retry_attempts):
        try:
            fix_patch, test_patch, modified_files, lines_added, lines_removed = extract_patches_from_compare(pr_data, token)
            pr_data["fix_patch"] = fix_patch
            pr_data["test_patch"] = test_patch
            pr_data["modified_files"] = modified_files
            pr_data["lines_added"] = lines_added
            pr_data["lines_removed"] = lines_removed

            if not fix_patch:
                return None

            return pr_data
        except Exception as e:
            print(f"Error processing PR #{pr_data.get('number', 'unknown')} on attempt {attempt + 1}/{retry_attempts}: {e}")
            if delay_on_error is None or attempt == retry_attempts - 1:
                return None
            else:
                print(
                    f"The {attempt + 1} attempt for PR #{pr_data.get('number', 'unknown')} failed. Sleeping for {delay_on_error}s then retrying..."
                )
                time.sleep(delay_on_error)
    return None


def main(
    tokens: list[str],
    out_dir: Path,
    filtered_prs_with_issues_file: Path,
    delay_on_error: Optional[int],
    retry_attempts: int,
    num_workers: int,
):
    print("starting build complete dataset (multi-threaded)")
    if not tokens or not tokens[0]:
        print("Error: GitHub tokens are required for extracting patches.")
        sys.exit(1)
    print(f"Number of tokens available: {len(tokens)}")
    print(f"Output directory: {out_dir}")
    print(f"Input PRs file: {filtered_prs_with_issues_file}")
    print(f"Delay on error: {delay_on_error}")
    print(f"Retry attempts: {retry_attempts}")
    print(f"Number of workers: {num_workers}")

    org_repo_re = re.compile(r"(.+)__(.+)_s04_merged.jsonl")
    m = org_repo_re.match(filtered_prs_with_issues_file.name)
    if not m:
        print(f"Error: Invalid pull file name: {filtered_prs_with_issues_file.name}")
        sys.exit(1)

    org = m.group(1)
    repo = m.group(2)
    print(f"Org: {org}")
    print(f"Repo: {repo}")

    with open(filtered_prs_with_issues_file, "r", encoding="utf-8") as file:
        all_prs_from_input = [json.loads(line) for line in file]
    print(f"Loaded {len(all_prs_from_input)} PRs from input file.")

    raw_dataset_numbers = set()
    output_file_path = out_dir / f"{org}__{repo}_s05_raw_dataset.jsonl"
    try:
        with open(output_file_path, "r", encoding="utf-8") as file:
            for line in file:
                data = json.loads(line)
                raw_dataset_numbers.add(data["number"])
        print(f"Loaded {len(raw_dataset_numbers)} PR numbers from existing output file to avoid reprocessing.")
    except FileNotFoundError:
        print("No existing output file found. Will process all applicable PRs.")

    prs_to_process = [
        pr for pr in all_prs_from_input if pr["number"] not in raw_dataset_numbers
    ]

    if not prs_to_process:
        print("No new PRs to process after checking against existing output. Exiting.")
        return
    
    print(f"Found {len(prs_to_process)} new PRs to process for patch extraction.")

    with ThreadPoolExecutor(max_workers=num_workers, thread_name_prefix='Patch-Extractor') as executor, \
         open(output_file_path, "a", encoding="utf-8") as out_file:
        
        future_to_pr_num = {}
        for idx, pr_data_item in enumerate(tqdm(prs_to_process, desc="Submitting PRs for patch extraction")):
            token_for_pr = tokens[idx % len(tokens)]
            future = executor.submit(process_pr_for_patches, pr_data_item, token_for_pr, retry_attempts, delay_on_error)
            future_to_pr_num[future] = pr_data_item.get("number", "unknown")

        processed_count = 0
        for future in tqdm(as_completed(future_to_pr_num), total=len(prs_to_process), desc="Extracting Patches"):
            pr_num_logging = future_to_pr_num[future]
            try:
                processed_pr_data = future.result()
                if processed_pr_data:
                    out_file.write(json.dumps(processed_pr_data, ensure_ascii=False) + "\n")
                    processed_count += 1
            except Exception as exc:
                print(f'PR #{pr_num_logging} generated an unexpected exception during future.result(): {exc}')
        
        print(f"Finished processing. Successfully extracted patches for and wrote {processed_count} new PRs.")


if __name__ == "__main__":
    parser = get_parser()
    args = parser.parse_args()

    tokens = get_tokens(args.tokens)

    main(
        tokens,
        args.out_dir,
        args.filtered_prs_with_issues_file,
        args.delay_on_error,
        args.retry_attempts,
        args.num_workers,
    )
