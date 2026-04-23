You need to write a **problem_statement** for a bug in lowRISC/opentitan — an open-source silicon Root of Trust SoC — a bug description aimed at an AI agent. The agent will have only this text to go by when fixing the bug; it cannot see fix.patch, tb_script, or any other repair information.

OpenTitan is a complete SoC platform containing multiple IP modules (such as AES, HMAC, KMAC, OTBN, SPI, I2C, UART, GPIO, Flash Controller, OTP Controller, Alert Handler, CSRNG, EDN, Entropy Source, Power/Reset/Clock Manager, etc.) as well as system-level infrastructure (TL-UL bus interconnect, Pinmux, Padctrl, etc.). A bug may involve the RTL logic of a single IP, or interactions between IPs, or system-level integration.

## Your input

The following files already exist under the current directory `{PR_DIR}`:

- `case.json`: the full case record, containing fields such as title/body/resolved_issues/base/fix_patch. **Read `resolved_issues` from here.**
- `fix.patch`: the ground-truth fix patch (helps you understand the bug, but must not be leaked to the agent)
- `result.json`: results from the previous tbgen/verify stages (contains tb_script, which helps you understand how the bug is triggered and how it is verified)
- `pr_meta.json`: PR summary

You may use `gh pr view {NUMBER} -R lowRISC/opentitan --json body,comments` and `gh issue view <issue_number> -R lowRISC/opentitan` to inspect PR/issue discussions and comments, but do not introduce fix hints or test details beyond the granularity of the original issue/PR body.

## Writing requirements

### Pick the information source

From `resolved_issues` in `case.json`, pick the issues that are semantically related to what the PR fixes as the primary reference. Ignore issues unrelated to this bug (e.g., DV task tickets, tracking issues, documentation-only issues, cross-repo issues).

If `resolved_issues` is empty or all issues are unrelated, fall back to the PR's `title` and `body`. If those also lack a useful bug description, distill a behavior-level description from the scope of code changes in fix.patch.

### Semantic elements

The problem_statement should cover four elements:

1. **Observed behavior**: describe the observable symptom from a user/developer perspective.
2. **Expected behavior**: what the correct behavior should be. This must be a decidable, concrete normative description — vague wording is not allowed. For example, do not write "the AES module should handle this correctly"; instead, write "when a new key is loaded via KEY_SHARE registers while an encryption is in progress, the AES module should finish the current operation first and then use the new key for the next operation". If there is an explicit clause in OpenTitan design specifications or a related standard (NIST SP 800-90A, FIPS 202, etc.), cite it.
3. **Affected function**: the affected functional module. For OpenTitan, specify the concrete IP module or subsystem (e.g., "the AES core's key scheduling logic", "the SPI Device's firmware mode RX FIFO", "the Alert Handler's escalation counter"), rather than just saying "opentitan has a bug".
4. **Trigger condition**: the trigger condition (at the functional/configuration level, e.g., "when EDN is in boot-time request mode and the request FIFO is full", "when the I2C target receives a repeated START after a NACK"). The trigger condition must be specific enough that the agent knows which boundary scenario to fix, not merely which module.

### Self-containedness

The generated problem_statement must be self-contained — an agent unfamiliar with the opentitan project should be able to understand what the bug is and what the correct behavior is after reading it. If the original issue relies on implicit context (referring to earlier discussion, assuming the reader knows a specific IP design spec, using project-internal terminology without explanation), add the minimum amount of background needed for the description to stand on its own.

OpenTitan-specific acronyms should be expanded on first use, for example: OTP (One-Time Programmable), CSRNG (Cryptographically Secure Random Number Generator), EDN (Entropy Distribution Network), OTBN (OpenTitan Big Number Accelerator), TL-UL (TileLink Uncached Lightweight), KMAC (Keccak Message Authentication Code).

### Information granularity

Use the granularity of the chosen original issue as reference. Information already in the original issue (including file names, signal names, register names, code snippets, etc.) may be kept verbatim — do not artificially blur it. But anything you add yourself should not exceed the original issue's granularity.

For reference: without a problem_statement, the default evaluation practice is to concatenate the bodies of all `resolved_issues` and feed them to the agent as-is. The value of problem_statement lies in integrating multiple sources, filtering unrelated-issue noise, filling in context for empty issues, while ensuring no leakage of fix approach or test method.

### Behavior-level alignment with the test

Look at the tb_script in `result.json` to understand the specific behavior it tests. Make sure the bug behavior described by problem_statement covers the scenario tb_script actually verifies. If tb_script tests a specific boundary condition (e.g., "SPI host mode with CPOL=1, CPHA=1 and a clock divider of 2"), problem_statement should mention that boundary condition (at the behavior level), not just give a vague module name.

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

Anything you add yourself must not contain: fix approaches (e.g., "should change to X"), excerpts of fix.patch/tb_script, simulation/build commands (e.g., "fusesoc run ...", "make -C ..."), or test names / test seeds / UVM assertion names / log paths.

Correct examples:
- ✅ "The AES module's key sideload interface does not correctly latch the new key when a sideload key update arrives during an ongoing encryption"
- ✅ "When the SPI Device operates in firmware mode and the RX FIFO watermark is set to 1, the watermark interrupt fires continuously even after the FIFO is drained"

Incorrect examples:
- ❌ "Run the DV test `aes_sideload_vseq` with seed 42" (leaks the test method)
- ❌ "The always_comb block in aes_core.sv line 315 is missing a case" (points at the fix location)
- ❌ "Change the FSM transition from StIdle to StLoad" (gives the fix approach)
- ❌ "The SPI module has some issues" (expected behavior not decidable)

## Output

Write in English, with length sufficient to cover the four elements completely.

Write the result to `{PR_DIR}/problem_statement.json`:

```json
{
  "org": "lowRISC",
  "repo": "opentitan",
  "number": {NUMBER},
  "problem_statement": "your text here",
  "anchor_source": "issue|pr_body|inferred",
  "anchor_issues": [43, 44]
}
```

`anchor_source` indicates what source you primarily relied on, and `anchor_issues` lists the issue numbers you actually used (if any).
