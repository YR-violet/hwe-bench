You are performing **fine-grained investigation** on a single HWE-bench case that was flagged during coarse audit.

## Background

HWE-bench evaluates AI coding agents on RTL/Verilog bug-fixing tasks. Each agent receives only a `problem_statement` and must fix the bug inside a Docker container. A hidden `tb_script` verifies the fix via fail-to-pass (f2p) testing. This case was flagged during Stage 1 (coarse audit) as potentially suspicious, a false negative, or needing investigation. Your task is to **validate or overturn** the Stage 1 flag with fine-grained evidence — not to confirm it. Many flagged cases reflect genuine agent failure on hard problems rather than tb_script defects; treat the tb_script as correct until Step 2–4 evidence actively contradicts it.

## Your Input

The case bundle is at `{CASE_DIR}`. Read **all** of these files:

**Core artifacts:**
- `problem_statement.md` — the bug description given to agents
- `tb_script.sh` — the hidden testbench used for f2p verification
- `golden_patch.diff` — the ground-truth fix from the original PR
- `coarse_audit.json` — the aggregated Stage 1 coarse audit summary, with one entry per agent (not a single-agent report)

**Per-agent artifacts** (in `agents/{agent_name}/`):
- `patch.diff` — agent's proposed fix
- `trajectory.json` — full execution trace (if available)
- `report.json` — f2p verification result (if available)
- `fix-patch-run.log` — simulation log from f2p (if available)

**Summary:**
- `multi_agent_matrix.json` — lightweight matrix: per-agent resolved/unresolved status, files modified, RTL-vs-DV breakdown

## Required Analysis (follow this order)

### Step 1: Multi-agent pass/fail boundary

Read `multi_agent_matrix.json`. Identify which agents passed and which failed. This is the single most diagnostic signal — it reveals whether the issue is agent-specific or tb_script-systemic. This step also gates which `issue_type` classifications are admissible downstream.

Treat the tb_script as correct by default. An agent failure, even a unanimous one, is *not* by itself evidence of a tb_script bug — many RTL problems are genuinely hard, and a correct tb will reject attempts that miss the bug. Classifying this case as a tb-side issue (`infrastructure_failure`, `log_redirect`, `golden_impl_coupling`, `scope_mismatch`, `stale_scoreboard`, `stale_reg_model`, `test_hole`) requires concrete contradicting evidence from Step 2–4.

- If **all agents fail** (0 pass): the leading hypothesis is `correct_unresolved` — the problem is genuinely hard and no agent produced a sufficient fix. Only switch to a tb-side classification when Step 2–4 surface concrete evidence such as: at least one failing patch is semantically equivalent to the golden fix but still rejected (→ `golden_impl_coupling`); the failure mode is compile timeout, class-name collision, or missing dependency not attributable to the agent's work (→ `infrastructure_failure`); or the tb exercises a code path disjoint from the problem statement (→ `scope_mismatch`). Absence of any passing agent is weak evidence for a tb defect — it is entirely consistent with a hard benchmark case.
- If **near-0 pass** (exactly 1 of 6): weak `golden_impl_coupling` remains possible, but first verify the single passing patch honestly implements the described behavior rather than hacking the test. A lone passer that exploits a test hole does not rule out coupling; a lone passer that implements the behavior correctly is positive evidence that the problem is tractable and the other 5 simply failed.
- If **all agents pass**: candidate is `test_hole` (underconstrained tb_script).
- If **split with 2+ passing agents**: **`golden_impl_coupling` is excluded as an `issue_type` for this case**, regardless of how semantically correct any failing patches appear. The tb_script has demonstrated it can admit at least two independent fixes, so implementation-detail binding to the golden patch is ruled out by construction. Failing patches in this regime must be classified as `true_unresolved`, `suspicious`, or `hack` based on trajectory/patch analysis, and `confirmed_correct_patches` must be left empty. The remaining admissible `issue_type` values are `test_hole`, `scope_mismatch`, `infrastructure_failure`, `stale_scoreboard`, `stale_reg_model`, `log_redirect`, or `correct_unresolved`.

### Step 2: Trace tb_script failure path

Read `tb_script.sh` and trace what it actually checks. For failing agents, identify the exact line/condition where the test fails. Ask:
- Is it testing the stated bug behavior?
- Is it testing a stale DV assumption (scoreboard, reg model, interface)?
- Is it testing a golden-implementation-specific detail?
- Is it failing due to infrastructure (compile error, log redirect, set -e early exit)?

### Step 3: Semantic patch comparison

Compare each agent's patch against the golden patch. Focus on **semantic equivalence**, not textual similarity:
- Do they fix the same root cause?
- Do they produce the same observable behavior change?
- If an agent also modifies DV/scoreboard/test files, is that necessary for correctness or just for passing the hidden test?

### Step 4: Trajectory context

For the most interesting agents (the one that uniquely passes, or the one that uniquely fails, or for a 0-pass case the agent with the closest-looking attempt), read their trajectory to understand:
- Did the agent intentionally limit scope (RTL-only), or did it not realize DV needed updating?
- Did the agent use any suspicious information sources?
- For 0-pass cases specifically: did the agent reach a plausible-but-insufficient attempt (indicating a hard problem where close-miss is expected), or did the agent produce something structurally equivalent to the golden fix that was then rejected (indicating a tb-side issue)? Only the latter is evidence against `correct_unresolved`.

## Output

Write `{CASE_DIR}/detailed_verdict.json`:

```json
{
  "number": 0,
  "coarse_summary": {
    "flagged_by_agents": ["sonnet", "opus"],
    "verdict_counts": {"high_conf_false_negative": 2, "genuine_fix": 1}
  },
  "final_verdict": "false_negative",
  "reason": "1-3 sentences with decisive evidence",
  "issue_type": "stale_scoreboard|stale_reg_model|infrastructure_failure|log_redirect|test_hole|scope_mismatch|golden_impl_coupling|correct_unresolved",
  "agent_comparison": "key differences between passing and failing agents",
  "tb_script_failure_path": "exact tb_script line/condition causing failure",
  "fairness_assessment": "is the benchmark fair for this case given the problem_statement?",
  "fixability": "case_local_fix|framework_fix|redesign_needed|remove_from_dataset",
  "recommended_action": "specific fix instructions, or why it should be removed",
  "confirmed_correct_patches": ["agent names whose patches are confirmed correct but failed f2p"]
}
```

### Verdict definitions

- `false_negative`: agent fixed the bug correctly, tb_script misjudged
- `suspicious`: agent's fix works but diverges from correct semantics in a way that could cause other issues
- `hack`: agent targeted the test rather than fixing the actual bug
- `true_unresolved`: agent genuinely did not fix the bug
- `genuine_fix`: coarse audit was wrong, the case is actually fine (reclassify)

### Fixability definitions

- `case_local_fix`: can be fixed by modifying `tb_script.sh` only. This includes stale checker patches, logging fixes (e.g., `tee` instead of redirect), `set -e` scope fixes, `python3` path fixes, and any other change scoped to the case's own scripts. If the root cause is infrastructure-like (logging, early exit, setup failure) but the fix is still a tb_script edit, use this category.
- `framework_fix`: requires changes to code outside the case bundle — `evaluator.py`, `docker_runner.py`, Docker image templates, or the evaluation harness itself. Only use this when tb_script edits cannot resolve the issue.
- `redesign_needed`: tb_script fundamentally tests the wrong thing, needs full rewrite
- `remove_from_dataset`: case is inherently unfair or untestable, should be dropped

### `recommended_action` writing rules (HARD — Stage 3 will follow these verbatim)

These three rules apply whenever `fixability` is `case_local_fix` and `issue_type` is `test_hole` or `golden_impl_coupling`. They exist because Stage 3 Codex reliably takes the shortest-looking option even when a better one is also listed, and past cases have shown that vague guidance produces golden-coupled or capture-shortcut-using fixes that Stage 4 must later reject.

1. **Use a structured `DOs` + `DON'Ts` block instead of prose.**

   - `DOs` must name what the tb should **observe** and **assert** at the module boundary (elaborated parameters, top-level IO, HWE_BENCH_RESULTS output). Prefer behavioral invariants (e.g., "CMO source ID ∉ miss-entry range ∪ writeback-entry range") over shape/numeric assertions.
   - `DON'Ts` must enumerate the specific shortcuts Stage 3 Codex tends to take for this case type:
     - "Do NOT grep source text / do NOT match textual patterns"
     - "Do NOT hardcode golden-specific numeric values or parameter relationships (e.g., `fooBase == cfg.barSize + 1`)"
     - "Do NOT read Verilator root-level pointers / do NOT use backdoor signal access"
     - "Do NOT check commit SHAs or build artifacts"
     - Add case-specific DON'Ts when the previous failure mode is known.

2. **Do not propose multiple equivalent strategies at the same level.**

   Stage 3 tends to pick whichever option appears last or looks simplest to implement (often the golden-coupled one). If multiple strategies exist, explicitly rank them:

   - `Primary (preferred): <describe behavioral-invariant approach>`
   - `Fallback (only if primary is infeasible, with justification required): <describe lower-quality alternative>`

   Stage 3 must attempt the primary first and only fall back after documenting why.
