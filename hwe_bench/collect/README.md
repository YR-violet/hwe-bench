# `collect/` — PR data-collection pipeline

This module turns a GitHub organization / repository pair into a feasibility-scored JSONL of bug-fix pull requests. Its output is the starting point for the `harness/` side of HWE-bench, which then generates testbenches, verifies them in Docker, and assembles the final evaluation dataset.

All outputs land under `datasets/pipeline/<ORG>/<ORG>__<REPO>_sNN_*.jsonl`. Each stage consumes the previous stage's JSONL and writes its own, so the pipeline is a straight chain with no branching.

## Pipeline shape

```
s01 fetch_prs ──► s02 filter_by_issues ──► s03 fetch_issues ──► s04 merge ──► s05 extract_patches
  GitHub API            local + LLM              GitHub API      local (join)    GitHub API
                                                                                       │
                                                                                       ▼
                                                 s08 feasibility  ◄── s07 llm_filter ◄── s06 filter_by_patch
                                                  Codex CLI            Codex CLI           local (size filter)
```

s01, s03, and s05 are the three stages that hit GitHub's REST API. s02 uses a hosted LLM (DeepSeek by default) to extract resolved-issue references. s07 and s08 shell out to the Codex CLI for per-PR classification and feasibility scoring respectively. Everything else is local file processing.

## Quick start

Run the whole pipeline end-to-end against a single repo:

```bash
uv run python -m hwe_bench.collect.pipeline \
  --org lowRISC --repo ibex \
  --tokens tokens.txt \
  --out-dir datasets/pipeline \
  --skip-existing
```

`--tokens` accepts either a path to a token file (one token per line) or multiple values inline (`--tokens tok1 tok2 tok3`). `--skip-existing` makes each stage a no-op if its output file already exists, which is how you resume after an interrupted run. To start part-way through the pipeline, use `--start-from N` where N is the stage number.

Per-stage controls: `--s7-workers` / `--s7-model` and `--s8-workers` / `--s8-model` govern Codex concurrency and model choice for the two LLM stages; `--max-files-changed` / `--max-rows-changed` set the s06 size cutoffs; `--no-llm` forces s02 back to regex-only extraction.

## Credentials and dependencies

**GitHub API tokens** are required for s01, s03, and s05 — at minimum one, but several tokens are strongly recommended because the pipeline shards requests across them for parallelism and rate-limit headroom. Personal access tokens with the default `public_repo` scope are sufficient for public repositories. Pass tokens via `--tokens <file>` (one token per line, blank lines ignored) or inline as `--tokens tok1 tok2 ...`. If the flag is omitted, the pipeline auto-detects a file named `token`, `tokens`, `token.txt`, or `tokens.txt` in the current working directory, so the recommended convention is to keep `tokens.txt` at the project root and run the pipeline from there.

**LLM endpoint for s02** uses `DEEPSEEK_API_KEY` pointed at `https://api.deepseek.com` by default, called through an OpenAI-compatible client. Override with `--llm_base_url`, `--llm_model`, and `--llm_api_key_env` to use a different provider. Pass `--no-llm` to fall back to pure regex extraction, at the cost of lower recall on unconventional issue references.

**Codex CLI for s07 and s08** must be on `PATH` (`npm install -g @openai/codex`). Authentication follows the CLI's own configuration — typically a ChatGPT Pro OAuth stored in `~/.codex/auth.json`. These stages invoke `codex exec -p normal`, so define a `normal` profile in `$CODEX_HOME/config.toml` or `~/.codex/config.toml` before running them. Quota is monitored via `hwe_bench.utils.codex_quota.wait_if_quota_exceeded`, which pauses new task submission when the account approaches its rate limit. The subprocess calls close stdin explicitly, so a non-interactive parent process cannot cause `codex exec` to block while waiting for extra stdin content.

## Stage reference

### s01 — fetch_prs

*Input: none. Output: `<ORG>__<REPO>_s01_prs.jsonl`.*

Paginates `get_pulls(state="all")` across the available tokens and records every PR's metadata. For merged PRs it additionally fetches the commit list so that s02 can scan commit messages locally without another round-trip. Writes are incremental, keyed by PR `id` for dedup on resume. For large repositories this is the longest-running stage (thousands of PRs plus per-PR commit fetches).

### s02 — filter_by_issues

*Input: s01 output. Output: `<ORG>__<REPO>_s02_filtered_prs.jsonl`.*

Local-only stage. Keeps only closed+merged PRs, then extracts referenced issue numbers through one of two paths. The regex path scans title, body, and commit messages for `(fixes|closes|resolves) #N`-style references. The LLM path sends the same text to a DeepSeek-reasoner class model and asks for a JSON array of issue numbers, which catches unconventional references like "related to issue #321" or "see #567 for details" that the regex misses. PRs with no extracted references are dropped.

### s03 — fetch_issues

*Input: s02 output. Output: `<ORG>__<REPO>_s03_issues.jsonl`.*

Unions the issue numbers referenced across s02's surviving PRs and fetches each one's title, body, and state via `get_issue(N)`. Token-sharded, thread-pooled. Unlike the other GitHub stages this one writes its output in a single final pass instead of incrementally, so a mid-run crash requires re-fetching every issue; the cost is typically a few minutes for a few hundred issues, which is why the simpler write path was kept.

### s04 — merge

*Input: s02 and s03 outputs. Output: `<ORG>__<REPO>_s04_merged.jsonl`.*

Pure local join. For each PR in s02, look up each of its referenced issue numbers in s03 and replace the integer list with a list of issue dicts (title, body, state, number). Single-threaded, completes in seconds.

### s05 — extract_patches

*Input: s04 output. Output: `<ORG>__<REPO>_s05_raw_dataset.jsonl`.*

For each PR, calls `GET /repos/<ORG>/<REPO>/compare/<base.sha>...<head.sha>` with `Accept: application/vnd.github.v3.diff` to retrieve the full diff. The diff is split by file path into `fix_patch` (design and source hunks) and `test_patch` (any hunk whose path contains `test`, `tests`, `e2e`, `testing`, `tb`, `tbs`, or `testbench`). Populates `modified_files`, `lines_added`, and `lines_removed`. PRs whose `fix_patch` is empty are dropped. Token-sharded, thread-pooled, incremental writes keyed by PR `number`.

### s06 — filter_by_patch

*Input: s05 output. Output: `<ORG>__<REPO>_s06_filtered_patches.jsonl`.*

Local size filter. Drops PRs where the combined diff touches more than `--max-files-changed` files (default 50) or more than `--max-rows-changed` lines (default 2000).

### s07 — llm_filter

*Input: s06 output if present, otherwise s05. Output: `<ORG>__<REPO>_s07_classified.jsonl`.*

Classifies each PR via Codex CLI into one of three `level1` categories: `RTL_BUG_FIX` (root cause in HDL), `SW_BUG_FIX` (root cause in firmware or software running on the hardware), or `OTHER` (test-only fixes, refactors, features, docs, CI). A finer `level2` subcategory and a `confidence` score in [0, 1] are also returned. The classification rubric lives in `s7_instruction.py`, including the full level2 taxonomy (`RTL_LOGIC` / `RTL_SPEC` / `RTL_INTERFACE` / `RTL_TIMING_SYNC` / `RTL_CONFIG_INTEG` / `RTL_OTHER` on the RTL side; `SW_HW_CONFIG` / `SW_HW_INTERACT` / `SW_FW_LOGIC` / `SW_OTHER` on the SW side), boundary rules for mixed cases, and five anchor examples.

### s08 — feasibility_filter

*Input: s05 raw dataset plus s07 classification. Output: `<ORG>__<REPO>_s08_scored.jsonl`.*

Only processes PRs classified as `RTL_BUG_FIX` or `SW_BUG_FIX` (`OTHER` is skipped). Scores each on four integer dimensions in [0, 2] — `benchmark_value` (how representative the bug is), `cross_layer_depth` (HW-SW understanding required; 0 by construction for RTL fixes), `reproducer_signal` (evidence quality for building a reproducer), `simulation_cost` (expected simulation cost) — plus an enum `reproducer_path` (`existing_test` / `existing_dv` / `minimal_tb` / `full_chip_sw` / `unclear`). The rubric and cross-field consistency rules live in `s8_instruction.py`. A `priority_score = 4·BV + 2·CLD + 6·RS − 3·SC` is also attached to each record as an advisory hint; it is not the decisive ranking — the actual downstream selection logic lives in `harness/tbgen` (s09), which consumes these raw dimensions and applies its own filtering and prioritization.


## Running a single stage

Each stage is also installed as a module entry point for debugging or partial reruns:

```bash
uv run python -m hwe_bench.collect.s1_fetch_prs \
  --out_dir datasets/pipeline/lowRISC \
  --tokens tokens.txt \
  --org lowRISC --repo ibex
```

Use `--help` on any stage to see its flags. The standalone entry points accept the same arguments that `pipeline.py` passes through — filenames, token paths, worker counts, LLM model overrides. Filenames must follow the `<ORG>__<REPO>_sNN_*.jsonl` convention because several stages parse org and repo back out of the input filename.

## Data contract between stages

A PR record accumulates fields as it moves through the pipeline. The key additions at each stage:

- **After s01:** PR metadata — `org`, `repo`, `number`, `state`, `title`, `body`, `base`, created / updated / closed / merged timestamps, `labels`, `commits`.
- **After s02:** `resolved_issues` as a list of integer issue numbers referenced by the PR.
- **After s03 + s04:** `resolved_issues` replaced by a list of issue dicts (`number`, `title`, `body`, `state`).
- **After s05:** `fix_patch`, `test_patch`, `modified_files`, `lines_added`, `lines_removed`.
- **After s07:** `level1`, `level2`, `confidence`, `reasoning`, `model`, `analysis_timestamp`, `raw_output`.
- **After s08:** `benchmark_value`, `cross_layer_depth`, `reproducer_signal`, `simulation_cost`, `reproducer_path`, `priority_score`, plus scoring metadata and reasoning.

Downstream HWE-bench stages (`harness/tbgen`, `harness/verify`, `harness/psgen`) start from the s08 scored JSONL, typically reranked by `priority_score`, and turn the surviving PRs into testbenches, executable reproducers, and problem statements for agent evaluation.
