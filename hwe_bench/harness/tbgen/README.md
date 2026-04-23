# tbgen (s09)

`tbgen/` is the s09 stage that turns collect-stage PR records into candidate benchmark reproducers. It filters scored candidates, connects each PR to the repository-specific harness, ensures the shared base Docker image exists, prepares per-PR working artifacts, renders the prompt used by Codex, and records the generated runtime scripts after they demonstrate base FAIL and fix PASS. The input is an s08 scored JSONL plus the s05 raw PR data; the output is an s09 JSONL with `tb_script` and `prepare_script` fields populated for successful cases.

The stage has three steps:

1. `prepare_input.py` filters and joins s08 scores with s05 raw records.
2. `run_batch.py` prepares the repository base image, renders the prompt template, and asks Codex to generate and validate scripts per PR.
3. `merge_results.py` folds successful scripts from artifact directories back into a JSONL.

## Codex Profile

`run_batch.py` launches `codex exec -p normal` through the shared `codex_batch.py` helper. Define a `normal` profile in `$CODEX_HOME/config.toml` or `~/.codex/config.toml` before running tbgen. The subprocess closes stdin explicitly, so it does not inherit non-terminating input from the parent process.

## Prepare Input

`prepare_input.py` reads the s08 scored file, keeps records with `status == "ok"`, applies optional filters, and joins each kept score record with the matching s05 raw PR record by `(org, repo, number)`.

```bash
uv run python -m hwe_bench.harness.tbgen.prepare_input \
  --scored datasets/pipeline/<ORG>/<ORG>__<REPO>_s08_scored.jsonl \
  --raw datasets/pipeline/<ORG>/<ORG>__<REPO>_s05_raw_dataset.jsonl \
  --output datasets/pipeline/<ORG>/<ORG>__<REPO>_s09_tbgen_input.jsonl \
  --min-bv 1 \
  --min-rs 1
```

Common filters:

| Option | Meaning |
|--------|---------|
| `--min-bv` | Minimum `benchmark_value` |
| `--min-rs` | Minimum `reproducer_signal` |
| `--max-sc` | Maximum `simulation_cost` |
| `--min-lines` / `--max-lines` | Bounds on added plus removed lines from s05 |
| `--max-files` | Maximum number of modified files |
| `--exclude-path` | Reproducer paths to exclude, such as `full_chip_sw` |
| `--min-sw-cld` | Minimum `cross_layer_depth` for `SW_BUG_FIX` records |

The output preserves the original raw PR fields and carries over the s08 fields used downstream: `level1`, `level2`, `benchmark_value`, `cross_layer_depth`, `reproducer_signal`, `simulation_cost`, `reproducer_path`, and `priority_score`.

## Run tbgen

`run_batch.py` first selects the repository harness from `hwe_bench/harness/repos/`, then ensures the shared base Docker image exists. The repo harness supplies the base image definition, default prepare flow, runtime paths, and test marker parser used later by `docker_runner`. The base image is shared across all PRs in the run; per-PR images are built later from the generated `prepare_script`.

After the base image is available, `run_batch.py` prepares one artifact directory per PR, renders the repository-specific prompt template, and dispatches Codex. The prompt asks Codex to generate a real `tb_script.sh`, optionally generate a `prepare_script.sh`, validate base FAIL and fix PASS in Docker, and write `result.json`.

```bash
uv run python -m hwe_bench.harness.tbgen.run_batch \
  --input datasets/pipeline/<ORG>/<ORG>__<REPO>_s09_tbgen_input.jsonl \
  --org <ORG> \
  --repo <REPO> \
  --out-root artifacts/s09_tbgen \
  --num-workers 1
```

By default, `run_batch.py` selects the prompt from `tbgen/prompts/{org}__{repo}.md`. Use `--prompt-template` for a custom prompt, `--only 123,456` for a small subset, and `--force` to rerun cases that already have `result.json` with `status == "success"`.

The supported repositories are hardcoded in `run_batch.py` through `_make_base_image()` and `_default_prompt_template()`. Adding a new repository requires adding its repo harness under `hwe_bench/harness/repos/`, registering the base-image class there, and adding a matching tbgen prompt template.

Artifacts are written under:

```text
artifacts/s09_tbgen/{org}__{repo}/pr-{N}/
```

Important files:

| File | Purpose |
|------|---------|
| `pr_meta.json` | Metadata and paths rendered into the prompt |
| `fix.patch` | Ground-truth fix patch used for fix PASS validation |
| `prompt_rendered.md` | Exact prompt passed to Codex |
| `result.json` | Codex result with `status`, scripts, and failure details |
| `tb_script.sh` | Generated runtime script, copied from successful `result.json` |
| `prepare_script.sh` | Optional prepare override |
| `logs/docker_build.txt` | Container setup log saved by Codex |
| `logs/test_run_base.txt` | Base run log |
| `logs/test_run_fix.txt` | Fix run log |

`result.json` is the authoritative output consumed by the merge step. The script files are useful for inspection and reruns, but `merge_results.py` reads script content from `result.json`.

## Merge Results

After tbgen finishes, merge successful scripts back into the JSONL:

```bash
uv run python -m hwe_bench.harness.tbgen.merge_results \
  --input datasets/pipeline/<ORG>/<ORG>__<REPO>_s09_tbgen_input.jsonl \
  --artifacts-root artifacts/s09_tbgen \
  --output datasets/pipeline/<ORG>/<ORG>__<REPO>_s09_tbgen_output.jsonl \
  --success-only
```

Without `--success-only`, records without a successful `result.json` are still written with empty or existing script fields. The standard construction path uses `--success-only` so that s10 verify only sees cases that produced a candidate test.

## Prompt Files

Repository-specific prompt templates live in `tbgen/prompts/`. `run_batch.py` renders them with values such as `{PR_DIR}`, `{REPO_ROOT}`, `{ORG}`, `{REPO}`, `{NUMBER}`, `{BASE_SHA}`, and `{BASE_IMAGE}`. The rendered prompt is saved as `prompt_rendered.md` in the per-PR artifact directory before Codex runs.

The prompts encode environment facts, preferred reproducer shape, result schema, and repository-specific constraints. They also explain how Codex should use the repo harness: start from the provided base image, work inside the repository path used by that harness, write `tb_script.sh`, write `prepare_script.sh` only when the default prepare flow is insufficient, and validate base FAIL / fix PASS before writing `result.json`.

OpenTitan prompts require VCS-backed `dvsim` or VCS/FuseSoC flows; XiangShan and rocket-chip prompts prefer focused Chisel/Mill or SBT flows over full-system simulation when a smaller reproducer is valid.