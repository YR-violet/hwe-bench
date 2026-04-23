# audit

`audit/` is the post-evaluation audit pipeline. It reviews completed agent runs from two angles:

1. whether the agent behavior and patch look legitimate, suspicious, or test-targeting
2. whether a benchmark `tb_script` rejected a correct fix, accepted a weak fix, or otherwise needs repair

The audit pipeline consumes dataset records, agent patches, Harbor trajectories, evaluator reports, and runtime logs. It does not replace normal evaluation; it is a follow-up pass used to validate benchmark quality.

## Stages

The pipeline has four stages plus one bundling step.

| Stage | Entry point | Purpose | Main output |
|-------|-------------|---------|-------------|
| Stage 1 | `run_batch.py` | Coarse audit of one agent on one dataset | `final_audit_report.json` |
| Stage 1.5 | `bundle_flagged.py` | Bundle flagged cases across agents | `artifacts/audit_investigation/{org}__{repo}/pr-N/` |
| Stage 2 | `run_investigation.py --stage investigate` | Detailed multi-agent diagnosis | `detailed_verdict.json` |
| Stage 3 | `run_investigation.py --stage fix` | Generate a case-local `tb_script` fix | `tb_script_fixed.sh`, `fix_report.json` |
| Stage 4 | `run_investigation.py --stage review` | Adversarial review of the fixed tb script | `review_result.json` |

All Codex calls use `codex exec -p normal` and close stdin explicitly. Define a `normal` profile in `$CODEX_HOME/config.toml` or `~/.codex/config.toml` before running audit.

## Stage 1: Coarse Audit

Stage 1 audits one agent run against one dataset. It collects per-case material into batches, then asks Codex to inspect trajectory behavior and patch validity.

```bash
uv run python -m hwe_bench.harness.audit.run_batch \
  --dataset datasets/pipeline/<ORG>/<ORG>__<REPO>_s11_eval_ready.jsonl \
  --patches results/<run-name>/patches/patches.jsonl \
  --jobs jobs/<run-name> \
  --eval-root results/<run-name>/eval_workdir \
  --final-report results/<run-name>/eval/final_report.json \
  --org <ORG> \
  --repo <REPO> \
  --agent <agent-label> \
  --dataset-name <dataset-label> \
  --batch-size 10 \
  --num-workers 1 \
  --resume
```

`--eval-root` must point at the evaluator workdir that contains per-case reports:

```text
<eval-root>/<org>/<repo>/evals/pr-N/report.json
<eval-root>/<org>/<repo>/evals/pr-N/fix-patch-run.log
```

Do not pass the evaluator `output_dir`; that directory only contains aggregate reports. If per-case reports are missing, Stage 1 cannot reliably distinguish resolved, unresolved, incomplete, and false-negative cases. `--final-report` can be used to provide the aggregate `final_report.json` explicitly when it is not under the legacy `<eval-root>/output/` path.

Stage 1 output is written under:

```text
artifacts/audit/{org}__{repo}/{agent}__{dataset-name}/
```

Important files:

| File or directory | Purpose |
|-------------------|---------|
| `run_meta.json` | Agent, repo, dataset, and batch metadata |
| `cases/pr-N/` | Raw per-case material copied for audit |
| `batches/batch-*/batch_manifest.json` | Batch input manifest |
| `batches/batch-*/batch_result.json` | Codex coarse audit result |
| `final_audit_report.json` | Aggregated Stage 1 report |
| `false_negative_queue.jsonl` | Cases likely requiring tb_script investigation |
| `manual_review_queue.jsonl` | Cases needing manual review |

## Stage 1.5: Bundle Flagged Cases

Stage 1.5 combines Stage 1 outputs from multiple agents. It selects cases where at least one agent received a non-safe patch verdict and writes a multi-agent case bundle for detailed investigation.

```bash
uv run python -m hwe_bench.harness.audit.bundle_flagged \
  --audit-root artifacts/audit/<ORG>__<REPO> \
  --dataset datasets/pipeline/<ORG>/<ORG>__<REPO>_s11_eval_ready.jsonl \
  --patches-dir results \
  --jobs-dir jobs \
  --eval-root-pattern "$(pwd)/results/hwe-<repo>-{agent}/eval_workdir" \
  --org <ORG> \
  --repo <REPO> \
  --dataset-name <dataset-label> \
  --output artifacts/audit_investigation
```

`--dataset-name` filters Stage 1 runs by `run_meta.json["dataset"]`. Use it when the audit root contains both full and curated dataset runs.

`--eval-root-pattern` may contain `{agent}` and `{dataset}`. The bundler tries the literal agent label and known short keywords such as `sonnet`, `opus`, `kimi`, `codex`, `deepseek`, `ds`, `qwen`, and `gemini`. If adding a new agent name, check `_agent_keywords()` in `bundle_flagged.py`.

Bundle output:

```text
artifacts/audit_investigation/{org}__{repo}/
  flagged_cases.json
  flagged_cases.jsonl
  pr-N/
    case.json
    case_manifest.json
    problem_statement.md
    tb_script.sh
    prepare_script.sh
    golden_patch.diff
    coarse_audit.json
    multi_agent_matrix.json
    agents/<agent-key>/
      patch.diff
      trajectory.json
      report.json
      fix-patch-run.log
```

`multi_agent_matrix.json` is the quickest file for understanding which agents passed, failed, were flagged, or lacked artifacts.

## Stages 2-4: Investigation, Fix, Review

`run_investigation.py` operates on a Stage 1.5 bundle.

Run investigation:

```bash
uv run python -m hwe_bench.harness.audit.run_investigation \
  --bundle-dir artifacts/audit_investigation/<ORG>__<REPO> \
  --stage investigate \
  --num-workers 1
```

Run tb_script fixes for eligible cases:

```bash
uv run python -m hwe_bench.harness.audit.run_investigation \
  --bundle-dir artifacts/audit_investigation/<ORG>__<REPO> \
  --stage fix \
  --num-workers 1
```

Review generated fixes:

```bash
uv run python -m hwe_bench.harness.audit.run_investigation \
  --bundle-dir artifacts/audit_investigation/<ORG>__<REPO> \
  --stage review \
  --num-workers 1
```

Use `--stage all` to run investigate, fix, and review in order. Use `--only 123,456` for a subset. Use `--force` to rerun completed outputs.

Stage behavior:

| Stage | Eligibility | Output |
|-------|-------------|--------|
| `investigate` | Any bundled case | `detailed_verdict.json` |
| `fix` | `detailed_verdict.json` has `fixability == "case_local_fix"` and is not `genuine_fix` | `tb_script_fixed.sh`, `fix_report.json` |
| `review` | `fix_report.json` has `status == "fixed"` | `review_result.json` |

Stage 3 and Stage 4 prompts may call `verify/run_case.py` through the rendered `{RUN_CASE}` path. If running from outside the repo root, pass `--repo-root`.

## Prompt Roles

| Prompt | Role |
|--------|------|
| `prompts/audit.md` | Stage 1 coarse trajectory and patch review |
| `prompts/fine_investigation.md` | Stage 2 detailed diagnosis and fixability classification |
| `prompts/fix_tb_script.md` | Stage 3 case-local tb_script repair |
| `prompts/fix_review.md` | Stage 4 adversarial review of a repaired tb_script |

The Stage 2 prompt treats the existing `tb_script` as correct by default. Agent failure alone is not evidence of a testbench bug. A tb-side issue must be supported by concrete evidence such as a semantically correct patch being rejected, a test hole, a scope mismatch, stale DV collateral, or an infrastructure failure.

## Rerun Notes

The investigation runner removes the current stage output before rerunning that stage, but it does not quarantine arbitrary backup files in a case directory. Codex can read all files in the case directory. If a previous manual rerun left files such as `*.stage*-v*`, `*.orig*`, or backup result files, move them out of the case directory before rerunning.

For bundles, avoid mixing old and new Stage 1 runs under the same `audit-root` unless `--dataset-name` filters them cleanly. Stale Stage 1 outputs can produce bundles with the wrong dataset label, wrong eval roots, or incomplete status data.

## Interpreting Results

Safe Stage 1 patch verdicts are `genuine_fix` and `true_unresolved`. Other verdicts are bundled for investigation.

Common Stage 2 `issue_type` values include:

| issue_type | Meaning |
|------------|---------|
| `correct_unresolved` | Agent patches are wrong or incomplete; tb_script is likely fine |
| `test_hole` | tb_script accepts a weak or irrelevant patch |
| `golden_impl_coupling` | tb_script is tied to the golden implementation rather than behavior |
| `scope_mismatch` | tb_script tests a different behavior than the problem statement |
| `stale_scoreboard` / `stale_reg_model` | DV collateral rejects otherwise correct patches |
| `infrastructure_failure` | Timeout, environment, logging, or setup problem |
| `remove_from_dataset` | Case is unfair or untestable |

Only merge a repaired `tb_script` after Stage 4 approves it, or after a manual review accepts the remaining risk.
