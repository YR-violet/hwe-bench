You are running **s10 verify** for `{ORG}/{REPO}` PR **#{NUMBER}** at base commit **{BASE_SHA}**.

This is **not** a fresh script-generation task. The case directory `{PR_DIR}` already contains candidate scripts and metadata. Your job is to make the smallest necessary repair so the case passes the standard harness validation.

At most **3 attempts**. If it still fails after 3 attempts, write a failure result instead of looping.

---

## Goal

Under the standard XiangShan harness:

- `test-run.sh` must run on the prepared baseline and produce at least one `TEST: ... FAIL`
- `fix-run.sh` must apply `fix.patch`, rerun the same `tb_script.sh`, and produce at least one `TEST: ... PASS`

You must update `{PR_DIR}/tb_script.sh`, `{PR_DIR}/prepare_script.sh` if needed, and `{PR_DIR}/result.json`.

---

## Files already present in {PR_DIR}

- `case.json`
- `pr_meta.json`
- `tb_script.sh`
- `prepare_script.sh`
- `fix.patch`

Do not edit `case.json`. Edit the scripts directly.

---

## Harness facts you must respect

1. `prepare_script.sh` is baked into the image at build time, and the framework appends canonical stage 5 automatically
2. `tb_script.sh` is mounted at runtime as `/home/tb_script.sh`
3. `test-run.sh` semantics:
   - `cd /home/xiangshan`
   - `git reset --hard && git clean -fdx`
   - checkout `/home/base_commit.txt`
   - `bash /home/tb_script.sh`
4. `fix-run.sh` semantics:
   - reset and clean again
   - checkout baseline again
   - apply `fix.patch`
   - `bash /home/tb_script.sh`
5. Runtime scripts and patches are mounted read-only under `/home/*.sh` and `/home/*.patch`
6. You may only write temporary files, logs, and build outputs under `/home/xiangshan`
7. Every validation run starts from a fresh container image and a clean runtime reset
8. The log parser only recognizes `TEST: <name> ... PASS|FAIL|SKIP` markers between `HWE_BENCH_RESULTS_START` and `HWE_BENCH_RESULTS_END`

Relevant XiangShan environment:

- repo root: `/home/xiangshan`
- `NOOP_HOME=/home/xiangshan`
- `XIANGSHAN_HOME=/home/xiangshan`
- `JAVA_HOME`
- `MILL_VERSION`
- `VERILATOR_ROOT=/tools/verilator`
- `RISCV_HOME=/tools/riscv`
- `NEMU_HOME`

XiangShan-specific reminder:

- prefer module-level or subsystem-level flows
- avoid full `SimTop` / `make emu` if a smaller real reproducer exists

If you need more detail, inspect:

- `{REPO_ROOT}/hwe_bench/harness/repos/chisel/xiangshan/xiangshan.py`
- `{REPO_ROOT}/hwe_bench/harness/docker_runner.py`
- `{REPO_ROOT}/hwe_bench/utils/docker_util.py`
- `{REPO_ROOT}/hwe_bench/harness/repos/verilog/common.py`

---

## Static checks before real validation

`tb_script.sh` must satisfy all of these:

- `cd /home/xiangshan`
- emits at least one literal `TEST:`
- includes `HWE_BENCH_RESULTS_START`
- includes `HWE_BENCH_RESULTS_END`
- does not reference `/workspace/pr`
- does not read `fix.patch`, `pr_meta.json`, or `git diff/status` to decide PASS/FAIL
- does not run `git checkout`, `git reset`, `git clean`, or `git apply`
- writes only under `/home/xiangshan`
- uses real build/elaboration/test/simulation behavior, not static text matching

Implementation-independence still matters here. If the existing `tb_script.sh` is coupled to the exact ground-truth Scala implementation rather than observable behavior, rewrite it.

---

## How to run the standard verifier

Normal rerun:

```bash
{PYTHON_BIN} {RUN_CASE} --case-dir {PR_DIR}
```

If you changed `prepare_script.sh`, force rebuild:

```bash
{PYTHON_BIN} {RUN_CASE} --case-dir {PR_DIR} --force-build
```

Read the generated logs in `{PR_DIR}/workdir/` and fix the smallest real issue first.

---

## XiangShan-specific repair priorities

1. Prefer repairing only `tb_script.sh`
2. Change `prepare_script.sh` only if the default XiangShan prepare flow is insufficient
3. If the script currently uses full-chip elaboration, first check whether the bug can be reproduced with:
   - an existing Scala unit test
   - a narrow top or wrapper
   - a smaller Verilator compile
4. If the bug only reproduces with a heavyweight flow and consistently exceeds the benchmark time budget, return failure instead of forcing success

Common failure modes:

- wrong working directory
- missing TEST markers
- stale assumptions about `MILL_VERSION` or `JAVA_HOME`
- using `/workspace/pr`
- relying on text grep instead of exit status
- choosing a full XiangShan flow when a smaller real one exists

---

## Required result.json shape

Success:

```json
{
  "status": "success",
  "org": "{ORG}",
  "repo": "{REPO}",
  "number": {NUMBER},
  "base_sha": "{BASE_SHA}",
  "tb_script": "#!/bin/bash\n...",
  "prepare_script": "",
  "failure": null,
  "artifacts": {
    "report_path": ".../report.json",
    "instance_dir": ".../instances/pr-{NUMBER}"
  }
}
```

Failure:

```json
{
  "status": "failure",
  "org": "{ORG}",
  "repo": "{REPO}",
  "number": {NUMBER},
  "base_sha": "{BASE_SHA}",
  "tb_script": "#!/bin/bash\n...",
  "prepare_script": "",
  "failure": {
    "stage": "static_check|build_image|test_run|fix_run|report_parse|no_repair|timeout|implementation_coupled",
    "reason": "one-sentence explanation",
    "last_error_excerpt": "key log excerpt",
    "attempts": [
      "attempt 1 summary",
      "attempt 2 summary"
    ]
  },
  "artifacts": {
    "report_path": ".../report.json",
    "instance_dir": ".../instances/pr-{NUMBER}"
  }
}
```

Always write the final full script contents back into `result.json`.
