You need to write a `problem_statement` for one `chipsalliance/caliptra-rtl` benchmark case.

The downstream repair agent will see only this text. It will not see `fix.patch`, `tb_script`, or any benchmark-construction artifacts.

Caliptra is a SystemVerilog hardware Root of Trust IP. Bugs may involve security-sensitive blocks such as AES, HMAC, SHA, ECC, DOE, keyvault, datavault, mailbox, `soc_ifc`, integration firmware interfaces, or newer MLDSA / MLKEM / ABR flows.

## Inputs

Inside `{PR_DIR}` you already have:

- `case.json`: full case record, including `title`, `body`, `resolved_issues`, `base`, and `fix_patch`
- `fix.patch`: ground-truth fix, for your understanding only
- `result.json`: verified tbgen/verify output, including the final `tb_script`
- `pr_meta.json`: PR summary

You may also inspect the public PR and issue discussion with `gh`.

## What the problem statement must contain

Your statement must cover four elements:

1. Observed behavior: what goes wrong from a user or integrator point of view
2. Expected behavior: what the design should do instead, in specific and testable terms
3. Affected function: the specific Caliptra IP, block, or subsystem
4. Trigger condition: the operating mode, sequence, configuration, or corner case that exposes the bug

## Source selection

Prefer the semantically relevant entries in `case.json["resolved_issues"]`.

- If one or more resolved issues directly describe the bug, use them as anchors.
- If the resolved issues are empty or irrelevant, fall back to the PR title/body.
- If those are still insufficient, infer the behavior from `fix.patch`, but stay at the behavior level rather than describing the implementation.

## Self-contained writing

The text must be understandable without prior Caliptra context. Expand uncommon abbreviations the first time they appear when necessary, for example:

- DOE: Deobfuscation Engine
- TRNG: True Random Number Generator
- KV: KeyVault
- SOC_IFC: SoC interface
- MLDSA / MLKEM: post-quantum signature / key encapsulation blocks

You may keep file names, register names, or signal names if they already appear in the original issue or PR text. Do not add extra low-level detail that only comes from the patch unless it is necessary to describe observable behavior.

## Alignment with the verified test

Read `result.json` and understand what behavior the final `tb_script` validates.

- If the test checks a specific corner case, the problem statement should mention that corner case behaviorally.
- Do not describe the test method, commands, seeds, log strings, or how the harness runs.
- The goal is behavioral alignment, not test leakage.

## Leakage rules

Do not include:

- a repair strategy or implementation advice
- patch hunks or code snippets from `fix.patch`
- commands such as `make ... verilator`
- test names, seeds, assertion names, or log paths
- any detail that only exists to explain how the benchmark test works

## Output

Write `{PR_DIR}/problem_statement.json`:

```json
{
  "org": "chipsalliance",
  "repo": "caliptra-rtl",
  "number": {NUMBER},
  "problem_statement": "your text here",
  "anchor_source": "issue|pr_body|inferred",
  "anchor_issues": [1224]
}
```

Use English.
