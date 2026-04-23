You are reviewing a generated **problem_statement** for `{ORG}/{REPO}` PR **#{NUMBER}**.

Your job is to make sure it is suitable as the sole bug description shown to an AI repair agent.

---

## Inputs in {PR_DIR}

- `problem_statement.json`: the generated statement to review
- `case.json`: full case record, including `resolved_issues`
- `fix.patch`: ground-truth fix
- `result.json`: verified test behavior

You may also inspect the original PR/issues with `gh`.

---

## Review criteria

### 1. Semantic completeness

The statement must clearly cover:

- observed behavior
- expected behavior
- affected function
- trigger condition

Expected behavior must be specific and testable, not vague.

### 2. Self-containment

A reader unfamiliar with XiangShan should still understand the bug.

If the current text assumes hidden project context, add the minimum necessary background.

### 3. No ambiguity

Check whether a reasonable agent could misunderstand:

- which subsystem is affected
- which edge condition matters
- what behavior is actually wrong

If multiple interpretations are possible, revise the text.

### 4. Alignment with the verified test

Compare the statement against `result.json`.

- If the statement mentions behavior not covered by the verified test, remove or weaken it
- If the verified test depends on a specific behavioral edge case that the statement omits, add it

This alignment is at the **behavior** level only. Do not describe the test method.

### 5. Information-granularity control

Keep the statement aligned with the original issue/PR granularity.

For XiangShan, names like FTQ, IFU, uncache, config generation, frontend redirect, or exception handling are fine when they come from the source material. Do not add extra patch-level implementation detail that only came from `fix.patch`.

### 6. Leakage check

Reject or rewrite text that leaks:

- repair instructions
- patch details
- file/line specifics not present in the original issue/PR
- build or simulation commands
- benchmark/test artifacts

---

## Output

Write English and save `{PR_DIR}/problem_statement_reviewed.json`:

```json
{
  "org": "{ORG}",
  "repo": "{REPO}",
  "number": {NUMBER},
  "problem_statement": "final text",
  "review_status": "approved|revised|rewritten",
  "review_notes": "what changed and why",
  "anchor_source": "issue|pr_body|inferred",
  "anchor_issues": [1234]
}
```

Use:

- `approved` if the original text is already good
- `revised` if small corrections are enough
- `rewritten` if the original text is substantially unsuitable
