You need to write a **problem_statement** for a bug in the lowRISC/ibex RISC-V processor core — a bug description aimed at an AI agent. The agent will have only this text to go by when fixing the bug; it cannot see fix.patch, tb_script, or any other repair information.

## Your input

The following files already exist under the current directory `{PR_DIR}`:

- `case.json`: the full case record, containing fields such as title/body/resolved_issues/base/fix_patch. **Read `resolved_issues` from here.**
- `fix.patch`: the ground-truth fix patch (helps you understand the bug, but must not be leaked to the agent)
- `result.json`: results from the previous tbgen/verify stages (contains tb_script, which helps you understand how the bug is triggered and how it is verified)
- `pr_meta.json`: PR summary

You may use `gh pr view {NUMBER} -R lowRISC/ibex --json body,comments` and `gh issue view <issue_number> -R lowRISC/ibex` to inspect PR/issue discussions and comments, but do not introduce fix hints or test details beyond the granularity of the original issue/PR body.

## Writing requirements

### Pick the information source

From `resolved_issues` in `case.json`, pick the issues that are semantically related to what the PR fixes as the primary reference. Ignore issues unrelated to this bug (e.g., DV task tickets, tracking issues, documentation-only issues, cross-repo issues).

If `resolved_issues` is empty or all issues are unrelated, fall back to the PR's `title` and `body`. If those also lack a useful bug description, distill a behavior-level description from the scope of code changes in fix.patch.

### Semantic elements

The problem_statement should cover four elements:

1. **Observed behavior**: describe the observable symptom from a user/developer perspective.
2. **Expected behavior**: what the correct behavior should be. This must be a decidable, concrete normative description — vague wording is not allowed. For example, do not write "the decoder should handle this correctly"; instead, write "the decoder should treat C.LUI with rd=0 as a HINT (NOP), not raise an illegal instruction exception". If there is an explicit clause in the RISC-V specification, cite it (e.g., "as specified in the RISC-V Debug Specification v0.13").
3. **Affected function**: the affected functional module (at function granularity, e.g., "the compressed instruction decoder", "PMP address matching logic").
4. **Trigger condition**: the trigger condition (at the architectural/spec level, e.g., "when rd=0", "in NAPOT mode"). The trigger condition must be specific enough that the agent knows which boundary scenario to fix, not merely which module.

### Self-containedness

The generated problem_statement must be self-contained — an agent unfamiliar with the ibex project should be able to understand what the bug is and what the correct behavior is after reading it. If the original issue relies on implicit context (referring to earlier discussion, assuming the reader knows a specific RISC-V spec detail, using project-internal terminology without explanation), add the minimum amount of background needed for the description to stand on its own.

### Information granularity

Use the granularity of the chosen original issue as reference. Information already in the original issue (including file names, signal names, code snippets, etc.) may be kept verbatim — do not artificially blur it. But anything you add yourself should not exceed the original issue's granularity.

For reference: without a problem_statement, the default evaluation practice is to concatenate the bodies of all `resolved_issues` and feed them to the agent as-is. The value of problem_statement lies in integrating multiple sources, filtering unrelated-issue noise, filling in context for empty issues, while ensuring no leakage of fix approach or test method.

### Behavior-level alignment with the test

Look at the tb_script in `result.json` to understand the specific behavior it tests. Make sure the bug behavior described by problem_statement covers the scenario tb_script actually verifies. If tb_script tests a specific boundary condition (e.g., "PMP in debug mode accessing address range 0x1A110000"), problem_statement should mention that boundary condition (at the behavior level), not just give a vague module name.

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

Anything you add yourself must not contain: fix approaches (e.g., "should change to X"), excerpts of fix.patch/tb_script, simulation/build commands (e.g., "fusesoc run ..."), or test names / test seeds / UVM assertion names / log paths.

Correct examples:
- ✅ "Compressed HINT instructions (rd=0) are incorrectly decoded as illegal instructions"
- ✅ "When the hart is in debug mode, PMP checks should not block accesses to the Debug Module address range (0x1A110000–0x1A11FFFF), as specified in the RISC-V Debug Specification"

Incorrect examples:
- ❌ "Run fusesoc ibex_simple_system, write a test that executes C.LUI with rd=0" (leaks the test method)
- ❌ "The case statement in the compressed decoder is missing a branch for rd==0" (points at the fix location)
- ❌ "Change line 215 to add a default case" (gives the fix approach)
- ❌ "The decoder has some issues with compressed instructions" (expected behavior not decidable)

## Output

Write in English, with length sufficient to cover the four elements completely.

Write the result to `{PR_DIR}/problem_statement.json`:

```json
{
  "org": "lowRISC",
  "repo": "ibex",
  "number": {NUMBER},
  "problem_statement": "your text here",
  "anchor_source": "issue|pr_body|inferred",
  "anchor_issues": [43, 44]
}
```

`anchor_source` indicates what source you primarily relied on, and `anchor_issues` lists the issue numbers you actually used (if any).
