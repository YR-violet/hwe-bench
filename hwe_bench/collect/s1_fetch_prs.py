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
from datetime import datetime
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
import os
import time

from github import RateLimitExceededException
from tqdm import tqdm

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
    parser.add_argument("--org", type=str, required=True, help="Organization name.")
    parser.add_argument("--repo", type=str, required=True, help="Repository name.")
    parser.add_argument(
        "--num_processing_workers",
        type=int,
        default=min(32, os.cpu_count() + 4 if os.cpu_count() else 10),
        help="Number of worker threads for processing pull request data after fetching.",
    )

    return parser


def main(tokens: list[str], out_dir: Path, org: str, repo_name: str, num_processing_workers: int):
    print("starting get all pull requests")
    print(f"Output directory: {out_dir}")
    print(f"Number of tokens available: {len(tokens)}")
    print(f"Org: {org}")
    print(f"Repo: {repo_name}")
    print(f"Number of data processing workers: {num_processing_workers}")

    if not tokens:
        print("Error: No GitHub tokens provided. Cannot proceed.")
        return

    clients = [get_github(token) for token in tokens]
    
    g_primary = clients[0]
    try:
        r_primary = g_primary.get_repo(f"{org}/{repo_name}")
        all_pulls_paginated_list = r_primary.get_pulls(state="all")
        total_prs = all_pulls_paginated_list.totalCount
        print(f"Total PRs reported by API: {total_prs}")
    except RateLimitExceededException:
        print(f"Rate limit exceeded with the primary token while trying to get total PR count for {org}/{repo_name}. Cannot plan page distribution.")
        return
    except Exception as e:
        print(f"Error getting repository or total PR count for {org}/{repo_name}: {e}. Cannot plan page distribution.")
        return

    if total_prs == 0:
        print(f"No pull requests found for {org}/{repo_name}. Exiting.")
        output_file_path = out_dir / f"{org}__{repo_name}_s01_prs.jsonl"
        with open(output_file_path, "w", encoding="utf-8") as file:
            pass
        return

    page_size = g_primary.per_page
    num_pages = (total_prs + page_size - 1) // page_size
    print(f"Calculated number of pages: {num_pages} (with page size: {page_size})")

    fetched_prs_all = []
    
    # Retry parameters for fetching pages
    MAX_FETCH_RETRIES = 3
    INITIAL_RETRY_DELAY_SECONDS = 5 # Initial delay for retries

    def fetch_pr_page(client_instance_index: int, page_num: int):
        actual_client = clients[client_instance_index]
        current_retry_delay = INITIAL_RETRY_DELAY_SECONDS
        for attempt in range(MAX_FETCH_RETRIES + 1):
            try:
                local_repo_obj = actual_client.get_repo(f"{org}/{repo_name}")
                page_data = local_repo_obj.get_pulls(state="all").get_page(page_num)
                return page_data if page_data is not None else []
            except RateLimitExceededException as rle:
                print(f"Rate limit hit for token index {client_instance_index} on page {page_num}, attempt {attempt + 1}/{MAX_FETCH_RETRIES + 1}.")
                if attempt < MAX_FETCH_RETRIES:
                    wait_for_rate_limit_reset(rle)
                else:
                    print(f"Failed to fetch page {page_num} after {MAX_FETCH_RETRIES + 1} attempts (rate limited).")
            except Exception as e:
                print(f"Error fetching page {page_num} for {org}/{repo_name} with token index {client_instance_index}, attempt {attempt + 1}/{MAX_FETCH_RETRIES + 1}. Error: {e}")
                if attempt < MAX_FETCH_RETRIES:
                    print(f"Retrying page {page_num} in {current_retry_delay} seconds...")
                    time.sleep(current_retry_delay)
                    current_retry_delay *= 2
                else:
                    print(f"Failed to fetch page {page_num} after {MAX_FETCH_RETRIES + 1} attempts.")

        return None

    num_fetching_workers = len(clients)
    failed_pages = []
    with ThreadPoolExecutor(max_workers=num_fetching_workers, thread_name_prefix='PR-Fetcher') as fetch_executor:
        fetch_futures = {}
        for i in range(num_pages):
            client_for_page_idx = i % len(clients)
            future = fetch_executor.submit(fetch_pr_page, client_for_page_idx, i)
            fetch_futures[future] = i

        for future in tqdm(as_completed(fetch_futures), total=len(fetch_futures), desc=f"Fetching PR Pages for {org}/{repo_name}"):
            page_num = fetch_futures[future]
            try:
                page_result_list = future.result()
                if page_result_list is not None:
                    fetched_prs_all.extend(page_result_list)
                else:
                    failed_pages.append(page_num)
            except Exception as e:
                print(f"Critical error processing page {page_num}: {e}")
                failed_pages.append(page_num)

    if failed_pages:
        print(f"WARNING: {len(failed_pages)} page(s) failed to fetch: {sorted(failed_pages)}")
        print("The output will be incomplete. Re-run to retry failed pages.")
    print(f"Total PR objects fetched across all pages: {len(fetched_prs_all)}")
    
    unique_prs_dict = {pr.id: pr for pr in fetched_prs_all}
    final_prs_to_process = list(unique_prs_dict.values())
    if len(fetched_prs_all) != len(final_prs_to_process):
        print(f"Deduplicated PRs: {len(fetched_prs_all)} -> {len(final_prs_to_process)}")
    else:
        print("No duplicate PRs found after fetching.")

    def datetime_serializer(obj):
        if isinstance(obj, datetime):
            return obj.isoformat()
        return obj

    output_file_path = out_dir / f"{org}__{repo_name}_s01_prs.jsonl"

    def prepare_pull_data(pull):
        # Fetch commits for merged PRs so Step 2 can run locally
        commits = []
        commits_ok = True
        if pull.merged_at:
            retry_delay = INITIAL_RETRY_DELAY_SECONDS
            for attempt in range(MAX_FETCH_RETRIES + 1):
                try:
                    commits = [
                        {
                            "sha": c.sha,
                            "parents": [p.sha for p in c.parents],
                            "message": c.commit.message,
                        }
                        for c in pull.get_commits()
                    ]
                    break
                except RateLimitExceededException as rle:
                    if attempt < MAX_FETCH_RETRIES:
                        print(f"Rate limit hit fetching commits for PR #{pull.number} (attempt {attempt + 1}/{MAX_FETCH_RETRIES + 1}).")
                        wait_for_rate_limit_reset(rle)
                    else:
                        print(f"Error: Failed to fetch commits for PR #{pull.number} after {MAX_FETCH_RETRIES + 1} attempts (rate limited).")
                        commits_ok = False
                except Exception as e:
                    if attempt < MAX_FETCH_RETRIES:
                        print(f"Warning: Failed to fetch commits for PR #{pull.number} (attempt {attempt + 1}/{MAX_FETCH_RETRIES + 1}): {e}. Retrying in {retry_delay}s...")
                        time.sleep(retry_delay)
                        retry_delay *= 2
                    else:
                        print(f"Error: Failed to fetch commits for PR #{pull.number} after {MAX_FETCH_RETRIES + 1} attempts: {e}")
                        commits_ok = False

        # Skip merged PRs whose commits could not be fetched —
        # they will be retried on the next resumption run.
        if pull.merged_at and not commits_ok:
            return None

        return {
            "org": org,
            "repo": repo_name,
            "number": pull.number,
            "state": pull.state,
            "title": pull.title,
            "body": pull.body,
            "url": pull.url,
            "id": pull.id,
            "node_id": pull.node_id,
            "html_url": pull.html_url,
            "diff_url": pull.diff_url,
            "patch_url": pull.patch_url,
            "issue_url": pull.issue_url,
            "created_at": datetime_serializer(pull.created_at),
            "updated_at": datetime_serializer(pull.updated_at),
            "closed_at": datetime_serializer(pull.closed_at),
            "merged_at": datetime_serializer(pull.merged_at),
            "merge_commit_sha": pull.merge_commit_sha,
            "labels": [label.name for label in pull.labels],
            "draft": pull.draft,
            "commits_url": pull.commits_url,
            "review_comments_url": pull.review_comments_url,
            "review_comment_url": pull.review_comment_url,
            "comments_url": pull.comments_url,
            "base": pull.base.raw_data,
            "commits": commits,
        }

    # Stage 2: Load existing output to support resumption after interrupts
    existing_pr_ids = set()
    if not output_file_path.parent.exists():
        output_file_path.parent.mkdir(parents=True, exist_ok=True)
    if output_file_path.exists():
        bad_lines = 0
        with open(output_file_path, "r", encoding="utf-8") as f:
            for line_num, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    existing_pr_ids.add(data["id"])
                except (json.JSONDecodeError, KeyError) as e:
                    bad_lines += 1
                    print(f"Warning: Skipping bad line {line_num} in existing output: {e}")
        if bad_lines:
            print(f"Warning: {bad_lines} bad line(s) found in existing output (skipped).")
        print(f"Resuming: {len(existing_pr_ids)} PRs already in output, will skip them.")

    prs_to_process = [pr for pr in final_prs_to_process if pr.id not in existing_pr_ids]
    if not prs_to_process:
        print("All PRs already processed. Nothing to do.")
        return
    print(f"{len(prs_to_process)} PRs to process ({len(existing_pr_ids)} already done).")

    # Stage 3: Process and write incrementally (append mode for crash safety)
    written_count = 0
    with ThreadPoolExecutor(max_workers=num_processing_workers, thread_name_prefix='PR-Processor') as process_executor, \
         open(output_file_path, "a", encoding="utf-8") as out_file:

        future_to_pr = {}
        for pull_obj in tqdm(prs_to_process, desc=f"Submitting PRs for data preparation"):
            future = process_executor.submit(prepare_pull_data, pull_obj)
            future_to_pr[future] = pull_obj.number

        for future in tqdm(as_completed(future_to_pr), total=len(future_to_pr), desc=f"Preparing PR data for {org}/{repo_name}"):
            pr_num = future_to_pr[future]
            try:
                pull_data_dict = future.result()
                if pull_data_dict:
                    out_file.write(json.dumps(pull_data_dict, ensure_ascii=False) + "\n")
                    out_file.flush()
                    written_count += 1
            except Exception as e:
                print(f"Error preparing data for PR #{pr_num}: {e}")

    print(f"Finished. Wrote {written_count} new PRs (total in file: {len(existing_pr_ids) + written_count}).")


if __name__ == "__main__":
    parser = get_parser()
    args = parser.parse_args()

    tokens = get_tokens(args.tokens)

    main(tokens, Path.cwd() / args.out_dir, args.org, args.repo, args.num_processing_workers)
