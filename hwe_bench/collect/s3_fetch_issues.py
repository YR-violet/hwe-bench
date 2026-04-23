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
import os
from pathlib import Path

import time

from github import UnknownObjectException, RateLimitExceededException
from tqdm import tqdm
from concurrent.futures import ThreadPoolExecutor, as_completed
from github.Repository import Repository

from hwe_bench.collect.util import get_github, get_tokens, wait_for_rate_limit_reset


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
        "--filtered_prs_file", type=Path, required=True, help="Path to pull file."
    )
    parser.add_argument(
        "--num_workers",
        type=int,
        default=min(32, os.cpu_count() + 4 if os.cpu_count() else 10),
        help="Number of worker threads for fetching issues.",
    )

    return parser


MAX_FETCH_RETRIES = 3
INITIAL_RETRY_DELAY_SECONDS = 5


def fetch_and_prepare_issue(repo_for_issue: Repository, issue_number: int, org_name: str, repo_name_str: str) -> dict | None:
    """Fetches a single issue by number and prepares its data as a dictionary."""
    retry_delay = INITIAL_RETRY_DELAY_SECONDS
    for attempt in range(MAX_FETCH_RETRIES + 1):
        try:
            issue = repo_for_issue.get_issue(issue_number)
            return {
                "org": org_name,
                "repo": repo_name_str,
                "number": issue.number,
                "state": issue.state,
                "title": issue.title,
                "body": issue.body,
            }
        except UnknownObjectException:
            print(f"Issue #{issue_number} not found in {org_name}/{repo_name_str}. Skipping.")
            return None
        except RateLimitExceededException as rle:
            if attempt < MAX_FETCH_RETRIES:
                print(f"Rate limit hit fetching issue #{issue_number} (attempt {attempt + 1}/{MAX_FETCH_RETRIES + 1}).")
                wait_for_rate_limit_reset(rle)
            else:
                print(f"Error: Failed to fetch issue #{issue_number} after {MAX_FETCH_RETRIES + 1} attempts (rate limited).")
        except Exception as e:
            if attempt < MAX_FETCH_RETRIES:
                print(f"Error fetching issue #{issue_number} (attempt {attempt + 1}/{MAX_FETCH_RETRIES + 1}): {e}. Retrying in {retry_delay}s...")
                time.sleep(retry_delay)
                retry_delay *= 2
            else:
                print(f"Error: Failed to fetch issue #{issue_number} after {MAX_FETCH_RETRIES + 1} attempts: {e}")
    return None


def main(tokens: list[str] | None, out_dir: Path, filtered_prs_file: Path, num_workers: int):
    print("starting get all related issues")
    print(f"Output directory: {out_dir}")
    if tokens:
        print(f"Number of GitHub tokens available: {len(tokens)}")
    else:
        print("Warning: No GitHub tokens provided. This script relies on tokens for API access.")
    print(f"Filtered PRs file: {filtered_prs_file}")
    print(f"Number of workers: {num_workers}")

    org_repo_re = re.compile(r"(.+)__(.+)_s02_filtered_prs.jsonl")
    m = org_repo_re.match(filtered_prs_file.name)
    if not m:
        print(f"Error: Invalid pull file name: {filtered_prs_file.name}")
        sys.exit(1)

    org = m.group(1)
    repo = m.group(2)
    print(f"Org: {org}")
    print(f"Repo: {repo}")

    with open(filtered_prs_file, "r", encoding="utf-8") as file:
        filtered_prs = [json.loads(line) for line in file]
        target_issues_numbers = set()
        for pr_data in filtered_prs:
            for issue_num_str in pr_data.get("resolved_issues", []):
                try:
                    target_issues_numbers.add(int(issue_num_str))
                except ValueError:
                    print(f"Warning: Could not convert issue number '{issue_num_str}' to int. Skipping.")
    
    if not target_issues_numbers:
        print("No target issues found from the filtered PRs file. Exiting.")
        output_file_path = out_dir / f"{org}__{repo}_s03_issues.jsonl"
        with open(output_file_path, "w", encoding="utf-8") as out_file:
            pass
        return

    print(f"Found {len(target_issues_numbers)} unique target issue numbers to fetch.")

    if not tokens or not tokens[0]:
        print("Error: GitHub tokens are required to fetch issues. Please provide them via --tokens.")
        sys.exit(1)

    repo_objs_for_issues: list[Repository] = []
    try:
        print(f"Initializing {len(tokens)} GitHub repository objects for fetching issues...")
        repo_objs_for_issues = [get_github(token).get_repo(f"{org}/{repo}") for token in tokens]
        if not repo_objs_for_issues:
            print("Error: Failed to initialize any repository objects from tokens.")
            sys.exit(1)
        print(f"Successfully initialized {len(repo_objs_for_issues)} GitHub repository objects.")
    except Exception as e:
        print(f"Error initializing GitHub clients/repository objects: {e}")
        sys.exit(1)

    fetched_issue_data_list = []
    output_file_path = out_dir / f"{org}__{repo}_s03_issues.jsonl"

    with ThreadPoolExecutor(max_workers=num_workers, thread_name_prefix='Issue-Fetcher') as executor:
        future_to_issue_num = {}
        target_issue_list_for_submission = sorted(list(target_issues_numbers))

        for idx, issue_num in enumerate(tqdm(target_issue_list_for_submission, desc="Submitting issue fetch tasks")):
            repo_to_use = repo_objs_for_issues[idx % len(repo_objs_for_issues)]
            future = executor.submit(fetch_and_prepare_issue, repo_to_use, issue_num, org, repo)
            future_to_issue_num[future] = issue_num
        
        for future in tqdm(as_completed(future_to_issue_num), total=len(target_issue_list_for_submission), desc="Fetching and Preparing Issues"):
            issue_number_logging = future_to_issue_num[future]
            try:
                result_dict = future.result()
                if result_dict:
                    fetched_issue_data_list.append(result_dict)
            except Exception as exc:
                print(f'Issue #{issue_number_logging} generated an exception during future processing: {exc}')

    print(f"Successfully fetched and prepared data for {len(fetched_issue_data_list)} issues out of {len(target_issues_numbers)} targets.")

    fetched_issue_data_list.sort(key=lambda x: x.get('number', 0), reverse=True)
    print(f"Writing {len(fetched_issue_data_list)} issue data to {output_file_path}...")

    with open(output_file_path, "w", encoding="utf-8") as out_file:
        for issue_data in tqdm(fetched_issue_data_list, desc="Writing Issues"):
            out_file.write(
                json.dumps(
                    issue_data,
                    ensure_ascii=False,
                )
                + "\n",
            )
    print(f"Finished writing related issues to {output_file_path}.")


if __name__ == "__main__":
    parser = get_parser()
    args = parser.parse_args()

    tokens = get_tokens(args.tokens)

    main(tokens, Path.cwd() / args.out_dir, args.filtered_prs_file, args.num_workers)
