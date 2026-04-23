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
from pathlib import Path
import re
import sys
from tqdm import tqdm
import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed

def get_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Filter PRs by resolved issues (local-only, no GitHub API needed)."
    )
    parser.add_argument(
        "--out_dir", type=Path, required=True, help="Output directory path."
    )
    parser.add_argument(
        "--prs_file", type=Path, required=True, help="Path to PRs JSONL file (with commits from step 1)."
    )
    parser.add_argument(
        "--num_workers",
        type=int,
        default=min(32, os.cpu_count() + 4 if os.cpu_count() else 10),
        help="Number of worker threads for processing pull requests.",
    )
    parser.add_argument(
        "--use_llm",
        action="store_true",
        default=True,
        help="Use LLM to extract resolved issues (requires DEEPSEEK_API_KEY or configured API). Default: True.",
    )
    parser.add_argument(
        "--no_llm",
        action="store_true",
        default=False,
        help="Disable LLM and use regex-only extraction.",
    )
    parser.add_argument(
        "--llm_base_url",
        type=str,
        default=None,
        help="OpenAI-compatible API base URL for LLM issue extraction (default: https://api.deepseek.com).",
    )
    parser.add_argument(
        "--llm_model",
        type=str,
        default=None,
        help="Model name for LLM issue extraction (default: deepseek-reasoner).",
    )
    parser.add_argument(
        "--llm_api_key_env",
        type=str,
        default=None,
        help="Environment variable name for LLM API key (default: DEEPSEEK_API_KEY).",
    )
    return parser


def extract_resolved_issues(
    pull: dict,
    use_llm: bool = False,
    llm_base_url: str = "https://api.deepseek.com",
    llm_model: str = "deepseek-reasoner",
    llm_api_key_env: str = "DEEPSEEK_API_KEY",
) -> list[int]:
    # Define 1. issue number regex pattern 2. comment regex pattern 3. keywords
    issues_pat = re.compile(r"(\w+)\s+\#(\d+)")
    comments_pat = re.compile(r"(?s)<!--.*?-->")
    keywords = {
        "close",
        "closes",
        "closed",
        "fix",
        "fixes",
        "fixed",
        "resolve",
        "resolves",
        "resolved",
    }

    # Construct text to search over for issue numbers from PR body and commit messages
    text = pull["title"] if pull["title"] else ""
    text += "\n" + (pull["body"] if pull["body"] else "")
    text += "\n" + "\n".join([commit["message"] for commit in pull["commits"]])

    if use_llm:
        issues_str = call_llm_for_issue_extraction(
            text, base_url=llm_base_url, model=llm_model, api_key_env=llm_api_key_env
        )
        # Convert string list to integer list
        resolved_issues = set()
        for issue_str in issues_str:
            try:
                issue_num = int(issue_str)
                if issue_num > 0:
                    resolved_issues.add(issue_num)
            except (ValueError, TypeError):
                continue
        return list(resolved_issues)

    # Remove comments from text
    text = comments_pat.sub("", text)
    # Look for issue numbers in text via scraping <keyword, number> patterns
    resolved_issues = set()
    for word, issue_num in issues_pat.findall(text):
        if word.lower() in keywords:
            resolved_issues.add(int(issue_num))

    if 0 in resolved_issues:
        resolved_issues.remove(0)

    return list(resolved_issues)

def call_llm_for_issue_extraction(
    text: str,
    max_retries: int = 5,
    base_url: str = "https://api.deepseek.com",
    model: str = "deepseek-reasoner",
    api_key_env: str = "DEEPSEEK_API_KEY",
) -> list[str]:
    """Call LLM to extract resolved issue numbers from PR text."""
    from openai import OpenAI  # lazy import: only needed when LLM is enabled

    api_key = os.environ.get(api_key_env)
    if not api_key:
        raise ValueError(f"{api_key_env} environment variable not set")

    client = OpenAI(api_key=api_key, base_url=base_url)
    
    prompt = f"""
You are an AI assistant specialized in analyzing GitHub Pull Requests and Issues. I need you to extract every referenced GitHub issue number from the text below.

Please note the following:
1. Recognize common reference formats such as "fixes #123", "resolves #456", "closes #789", etc.
2. Also recognize less conventional references such as "related to issue #321" or "see #567 for details".
3. Recognize any bare numbers that, from context, are clearly issue numbers.
4. Look for any other patterns that may indicate an issue reference.

Output requirements:
- Return a single JSON object containing exactly one key, `issues`.
- The value of `issues` must be an array of strings; each element is a positive-integer issue number with the `#` stripped (e.g., "123").
- Do not include any other keys, comments, or explanatory text.
- Do not use Markdown code fences; do not add backticks.

Example:
{{"issues": ["123", "456", "789"]}}

Here is the text to analyze:
{text}
"""
    
    for attempt in range(max_retries):
        try:
            response = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                temperature=1,
                response_format={"type": "json_object"},
            )
            
            content = response.choices[0].message.content
            
            cleaned_content = content.strip()
            match_json_block = re.search(r"```json\s*([\s\S]*?)\s*```", cleaned_content, re.IGNORECASE)
            if match_json_block:
                cleaned_content = match_json_block.group(1).strip()
            else:
                match_any_block = re.search(r"```\s*([\s\S]*?)\s*```", cleaned_content)
                if match_any_block:
                    cleaned_content = match_any_block.group(1).strip()
                    
            try:
                issues_data = json.loads(cleaned_content)
                # Prefer object with key "issues"; fall back to array for compatibility
                if isinstance(issues_data, dict):
                    issues_raw = issues_data.get("issues", [])
                elif isinstance(issues_data, list):
                    issues_raw = issues_data
                else:
                    print(f"Unexpected response format: {cleaned_content}")
                    print(f"Retrying... {attempt + 1} of {max_retries}")
                    continue
                
                filtered_issues = []
                for issue in issues_raw:
                    if isinstance(issue, str):
                        issue_str = issue.strip()
                        if issue_str and issue_str.isdigit() and int(issue_str) > 0:
                            filtered_issues.append(issue_str)
                    elif isinstance(issue, int) and issue > 0:
                        filtered_issues.append(str(issue))
                
                seen = set()
                unique_issues = []
                for issue in filtered_issues:
                    if issue not in seen:
                        seen.add(issue)
                        unique_issues.append(issue)
                
                return unique_issues
            except json.JSONDecodeError:
                print(f"Failed to parse JSON: {cleaned_content}")
                print(f"Retrying... {attempt + 1} of {max_retries}")
                continue
        except Exception as e:
            print(f"Error on attempt {attempt + 1}: {e}")
            if attempt < max_retries - 1:
                print(f"Retrying... {attempt + 1} of {max_retries}")
                
    print("Failed to extract issues from LLM")
    return []
        
def process_single_pr(
    pull_data: dict,
    use_llm: bool = False,
    llm_base_url: str = "https://api.deepseek.com",
    llm_model: str = "deepseek-reasoner",
    llm_api_key_env: str = "DEEPSEEK_API_KEY",
) -> dict | None:
    """Process a single pull request: extract resolved issues from local data.

    Only processes merged PRs (state=closed and merged_at is not empty).
    Commits are expected to be already present in pull_data (populated by Step 1).
    """
    if pull_data.get("state") != "closed":
        return None

    if not pull_data.get("merged_at"):
        return None

    # Ensure commits field exists (backward compat with old _prs.jsonl without commits)
    if "commits" not in pull_data:
        pull_data["commits"] = []

    resolved_issues = extract_resolved_issues(
        pull_data,
        use_llm=use_llm,
        llm_base_url=llm_base_url,
        llm_model=llm_model,
        llm_api_key_env=llm_api_key_env,
    )
    if not resolved_issues:
        return None

    pull_data["resolved_issues"] = resolved_issues
    return pull_data


def main(
    out_dir: Path,
    prs_file: Path,
    num_workers: int,
    use_llm: bool = True,
    llm_base_url: str = "https://api.deepseek.com",
    llm_model: str = "deepseek-reasoner",
    llm_api_key_env: str = "DEEPSEEK_API_KEY",
):
    print("Starting local PR filtering (no GitHub API needed)")
    print(f"Output directory: {out_dir}")
    print(f"Input file: {prs_file}")
    print(f"Use LLM: {use_llm}")
    print(f"Number of workers: {num_workers}")

    # Fail fast if LLM is enabled but dependencies are missing
    if use_llm:
        try:
            import openai  # noqa: F401
        except ImportError:
            print("Error: LLM is enabled but 'openai' package is not installed.")
            print("Install it with: pip install openai")
            sys.exit(1)
        api_key = os.environ.get(llm_api_key_env)
        if not api_key:
            print(f"Error: LLM is enabled but {llm_api_key_env} environment variable is not set.")
            print("Set the key or use --no_llm to fall back to regex extraction.")
            sys.exit(1)
        print(f"LLM config: model={llm_model}, base_url={llm_base_url}")

    org_repo_re = re.compile(r"(.+)__(.+)_s01_prs.jsonl")
    m = org_repo_re.match(prs_file.name)
    if not m:
        print(f"Error: Invalid pull file name: {prs_file.name}")
        sys.exit(1)

    org = m.group(1)
    repo = m.group(2)
    print(f"Org: {org}, Repo: {repo}")

    print(f"Loading PRs from {prs_file}...")
    with open(prs_file, "r", encoding="utf-8") as in_file:
        all_prs = [json.loads(line) for line in in_file]
    print(f"Loaded {len(all_prs)} PRs from input.")

    # Load existing output to support resumption after interrupts
    output_file_path = out_dir / f"{org}__{repo}_s02_filtered_prs.jsonl"
    existing_pr_numbers = set()
    if output_file_path.exists():
        bad_lines = 0
        with open(output_file_path, "r", encoding="utf-8") as f:
            for line_num, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    existing_pr_numbers.add(data["number"])
                except (json.JSONDecodeError, KeyError) as e:
                    bad_lines += 1
                    print(f"Warning: Skipping bad line {line_num} in existing output: {e}")
        if bad_lines:
            print(f"Warning: {bad_lines} bad line(s) found in existing output (skipped).")
        print(f"Resuming: {len(existing_pr_numbers)} PRs already in output, will skip them.")

    prs_to_process = [pr for pr in all_prs if pr.get("number") not in existing_pr_numbers]
    if not prs_to_process:
        print("All PRs already processed. Nothing to do.")
        return
    print(f"{len(prs_to_process)} PRs to process ({len(existing_pr_numbers)} already done).")

    # Process and write incrementally (append mode for crash safety)
    written_count = 0
    with ThreadPoolExecutor(max_workers=num_workers, thread_name_prefix='PR-Filter') as executor, \
         open(output_file_path, "a", encoding="utf-8") as out_file:

        future_to_pr_num = {}
        for pull_data_item in tqdm(prs_to_process, desc="Submitting PRs for filtering"):
            future = executor.submit(
                process_single_pr, pull_data_item,
                use_llm, llm_base_url, llm_model, llm_api_key_env,
            )
            future_to_pr_num[future] = pull_data_item.get("number", "unknown")

        for future in tqdm(as_completed(future_to_pr_num), total=len(prs_to_process), desc="Filtering PRs"):
            pr_num = future_to_pr_num[future]
            try:
                result = future.result()
                if result:
                    out_file.write(json.dumps(result, ensure_ascii=False) + "\n")
                    out_file.flush()
                    written_count += 1
            except Exception as exc:
                print(f'PR #{pr_num} generated an exception during processing: {exc}')

    print(f"Finished. Wrote {written_count} new filtered PRs (total in file: {len(existing_pr_numbers) + written_count}).")


if __name__ == "__main__":
    parser = get_parser()
    args = parser.parse_args()

    main(
        args.out_dir, args.prs_file, args.num_workers,
        use_llm=args.use_llm and not args.no_llm,
        llm_base_url=args.llm_base_url or "https://api.deepseek.com",
        llm_model=args.llm_model or "deepseek-reasoner",
        llm_api_key_env=args.llm_api_key_env or "DEEPSEEK_API_KEY",
    )
