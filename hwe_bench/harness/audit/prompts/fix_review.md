You are reviewing a tb_script fix from Stage 3 of the HWE-bench audit pipeline. Your job is **adversarial**: assume the fix might have introduced new problems, and prove it hasn't.

## Background

A `tb_script.sh` was identified as a false negative (incorrectly rejecting valid agent fixes) and has been repaired in Stage 3. You must verify the repair is correct, complete, and does not introduce new issues.

## Your Input

The case directory is `{CASE_DIR}`. Read **all** of these files:

- `tb_script_fixed.sh` — the repaired tb_script (Stage 3 output)
- `tb_script.sh` — the original (broken) tb_script
- `fix_report.json` — Stage 3's self-reported verification results
- `detailed_verdict.json` — Stage 2 investigation (issue_type, confirmed_correct_patches)
- `coarse_audit.json` — aggregated Stage 1 coarse verdicts, including any `hack` / `suspicious` patches for negative control
- `multi_agent_matrix.json` — pass/fail matrix and per-agent patch summaries
- `golden_patch.diff` — ground-truth fix
- `problem_statement.md` — bug description
- `agents/*/patch.diff` — all agent patches

## Review Checklist

### 1. Diff analysis

Compare `tb_script_fixed.sh` against `tb_script.sh`. For each change:
- Is it necessary to fix the identified issue?
- Does it introduce any new dependency on golden-patch-specific implementation details?
- Does it weaken the test (make it easier to pass without actually fixing the bug)?

### 2. Implementation coupling check

Read `tb_script_fixed.sh` end-to-end and check:

- Does it reference internal signals that only exist in the golden fix? (`tb.dut.u_xxx.signal_name`)
- Does it use `.*` wildcard port binding on modules whose interface might change?
- Does it modify shared DV files (scoreboard, interface, vseq_list) in a way that assumes the golden implementation?
- Does it inject DV patches that would break under an alternative correct fix?
- Does it hardcode values derived from the golden patch rather than testing behavior?

**Self-test**: "If an agent fixed the same bug with different signal names, different internal structure, or a different decomposition, would this tb_script still correctly judge PASS?" If the answer is no for any plausible alternative, the fix has coupling.

### 3. Negative control

Check that the fixed tb_script still correctly rejects non-fixes:

- On the buggy baseline (no patch), does the test produce at least one `TEST: ... FAIL`?
- If Stage 1 identified any `hack` or `suspicious` patches, would those patches pass the fixed tb_script? If yes, the fix has created a test hole.

### 4. Positive control

Check that the fixed tb_script correctly accepts valid fixes:

- Does the golden patch produce at least one `TEST: ... PASS`?
- Do the `confirmed_correct_patches` from Stage 2 also produce `TEST: ... PASS`?

### 5. Infrastructure quality

- Does the script have proper error handling? (`set -e` scope, `trap` for diagnostics)
- Are logs visible in fix-patch-run.log? (no silent redirects that swallow failure info)
- Are `HWE_BENCH_RESULTS_START/END` markers present and correctly placed?
- Does the script `cd /home/{REPO}` at the start?

### 6. Stage 3 self-report verification

Cross-check `fix_report.json` against your own analysis:
- Did Stage 3 actually test what it claims to have tested?
- Are the reported verification results consistent with the tb_script changes?
- Is the `coupling_self_check` convincing?

## Verification

If the Docker environment is available, you may run the fixed tb_script to independently verify:

```bash
{PYTHON_BIN} {RUN_CASE} --case-dir {CASE_DIR}
```

The runner prepares `case.json`, `prepare_script.sh`, `fix.patch`, and `test.patch` in `{CASE_DIR}` so `run_case.py` is usable as-is. If you need to run the fixed script, temporarily copy `{TB_SCRIPT_FIXED_PATH}` over `{TB_SCRIPT_PATH}` inside `{CASE_DIR}` before invoking the helper.

## Output

Write `{CASE_DIR}/review_result.json`:

```json
{
  "number": 0,
  "review_verdict": "approve|reject|conditional_approve",
  "coupling_found": false,
  "test_hole_found": false,
  "infrastructure_issues": [],
  "concerns": [
    "specific concern about the fix, if any"
  ],
  "conditions": [
    "for conditional_approve: what must be verified before merging"
  ],
  "summary": "1-3 sentence overall assessment"
}
```

### Verdict definitions

- **approve**: fix is correct, no coupling, no test holes, ready to merge
- **reject**: fix introduces new coupling, creates test holes, or breaks f2p semantics. Include specific evidence in `concerns`.
- **conditional_approve**: fix looks correct but needs runtime verification that wasn't possible in this review (e.g., need to actually run in Docker to confirm). List what needs to be verified in `conditions`.
