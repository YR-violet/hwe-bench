You need to write a **problem_statement** for a bug in the `openhwgroup/cva6` RISC-V application processor (6-stage pipeline, supports RV32/RV64) — a bug description aimed at an AI agent. The agent will have only this text to go by when fixing the bug; it cannot see fix.patch, tb_script, or any other repair information.

## Your input

The following files already exist under the current directory `{PR_DIR}`:

- `case.json`: the full case record, containing fields such as title/body/resolved_issues/base/fix_patch. **Read `resolved_issues` from here.**
- `fix.patch`: the ground-truth fix patch (helps you understand the bug, but must not be leaked to the agent)
- `result.json`: results from the previous tbgen/verify stages (contains tb_script, which helps you understand how the bug is triggered and how it is verified)
- `pr_meta.json`: PR summary

You may use `gh pr view {NUMBER} -R openhwgroup/cva6 --json body,comments` and `gh issue view <issue_number> -R openhwgroup/cva6` to inspect PR/issue discussions and comments, but do not introduce fix hints or test details beyond the granularity of the original issue/PR body.

## Writing requirements

### Pick the information source

From `resolved_issues` in `case.json`, pick the issues that are semantically related to what the PR fixes as the primary reference. Ignore issues unrelated to this bug (e.g., DV task tickets, tracking issues, documentation-only issues, cross-repo issues).

If `resolved_issues` is empty or all issues are unrelated, fall back to the PR's `title` and `body`. If those also lack a useful bug description, distill a behavior-level description from the scope of code changes in fix.patch.

### Semantic elements

The problem_statement should cover four elements:

1. **Observed behavior**: describe the observable symptom from a user/developer perspective.
2. **Expected behavior**: what the correct behavior should be. This must be a decidable, concrete normative description — vague wording is not allowed. For example, do not write "the pipeline should handle this correctly"; instead, write "when writing `pmpcfg1`, the corresponding PMP configuration bytes should update the correct entry rather than aliasing another slot". If there is an explicit clause in the RISC-V specification, cite it.
3. **Affected function**: the affected functional module (at function granularity, e.g., "the CSR/PMP update logic", "the frontend fetch redirection logic", "the cache subsystem response path").
4. **Trigger condition**: the trigger condition (at the architectural/spec level, e.g., "when `mcountinhibit[0]` is set", "when accessing the next TOR entry", "under a specific PMP address mode"). The trigger condition must be specific enough that the agent knows which boundary scenario to fix, not merely which directory.

### Self-containedness

The generated problem_statement must be self-contained — an agent unfamiliar with the cva6 project should be able to understand what the bug is and what the correct behavior is after reading it. If the original issue relies on implicit context, add the minimum amount of background needed for the description to stand on its own.

### Information granularity

Use the granularity of the chosen original issue as reference. Information already in the original issue (including file names, signal names, code snippets, etc.) may be kept verbatim — do not artificially blur it. But anything you add yourself should not exceed the original issue's granularity.

For cva6, functional areas often map directly to modules or directories, e.g., `decoder.sv`, `alu.sv`, `frontend/*`, `cache_subsystem/*`, `core/csr_regfile.sv`. If these names already appear in the original issue / PR description, you may keep them; if not, do not introduce implementation detail beyond the original granularity.

### Behavior-level alignment with the test

Look at the tb_script in `result.json` to understand the specific behavior it tests. Make sure the bug behavior described by problem_statement covers the scenario tb_script actually verifies. If tb_script tests a specific boundary condition (e.g., a particular PMP CSR write, a particular frontend redirect condition, a particular cache response sequencing scenario), problem_statement should mention that boundary condition (at the behavior level), not just give a vague module name.

Note: alignment here is at the behavior level, not at the test-method level. Describe "what goes wrong and when", not "how to set up a test to verify it".

### Output format

A structured GitHub issue format is recommended, using Markdown headings to separate sections:

```markdown
## Description
...
## Expected Behavior
...
## Actual Behavior
...
```

This is not mandatory, but a structured format makes it easier for the agent to parse the key information.

### Overall principle

Describe **what goes wrong and when**. Do not describe **how to set up a test**. Do not describe the fix approach.

Anything you add yourself must not contain: fix approaches (e.g., "should change to X"), excerpts of fix.patch/tb_script, simulation/build commands (e.g., "run `make verilate ...`"), or test names / test seeds / log paths.

Correct examples:
- ✅ "When `mcountinhibit[0]` disables the cycle counter, `mcycle` should hold its previous value instead of tracking `instret`"
- ✅ "A write to `pmpcfg1` should update the intended PMP configuration bytes, not alias a different PMP entry"

Incorrect examples:
- ❌ "Run `python3 verif/sim/cva6.py` with a custom C test" (leaks the test method)
- ❌ "Change the index calculation in `csr_regfile.sv` from X to Y" (points at the fix approach)
- ❌ "The CSR logic has some issues" (expected behavior not decidable)

## Output

Write in English, with length sufficient to cover the four elements completely.

Write the result to `{PR_DIR}/problem_statement.json`:

```json
{
  "org": "openhwgroup",
  "repo": "cva6",
  "number": {NUMBER},
  "problem_statement": "your text here",
  "anchor_source": "issue|pr_body|inferred",
  "anchor_issues": [43, 44]
}
```

`anchor_source` indicates what source you primarily relied on, and `anchor_issues` lists the issue numbers you actually used (if any).
