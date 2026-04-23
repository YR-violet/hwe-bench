You are fixing a `tb_script.sh` for an HWE-bench case that was confirmed as a **false negative** during fine-grained investigation.

## Background

HWE-bench evaluates AI coding agents on RTL/Verilog bug-fixing tasks. The hidden `tb_script.sh` verifies agent fixes via fail-to-pass (f2p): it must FAIL on the buggy baseline and PASS after a correct fix is applied. This case's tb_script incorrectly rejects valid fixes.

## Your Input

The case directory is `{CASE_DIR}`. Read **all** of these files:

- `detailed_verdict.json` — Stage 2 investigation result, including `issue_type`, `recommended_action`, and `confirmed_correct_patches`
- `coarse_audit.json` — aggregated Stage 1 coarse verdicts, including any `hack` / `suspicious` patches to use as negative controls
- `multi_agent_matrix.json` — pass/fail matrix and per-agent patch summaries
- `tb_script.sh` — the current (broken) testbench script
- `golden_patch.diff` — the ground-truth fix
- `problem_statement.md` — the bug description
- `agents/*/patch.diff` — patches from all agents (some confirmed correct in Stage 2)
- `agents/*/report.json` and `agents/*/fix-patch-run.log` — per-agent f2p evidence, if present

For OpenTitan cases, you also have access to the Docker container environment. If you need to test the fix, use:

```bash
# Spin up a container from the pre-built image
CTR="audit-fix-{NUMBER}-$$"
docker run -d --rm --init --name "$CTR" \
  {DOCKER_IMAGE} tail -f /dev/null

# Test tb_script against baseline (should FAIL)
docker exec "$CTR" bash -c 'cd /home/{REPO} && git reset --hard && bash {TB_SCRIPT_PATH}'

# Test tb_script against golden fix (should PASS)
docker exec "$CTR" bash -c 'cd /home/{REPO} && git reset --hard && git apply {GOLDEN_PATCH_PATH} && bash {TB_SCRIPT_PATH}'
```

Or use the standard harness helper:
```bash
{PYTHON_BIN} {RUN_CASE} --case-dir {CASE_DIR}
```

The runner prepares `case.json`, `prepare_script.sh`, `fix.patch`, and `test.patch` in `{CASE_DIR}` so `run_case.py` is usable as-is.

## Fix Requirements

### What to fix

Based on `detailed_verdict.json`'s `issue_type`:

- **stale_scoreboard / stale_reg_model**: patch the stale DV file in tb_script (inject the necessary DV update before running dvsim), OR rewrite to avoid depending on the stale component
- **infrastructure_failure**: fix logging (use `tee` instead of redirect, add `trap` for early exit diagnostics), fix Python path (`python3` not `python`), fix `set -e` scope
- **log_redirect**: ensure failure diagnostics are visible in fix-patch-run.log, not swallowed by redirects
- **test_hole**: strengthen the test to also reject incorrect fixes (add negative-case checks)
- **golden_impl_coupling**: rewrite to test observable behavior instead of implementation detail

### What NOT to do

1. **Do not introduce new implementation coupling.** The fixed tb_script must work for ANY functionally equivalent correct fix, not just the golden patch. Before finalizing, ask yourself: "if an agent fixed the same bug with different signal names, different FSM encoding, or different file organization, would this tb_script still correctly judge PASS?"

2. **Do not weaken the test.** The fix must still correctly reject patches that do NOT fix the bug. If `detailed_verdict.json` lists any `hack` or `suspicious` patches from Stage 1, verify that those would still FAIL (or at least not PASS) with your fix.

3. **Do not modify files outside `tb_script_fixed.sh`.** In particular:
   - Do NOT modify `prepare_script.sh` to pre-patch DV files (this breaks f2p semantics — base FAIL would become compile-FAIL instead of behavior-FAIL)
   - Do NOT modify the golden patch or problem statement
   - Only write to `tb_script_fixed.sh` and `fix_report.json`

4. **Do not use static file checks.** The tb_script must verify behavior through real compilation and simulation (VCS/dvsim), not by grepping source files.

5. **Do not treat existing golden-specific constructs as untouchable.** If `tb_script.sh` already contains source-text greps against golden edits, hardcoded commit SHAs, or file paths specific to the golden patch's layout, you are responsible for removing them alongside your new fix. The review stage evaluates the final `tb_script_fixed.sh` as a whole; inherited coupling still counts as coupling and will cause rejection.

6. **Do not pick the narrower assertion when a broader behavioral invariant is available.** Stage 2's `recommended_action` often offers multiple options, e.g. "assert releaseIdBase equals N" vs. "assert the two source-ID ranges do not intersect". Always choose the most foundational invariant that still rejects the flagged incorrect patches. Narrow assertions that encode a specific partition scheme, field value, or structural choice from the golden patch create new false-negative risk: they reject functionally equivalent fixes that chose a different decomposition.

7. **Do not split an end-to-end test into isolated per-endpoint tests.** If the bug is an end-to-end path (producer → forwarder → consumer), the fix must still exercise the full path in one simulation run. Replacing a single integrated test with a pair or set of leaf tests that each check one endpoint in isolation is forbidden: a patch can satisfy each leaf while leaving the intermediate wiring broken, creating a new test hole. When in doubt, instantiate the lowest common ancestor module that contains all endpoints.

### Verification checklist

Before writing `fix_report.json`, you **must** run the Docker verification described in the input section and populate `verification.base_fail`, `verification.golden_pass`, and `verification.agent_patches_tested` with **actually observed** container results. Static reasoning alone is not sufficient — the review stage will independently re-run `run_case.py` and any gap between your claimed verification and live results will cause rejection.

Each checked outcome below must be backed by a real container run:

- [ ] `base` (no patch applied) → tb_script FAILs with at least one `TEST: ... FAIL`
- [ ] `golden_patch` applied → tb_script PASSes with at least one `TEST: ... PASS`
- [ ] At least one confirmed-correct agent patch (from `confirmed_correct_patches`) → tb_script PASSes
- [ ] If a known hack/suspicious patch exists, it does NOT pass (best effort)

If any of the first two outcomes cannot be demonstrated in an actual container run (timeout, image unavailable, infrastructure error, etc.), emit `status="unfixable"` with an explanation rather than `status="fixed"`. Do not claim `base_fail=true` or `golden_pass=true` unless you have observed it live in a container.

## Output

Write the fixed tb_script to `{CASE_DIR}/tb_script_fixed.sh`.

Write `{CASE_DIR}/fix_report.json`:

```json
{
  "number": 0,
  "issue_type": "from detailed_verdict.json",
  "fix_description": "1-3 sentences: what was changed and why",
  "changes_summary": "specific lines/sections modified in tb_script",
  "verification": {
    "base_fail": true,
    "golden_pass": true,
    "agent_patches_tested": [
      {"agent": "sonnet", "expected": "pass", "actual": "pass"},
      {"agent": "opus", "expected": "pass", "actual": "pass"}
    ],
    "hack_patches_tested": []
  },
  "coupling_self_check": "explanation of why this fix does not introduce new coupling",
  "status": "fixed"
}
```

If the fix cannot be done without introducing coupling or breaking f2p semantics, write:

```json
{
  "number": 0,
  "issue_type": "from detailed_verdict.json",
  "fix_description": "why the fix is not feasible",
  "status": "unfixable",
  "recommended_action": "remove_from_dataset or redesign_needed"
}
```
