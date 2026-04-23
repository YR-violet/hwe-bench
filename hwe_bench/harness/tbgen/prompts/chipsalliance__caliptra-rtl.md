You are generating a `tb_script.sh` for a single `chipsalliance/caliptra-rtl` PR and validating it inside the standard HWE-bench Docker harness.

This is a closed-loop task: inspect the PR/case metadata, generate a runnable test script, validate `base FAIL / fix PASS`, and write the structured result to `{PR_DIR}/result.json`.

You may iterate up to 3 times. If you still cannot produce a valid `base FAIL / fix PASS` test after 3 attempts, stop and report failure. Do not loop indefinitely.

## Inputs

The following files already exist under `{PR_DIR}`:

- `pr_meta.json`: PR metadata, including `org`, `repo`, `number`, `base_sha`, `title`, `body`, and `resolved_issues`
- `fix.patch`: ground-truth fix patch used only to validate that your test passes after the bug is fixed
- `logs/`: directory where you should save important logs
- `{BASE_IMAGE}`: prebuilt Caliptra base image. Do not rebuild it yourself during tbgen.

You may inspect repository sources on the host side and use public web search / `gh` for PR or issue context. The actual checkout, build, simulation, and validation must happen inside Docker.

## Required outputs

You must write the following files under `{PR_DIR}`:

1. `tb_script.sh`: the runtime test script executed by the harness
2. `prepare_script.sh`: optional; only create this if the default environment preparation is insufficient
3. `result.json`: final structured result

You must also preserve useful logs under `{PR_DIR}/logs/`:

- `docker_build.txt`
- `test_run_base.txt`
- `test_run_fix.txt`

## Hard constraints

1. `tb_script.sh` must `cd /home/caliptra-rtl`.
2. `tb_script.sh` must print `TEST:` markers in the exact format `TEST: <name> ... PASS|FAIL|SKIP`.
3. All `TEST:` markers must be printed strictly between:
   - `echo "HWE_BENCH_RESULTS_START"`
   - `echo "HWE_BENCH_RESULTS_END"`
4. Running on the baseline `base_sha` must fail:
   - exit code must be non-zero
   - log must contain at least one `TEST: ... FAIL`
5. Running after applying `fix.patch` must pass:
   - exit code must be 0
   - log must contain at least one `TEST: ... PASS`
6. `prepare_script` should be the empty string by default. Only emit it if the default stage 1-4 environment is insufficient. If you emit it, it replaces stage 1-4 completely; stage 5 is appended automatically by the harness.
7. All paths in `tb_script.sh` and `prepare_script.sh` must be based on `/home/caliptra-rtl`. Do not reference `/workspace/pr` or host-side artifact paths.
8. PASS/FAIL must be decided by a real compile or simulation flow. Do not read `fix.patch`, `pr_meta.json`, `git diff`, or source text to fake PASS/FAIL.
9. Prefer simulator exit codes as the oracle. Only fall back to log-grepping when the simulator always exits 0 and there is no cleaner signal.
10. If a single `tb_script.sh` run takes more than 30 minutes, report failure with `failure.stage = "timeout"`.
11. The container already auto-activates the micromamba `caliptra` environment through `BASH_ENV`. Do not manually run `micromamba shell hook` or `micromamba activate`.
12. The open-source harness only supports Verilator-based flows. Do not rely on VCS, UVM VIP, QVIP, Avery AXI VIP, or any other closed verification stack.
13. Implementation independence matters: your test must validate observable behavior, not a specific internal implementation from `fix.patch`.

## Caliptra-specific guidance

Caliptra is a hardware Root of Trust IP with many security-focused blocks:

- crypto/data path blocks: AES, HMAC, SHA256, SHA512, SHA3/KMAC, ECC, DOE
- key and storage blocks: keyvault, datavault, pcrvault
- system/integration blocks: soc_ifc, integration top, mailbox, DMA, watchdog, TRNG plumbing
- post-quantum / newer blocks in later commits: MLDSA, MLKEM, ABR-related flows

Open-source simulation in this harness is centered on:

- `tools/scripts/Makefile`
- `src/integration/test_suites/*`
- Verilator
- the RISC-V firmware toolchain

The upstream CI currently uses Verilator `v5.044` and a `riscv64-unknown-elf` toolchain on `ubuntu-22.04`. The harness base image already installs these tools.

## Preferred test strategy

Prefer the narrowest real test that still exercises the bug:

1. Reuse an existing integration smoke test from `src/integration/test_suites/` when one already covers the affected behavior.
2. If no existing test directly covers the bug, create a small derived test under a private scratch directory inside `/home/caliptra-rtl`, but still drive it through the real Caliptra build flow.
3. Prefer per-IP or targeted integration scenarios over full broad regression runs. Do not run the entire L0 regression unless there is no narrower option.

Good candidates include:

- `make -C <run_dir> -f /home/caliptra-rtl/tools/scripts/Makefile TESTNAME=<existing_test> verilator`
- a small adaptation of an existing `src/integration/test_suites/<name>` test, compiled and run through the same Makefile
- targeted unit-level or block-level Verilator compilation using existing `src/<ip>/config/*.vf` and `src/<ip>/tb` collateral when the bug is local to one IP and that route is simpler than the full integration top

## What to avoid

- Do not use the closed VCS/UVM flows described elsewhere in the repo README.
- Do not hard-code internal signal names from the golden patch if you can instead check an externally visible behavior.
- Do not edit shared repo sources in a way that changes the benchmark target itself just to make the test pass.
- Do not rely on stale wrappers or interface changes from the golden fix. If the bug can be checked through a stable external interface, do that instead.

## Harness facts

The standard harness implementation is here:

- Caliptra harness: `{REPO_ROOT}/hwe_bench/harness/repos/verilog/caliptra/caliptra.py`
- Docker build/run wrapper: `{REPO_ROOT}/hwe_bench/harness/docker_runner.py`
- Shared finalize and log parser helpers: `{REPO_ROOT}/hwe_bench/harness/repos/common.py`

The default `prepare_script` already does:

1. checkout `base_sha`
2. sync and update submodules
3. detect requested Verilator / RISC-V toolchain versions from `.github/workflows/build-test-verilator.yml`
4. install minimal Python deps (`pyyaml`) for the open-source scripts

If that is enough, leave `prepare_script` empty.

## Recommended workflow

1. Read `pr_meta.json`, `fix.patch`, and the relevant repo files on the host.
2. Determine the smallest real Caliptra simulation path that can expose the bug.
3. Enter the Docker container based on `{BASE_IMAGE}`.
4. Write and debug `tb_script.sh` inside the container.
5. Run it on the baseline checkout and save the output to `{PR_DIR}/logs/test_run_base.txt`.
6. Apply `fix.patch`, rerun the same script, and save the output to `{PR_DIR}/logs/test_run_fix.txt`.
7. If the default environment was not enough, write a full replacement `prepare_script.sh`, rebuild the per-PR image, and repeat.
8. Write the final `result.json`.

## Result schema

Success:

```json
{
  "status": "success",
  "org": "chipsalliance",
  "repo": "caliptra-rtl",
  "number": 1227,
  "base_sha": "xxxxxxxx",
  "tb_script": "#!/bin/bash\n...",
  "prepare_script": "",
  "failure": null,
  "artifacts": {
    "base_log": "{PR_DIR}/logs/test_run_base.txt",
    "fix_log": "{PR_DIR}/logs/test_run_fix.txt"
  }
}
```

Failure:

```json
{
  "status": "failure",
  "org": "chipsalliance",
  "repo": "caliptra-rtl",
  "number": 1227,
  "base_sha": "xxxxxxxx",
  "tb_script": "#!/bin/bash\n...",
  "prepare_script": "",
  "failure": {
    "stage": "static_check|build_image|test_run|fix_run|timeout|no_repair|implementation_coupled",
    "reason": "short explanation",
    "last_error_excerpt": "important last log excerpt",
    "attempts": [
      "Attempt 1 summary",
      "Attempt 2 summary"
    ]
  },
  "artifacts": {
    "base_log": "{PR_DIR}/logs/test_run_base.txt",
    "fix_log": "{PR_DIR}/logs/test_run_fix.txt"
  }
}
```

Write `result.json` even on failure.
