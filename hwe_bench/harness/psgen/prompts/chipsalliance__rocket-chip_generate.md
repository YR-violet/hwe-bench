You need to write a **problem_statement** for `{ORG}/{REPO}` PR **#{NUMBER}**.

The downstream repair agent will see only this text. It will **not** see `fix.patch`, `tb_script`, or any of the benchmark construction artifacts. The verified case was built against base commit `{BASE_SHA}` and associated with base image `{BASE_IMAGE}`.

---

## Inputs in {PR_DIR}

- `case.json`: full verified case record, including `resolved_issues`
- `fix.patch`: ground-truth fix, for your understanding only
- `result.json`: verified `tb_script` / `prepare_script`, useful for understanding the exact observable behavior being tested
- `pr_meta.json`: task summary

You may also use:

- `gh pr view {NUMBER} -R {ORG}/{REPO} --json body,comments`
- `gh issue view <issue_number> -R {ORG}/{REPO}`
- repository context under `{REPO_ROOT}`

Do not introduce details from comments that go beyond the information granularity of the original issue or PR body.

---

## What the problem_statement must contain

It must cover these four elements:

1. **Observed behavior**
2. **Expected behavior**
3. **Affected function**
4. **Trigger condition**

The statement must be self-contained for an agent that does not already know rocket-chip internals.

For rocket-chip, the affected function is often a subsystem such as:

- CSR or privilege-state handling
- TLB / PTW / virtual-memory behavior
- cache, coherence, or TileLink-related logic
- diplomacy/config elaboration
- tile or core parameterization
- interrupts, exceptions, or pipeline control

Keep the description at the behavior/specification level, not the patch level.

---

## Source selection rules

Primary source order:

1. Semantically relevant `resolved_issues` from `case.json`
2. PR `title` and `body`
3. Behavioral inference from `fix.patch` only if the issue/PR text is insufficient

Ignore unrelated tracking issues, documentation-only issues, or cross-repo issues that do not describe the bug being fixed here.

---

## Alignment with the verified test

Read `result.json` and understand what the verified rocket-chip test actually checks.

The problem statement must align with the **behavior** validated by the test:

- if the test covers a specific edge condition, include that edge condition
- do not describe the test method, build command, script path, or simulator setup
- do not omit the exact behavioral scenario if the test depends on it

For example, a good rocket-chip statement names the failing functional scenario, not the exact Scala code change and not the SBT or Verilator invocation.

---

## Leakage rules

Do **not** include:

- the repair strategy
- patch hunks or code snippets from `fix.patch`
- testbench construction details
- build or simulation commands
- log paths, test names, or prompt artifacts

Good:

- “Writes to a virtual supervisor status field incorrectly modify read-only state when the extension is absent.”
- “A generated configuration miscomputes a downstream parameter under a specific address-width or privilege-mode combination.”

Bad:

- “Edit `CSR.scala` to mask the XS bits when writing VSSTATUS.”
- “Run `sbt testOnly ...` and check the generated Verilator build.”

---

## Output format

Write English and save `{PR_DIR}/problem_statement.json`:

```json
{
  "org": "{ORG}",
  "repo": "{REPO}",
  "number": {NUMBER},
  "problem_statement": "your text here",
  "anchor_source": "issue|pr_body|inferred",
  "anchor_issues": [1234]
}
```

Use `anchor_source` to describe the dominant source, and `anchor_issues` for the actual issues you relied on.
