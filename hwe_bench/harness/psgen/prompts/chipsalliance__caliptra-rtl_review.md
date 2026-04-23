You are reviewing a generated `problem_statement` for one `chipsalliance/caliptra-rtl` benchmark case.

Your task is to ensure the text is suitable for a downstream repair agent that will not see `fix.patch`, `tb_script`, or any benchmark-construction metadata.

Caliptra is a hardware Root of Trust IP with security-focused blocks such as AES, HMAC, SHA, ECC, keyvault, datavault, DOE, mailbox, `soc_ifc`, integration firmware flows, and post-quantum accelerators in later revisions.

## Inputs

Inside `{PR_DIR}` you have:

- `problem_statement.json`: the generated text under review
- `case.json`: full case record
- `fix.patch`: ground-truth fix
- `result.json`: verified tbgen/verify result, including the final `tb_script`

You may inspect public PR or issue discussion if needed.

## Review checklist

### 1. Semantic completeness

Ensure the statement clearly covers:

- observed incorrect behavior
- expected correct behavior
- affected block or subsystem
- trigger condition

If the expected behavior is vague, rewrite it to be testable.

### 2. Self-containment

A reader who does not already know Caliptra should still understand the bug.

- Expand ambiguous abbreviations when needed.
- Add only minimal project background.
- Avoid relying on unstated repo-specific assumptions.

### 3. Clarity and lack of ambiguity

Check whether a reasonable engineer could misread the statement and repair the wrong behavior.

- Is the affected block specific enough?
- Is the trigger condition precise enough?
- Are overloaded terms clarified?

### 4. Alignment with the verified test behavior

Read `result.json` and understand what the verified `tb_script` actually checks.

- If the statement omits a key corner case that the test depends on, add it behaviorally.
- If the statement describes behavior that the test does not verify, remove or soften it.

### 5. Granularity

Stay within the level of detail supported by the original issues or PR description.

- Keep names that already appear in the source material.
- Do not add extra patch-only implementation detail.

### 6. Leakage check

Unless a detail already appears in the original issue or PR text, do not add:

- repair instructions
- patch snippets
- exact build or simulation commands
- test names, seeds, assertion names, or log paths
- harness-specific setup details

## Output

Write `{PR_DIR}/problem_statement_reviewed.json`:

```json
{
  "org": "chipsalliance",
  "repo": "caliptra-rtl",
  "number": {NUMBER},
  "problem_statement": "final text",
  "review_status": "approved|revised|rewritten",
  "review_notes": "what changed and why",
  "anchor_source": "issue|pr_body|inferred",
  "anchor_issues": [1224]
}
```

Use English.
