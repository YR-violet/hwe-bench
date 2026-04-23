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
from pathlib import Path

from tqdm import tqdm


def get_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="A command-line tool for processing repositories."
    )
    parser.add_argument(
        "--out_dir", type=Path, required=True, help="Output directory path."
    )
    parser.add_argument("--org", type=str, required=True, help="Organization name.")
    parser.add_argument("--repo", type=str, required=True, help="Repository name.")

    return parser


def main(out_dir: Path, org: str, repo: str):
    print("starting merge pull requests with related issues")
    print(f"Output directory: {out_dir}")
    print(f"Org: {org}")
    print(f"Repo: {repo}")

    with open(
        out_dir / f"{org}__{repo}_s02_filtered_prs.jsonl",
        "r",
        encoding="utf-8",
    ) as pull_file:
        filtered_prs = [json.loads(line) for line in pull_file]

    with open(
        out_dir / f"{org}__{repo}_s03_issues.jsonl", "r", encoding="utf-8"
    ) as issue_file:
        issues = {}
        for line in issue_file:
            issue = json.loads(line)
            issues[issue["number"]] = issue

    with open(
        out_dir / f"{org}__{repo}_s04_merged.jsonl", "w", encoding="utf-8"
    ) as file:
        for pull in tqdm(filtered_prs, desc="Merging dataset"):
            resolved_issues = []
            for issue_number in pull["resolved_issues"]:
                # Convert to int to handle both string and int inputs
                try:
                    issue_num = int(issue_number)
                except (ValueError, TypeError):
                    print(f"Warning: Invalid issue number format: {issue_number}")
                    continue
                
                if issue_num not in issues:
                    continue
                resolved_issues.append(issues[issue_num])

            pull["resolved_issues"] = resolved_issues

            file.write(json.dumps(pull, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    parser = get_parser()
    args = parser.parse_args()

    main(args.out_dir, args.org, args.repo)
