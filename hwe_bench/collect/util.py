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
import random
import sys
import time
from pathlib import Path

from github import Auth, Github
from github import RateLimitExceededException


def get_github(token: str) -> Github:
    """Create an authenticated GitHub client instance."""
    auth = Auth.Token(token)
    return Github(auth=auth, per_page=100)


def parse_tokens(tokens: str | list[str] | Path) -> list[str]:
    """
    Try to parse tokens as a list of strings.
    """

    if isinstance(tokens, list):
        return tokens
    elif isinstance(tokens, str):
        return [tokens]
    elif isinstance(tokens, Path):
        if not tokens.exists() or not tokens.is_file():
            raise ValueError(f"Token file {tokens} does not exist or is not a file.")
        with tokens.open("r", encoding="utf-8") as file:
            return [line.strip() for line in file if line.strip()]
    return []


def find_default_token_file() -> Path:
    """
    Try to find a default token file in the current directory.
    """

    possible_files = ["token", "tokens", "token.txt", "tokens.txt"]
    for file_name in possible_files:
        file_path = Path(file_name)
        file_path = Path.cwd() / file_path
        if file_path.exists() and file_path.is_file():
            return file_path
    return None


def get_tokens(tokens) -> list[str]:
    if tokens is None:
        default_token_file = find_default_token_file()
        if default_token_file is None:
            print("Error: No tokens provided and no default token file found.")
            sys.exit(1)
        tokens = default_token_file
    else:
        # If tokens are provided as a list with a single element,
        # check if it's a file path before treating it as a token string
        if isinstance(tokens, list) and len(tokens) == 1:
            candidate = tokens[0]
            if Path(candidate).is_file():
                tokens = Path(candidate)
            else:
                tokens = candidate
        # Otherwise pass the list directly

    try:
        token_list = parse_tokens(tokens)
        if not token_list:
            raise ValueError("Token list is empty after parsing.")
    except ValueError as e:
        print(f"Error: {e}")
        sys.exit(1)

    assert token_list, "No tokens provided."
    return token_list


def wait_for_rate_limit_reset(exc: RateLimitExceededException, fallback_seconds: int = 60, max_wait: int = 3700):
    """Wait until GitHub rate limit resets.

    Tries three sources in order:
    1. X-RateLimit-Reset header (primary rate limit)
    2. Retry-After header (secondary / abuse rate limit)
    3. Fallback to fixed wait
    """
    headers = getattr(exc, "headers", None) or {}

    # Try X-RateLimit-Reset (unix timestamp)
    reset_raw = headers.get("X-RateLimit-Reset") or headers.get("x-ratelimit-reset")
    if reset_raw:
        try:
            reset_ts = float(reset_raw)
            wait = reset_ts - time.time() + random.uniform(1, 3)
            if wait > max_wait:
                wait = max_wait
            if wait > 0:
                print(f"Rate limit hit. Waiting {wait:.0f}s until reset ({time.strftime('%H:%M:%S', time.localtime(reset_ts))})...")
                time.sleep(wait)
                return
            # reset_ts already passed, short wait then retry
            time.sleep(random.uniform(1, 3))
            return
        except (ValueError, TypeError):
            pass

    # Try Retry-After (seconds, common for secondary/abuse limits)
    retry_after = headers.get("Retry-After") or headers.get("retry-after")
    if retry_after:
        try:
            wait = min(float(retry_after) + random.uniform(1, 3), max_wait)
            if wait > 0:
                print(f"Rate limit hit (secondary). Waiting {wait:.0f}s (Retry-After)...")
                time.sleep(wait)
                return
        except (ValueError, TypeError):
            pass

    print(f"Rate limit hit. Waiting {fallback_seconds}s (fallback)...")
    time.sleep(fallback_seconds)


def optional_int(value):
    if value.lower() == "none" or value.lower() == "null" or value == "":
        return None
    try:
        return int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"Invalid integer value: {value}")
