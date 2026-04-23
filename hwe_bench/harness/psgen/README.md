# psgen (s11)

`psgen/` is the s11 stage that creates the `problem_statement` shown to evaluation agents. It starts from s10 verified cases, generates a concise issue-style description, reviews it for clarity and leakage, and merges the final text into the s11 eval-ready JSONL.

The stage has three actions:

1. `generate` writes `problem_statement.json`.
2. `review` writes `problem_statement_reviewed.json`.
3. `merge` folds the reviewed statement and s10 verification report fields into the output JSONL.

Use `all` to run the three actions in order.

## Codex Profile

`run_batch.py` launches `codex exec -p normal`. Define a `normal` profile in `$CODEX_HOME/config.toml` or `~/.codex/config.toml` before running psgen. The subprocess closes stdin explicitly, so it does not inherit non-terminating input from the parent process.

## Generate

```bash
uv run python -m hwe_bench.harness.psgen.run_batch generate \
  --org <ORG> \
  --repo <REPO> \
  --input datasets/pipeline/<ORG>/<ORG>__<REPO>_s10_verified.jsonl \
  --artifacts-root artifacts/s10_verify/<ORG>__<REPO> \
  --repo-root $(pwd) \
  --num-workers 4
```

`--repo-root` is rendered into the prompt as `{REPO_ROOT}`. Pass the repository root explicitly if running psgen from another directory.

Generation prompts read the s10 case artifact directory, including `case.json`, `fix.patch`, `result.json`, and `pr_meta.json`. They may also use public PR or issue context. The output must be written to:

```text
artifacts/s10_verify/<ORG>__<REPO>/pr-<N>/problem_statement.json
```

## Review

```bash
uv run python -m hwe_bench.harness.psgen.run_batch review \
  --org <ORG> \
  --repo <REPO> \
  --input datasets/pipeline/<ORG>/<ORG>__<REPO>_s10_verified.jsonl \
  --artifacts-root artifacts/s10_verify/<ORG>__<REPO> \
  --repo-root $(pwd) \
  --num-workers 4
```

Review prompts check that the generated problem statement is self-contained, behavior-level, aligned with the verified test, and free of patch or test leakage. The reviewed output is written to:

```text
artifacts/s10_verify/<ORG>__<REPO>/pr-<N>/problem_statement_reviewed.json
```

`merge` prefers the reviewed file when it exists and falls back to the generated file otherwise.

## Merge

```bash
uv run python -m hwe_bench.harness.psgen.run_batch merge \
  --org <ORG> \
  --repo <REPO> \
  --input datasets/pipeline/<ORG>/<ORG>__<REPO>_s10_verified.jsonl \
  --artifacts-root artifacts/s10_verify/<ORG>__<REPO> \
  --output datasets/pipeline/<ORG>/<ORG>__<REPO>_s11_eval_ready.jsonl
```

The merge step writes `problem_statement` into each JSONL record. It also copies selected fields from the s10 `report.json` into the output record: `run_result`, `test_patch_result`, `fix_patch_result`, `fixed_tests`, `p2p_tests`, `f2p_tests`, `s2p_tests`, and `n2p_tests`.

If `--output` is omitted, the script replaces `s10_verified` with `s11_eval_ready` in the input path.

## Run All Stages

```bash
uv run python -m hwe_bench.harness.psgen.run_batch all \
  --org <ORG> \
  --repo <REPO> \
  --input datasets/pipeline/<ORG>/<ORG>__<REPO>_s10_verified.jsonl \
  --artifacts-root artifacts/s10_verify/<ORG>__<REPO> \
  --repo-root $(pwd) \
  --output datasets/pipeline/<ORG>/<ORG>__<REPO>_s11_eval_ready.jsonl \
  --num-workers 4
```

Use `--only 123,456` for a subset and `--force` to regenerate or rereview cases that already have valid output files.

## Prompt Files

Prompt templates live in `psgen/prompts/` and are named `{org}__{repo}_generate.md` and `{org}__{repo}_review.md`.

The generate prompts ask for a self-contained bug report covering four elements: observed behavior, expected behavior, affected function, and trigger condition. The review prompts check semantic completeness, ambiguity, behavior-level alignment with the verified test, information granularity, and leakage.

The downstream repair agent sees only the final `problem_statement`; it does not see `fix.patch`, `tb_script`, test names, logs, simulator commands, or construction artifacts.
