You are running s10 verify for a `chipsalliance/caliptra-rtl` benchmark case.

This is not a from-scratch tbgen task. The case directory already contains a candidate `tb_script.sh`, an optional `prepare_script.sh`, the patch, and the metadata. Your job is to repair only what is necessary so the standard harness achieves `base FAIL / fix PASS`.

You may iterate up to 3 times. After 3 failed attempts, stop and emit a failure result.

## Goal

Make the case satisfy the standard HWE-bench semantics:

- `test-run.sh`: execute `tb_script.sh` on the prepared baseline and observe at least one `TEST: ... FAIL`
- `fix-run.sh`: apply `fix.patch`, execute the same `tb_script.sh`, and observe at least one `TEST: ... PASS`

You must update the files inside `{PR_DIR}` and write the final answer to `{PR_DIR}/result.json`.

## Files in the case directory

- `case.json`: full case record
- `pr_meta.json`: verify-stage metadata and paths
- `tb_script.sh`: current runtime script under review
- `prepare_script.sh`: current prepare override, if any
- `fix.patch`

Edit `tb_script.sh` and `prepare_script.sh` directly in `{PR_DIR}`. Do not edit `case.json`.

## Harness semantics you must respect

1. The prepared image contains `/home/caliptra-rtl`.
2. `prepare_script.sh`, if present, is baked into the image build and stage 5 finalize is appended automatically by the framework.
3. The baseline commit is recorded in `/home/base_commit.txt`.
4. `tb_script.sh` is mounted at runtime as `/home/tb_script.sh`.
5. `test-run.sh` does:
   - `cd /home/caliptra-rtl`
   - `git reset --hard && git clean -fdx`
   - `git checkout "$(cat /home/base_commit.txt)"`
   - `git submodule update --init --recursive || true`
   - `bash /home/tb_script.sh`
6. `fix-run.sh` does the same, then applies `/home/fix.patch`, then runs `bash /home/tb_script.sh`.
7. All writable build outputs and scratch files must stay under `/home/caliptra-rtl`.
8. The harness log parser only recognizes `TEST: <name> ... PASS|FAIL|SKIP` markers between `HWE_BENCH_RESULTS_START` and `HWE_BENCH_RESULTS_END`.

## Static checks

Before running anything, verify:

- `tb_script.sh` starts by entering `/home/caliptra-rtl`
- it contains `HWE_BENCH_RESULTS_START`, `HWE_BENCH_RESULTS_END`, and at least one literal `TEST:`
- it does not reference `/workspace/pr`
- it does not read `fix.patch`, `pr_meta.json`, or `git diff/status` to decide PASS/FAIL
- it does not run `git checkout`, `git reset`, `git clean`, or `git apply`
- it only writes under `/home/caliptra-rtl`
- it uses a real Caliptra compile/simulation path, not a fake or purely static check

## Caliptra-specific constraints

- This harness supports open-source Verilator flows only.
- Prefer `tools/scripts/Makefile` and existing `src/integration/test_suites` tests.
- Prefer a targeted per-IP or focused integration test over broad regression.
- Do not depend on VCS, UVM VIP, or any closed verification environment.

## Repair priority

1. Prefer fixing only `tb_script.sh`.
2. Only change `prepare_script.sh` if the default environment is demonstrably insufficient.
3. If you change `prepare_script.sh`, force an image rebuild on the next verification run.

## Standard verification command

Use the helper script:

```bash
{PYTHON_BIN} {RUN_CASE} --case-dir {PR_DIR}
```

If you changed `prepare_script.sh`, use:

```bash
{PYTHON_BIN} {RUN_CASE} --case-dir {PR_DIR} --force-build
```

Useful outputs appear under:

- `{PR_DIR}/workdir/chipsalliance/caliptra-rtl/images/pr-{NUMBER}/build_image.log`
- `{PR_DIR}/workdir/chipsalliance/caliptra-rtl/instances/pr-{NUMBER}/test-patch-run.log`
- `{PR_DIR}/workdir/chipsalliance/caliptra-rtl/instances/pr-{NUMBER}/fix-patch-run.log`
- `{PR_DIR}/workdir/chipsalliance/caliptra-rtl/instances/pr-{NUMBER}/report.json`

## Common failure modes

- Verilator compile errors from using the wrong file list or top
- firmware build failures from missing `TESTNAME`, missing toolchain, or wrong include assumptions
- tests that pass on both base and fix because the scenario does not hit the bug
- tests that fail on both base and fix because the script depends on implementation details or stale generated collateral
- tests that are too broad and time out

## Result format

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
    "report_path": ".../report.json",
    "instance_dir": ".../instances/pr-1227"
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
    "stage": "static_check|build_image|test_run|fix_run|report_parse|no_repair|implementation_coupled",
    "reason": "short explanation",
    "last_error_excerpt": "important final log excerpt",
    "attempts": [
      "Attempt 1 summary",
      "Attempt 2 summary"
    ]
  },
  "artifacts": {
    "report_path": ".../report.json",
    "instance_dir": ".../instances/pr-1227"
  }
}
```

Always write `result.json`, even if the case fails.
