You are generating a benchmark case for **one** `{ORG}/{REPO}` pull request, PR **#{NUMBER}**, base commit **{BASE_SHA}**.

Your job is to produce a real `tb_script.sh` and, only if necessary, a `prepare_script.sh`, then validate that they reproduce the bug under the standard harness flow and write `{PR_DIR}/result.json`.

You must complete a closed loop:

1. Understand the bug from `{PR_DIR}/pr_meta.json` and `{PR_DIR}/fix.patch`
2. Launch a persistent container from `{BASE_IMAGE}`
3. Reproduce **base FAIL**
4. Apply `fix.patch` and reproduce **fix PASS**
5. Write the final scripts and structured result files back into `{PR_DIR}`

Stop after at most **3 iterations**. If you still cannot get a stable real reproduction, write a failure result instead of looping forever.

---

## Inputs

The following files already exist in `{PR_DIR}`:

- `{PR_DIR}/pr_meta.json`
- `{PR_DIR}/fix.patch`
- `{BASE_IMAGE}`: prebuilt XiangShan base image

You may read repository code under `{REPO_ROOT}` on the host, use `gh`, and search public docs. All actual checkout, build, elaboration, Verilator compilation, and validation must happen inside Docker.

---

## Required outputs

You must write:

1. `{PR_DIR}/tb_script.sh`
2. `{PR_DIR}/prepare_script.sh` only if the default stage-1..4 environment is insufficient. Otherwise leave it absent or empty.
3. `{PR_DIR}/result.json`

You must also save logs under `{PR_DIR}/logs/`:

- `docker_build.txt`
- `test_run_base.txt`
- `test_run_fix.txt`

---

## XiangShan container facts

Inside `{BASE_IMAGE}`:

- OS: Ubuntu 22.04
- Repo root: `/home/xiangshan`
- The harness runtime scripts reset to the prepared baseline, then run `/home/tb_script.sh`
- JDK 11 and JDK 17 are installed
- `JAVA_HOME` is selected by `prepare.sh`
- `mill` is available and auto-selects the version from `.mill-version` or the prepared environment
- Verilator versions available in the image: `/tools/verilator-v4.210` and `/tools/verilator-v5.008`
- Default symlink: `/tools/verilator -> /tools/verilator-v4.210`
- RISC-V toolchain is under `/tools/riscv`
- Repo-local environment is normally exposed through:
  - `NOOP_HOME=/home/xiangshan`
  - `XIANGSHAN_HOME=/home/xiangshan`
  - `JAVA_HOME`
  - `MILL_VERSION`
  - `VERILATOR_ROOT=/tools/verilator`
  - `RISCV_HOME=/tools/riscv`
  - `NEMU_HOME` (may point at a shim using `ready-to-run/*.so`)

Important runtime fact:

- `/workspace/pr` is mounted only during **tbgen generation**. It does **not** exist during benchmark evaluation. Your final `tb_script.sh` and `prepare_script.sh` must not reference `/workspace/pr` or any host mount path.

---

## XiangShan-specific strategy

XiangShan is a **Chisel/Scala** project. The agent fixes `.scala` files. Your `tb_script.sh` must trigger a real Chisel build or test flow that distinguishes base from fixed behavior.

Typical flows:

- Mill-driven unit tests or elaboration under `build.mill` / `build.sc`
- `Makefile.test` targets
- Small Scala test targets under `src/test/scala`
- Wrapper-top elaboration such as `FrontendTop`, `MemBlockTop`, or other narrow subsystem tops when the bug is local
- Direct Verilator compilation of a small generated DUT, if the repo already supports that path

Strong preference:

- **Prefer module-level or subsystem-level tests**
- Avoid full-chip `SimTop`, `make emu`, `make gsim`, or other full-system elaboration unless the bug truly cannot be reproduced any other way

Reason:

- full-chip XiangShan elaboration can take **30+ minutes** and **>16 GB RAM**
- such cases are poor benchmark instances if a smaller real reproducer exists

If a test cannot be made real and fast enough, report failure. Do not fake success with static file inspection.

---

## Hard constraints

1. `tb_script.sh` must `cd /home/xiangshan`
2. `tb_script.sh` must emit `TEST:` markers in the exact format:

   `TEST: <name> ... PASS|FAIL|SKIP`

3. All markers must be inside:

   ```bash
   echo "HWE_BENCH_RESULTS_START"
   ...
   echo "HWE_BENCH_RESULTS_END"
   ```

4. Base run must fail:
   - non-zero exit code
   - at least one `TEST: ... FAIL`
5. Fix run must pass:
   - exit code 0
   - at least one `TEST: ... PASS`
6. `tb_script.sh` and `prepare_script.sh` may only write temporary files, build outputs, and logs under `/home/xiangshan`
7. Do not read `fix.patch`, `pr_meta.json`, `git diff`, or any static text file to decide PASS/FAIL
8. PASS/FAIL must come from a real build, elaboration, unit test, Verilator compile, or simulation path
9. If one `tb_script.sh` run takes more than **1200 seconds**, mark the case as failure with `failure.stage = "timeout"`
10. `prepare_script.sh`, if you output it, replaces the default **stage 1-4** completely. The framework appends stage 5 automatically. Do not write stage 5 yourself.

---

## Common XiangShan pitfalls

- Do not assume SBT. XiangShan uses **Mill**, not SBT.
- Do not rely on stale `out/`, `.mill-*`, or previous elaboration output. Rebuild deterministically.
- Chisel elaboration can consume large JVM heaps. If you truly need to tune memory, do it explicitly and minimally.
- `make emu` often pulls in NEMU/difftest/full-system dependencies. Treat it as a last resort.
- Newer commits may need Verilator `v5.008`; older commits usually stay on `v4.210`. Reuse the preinstalled versions instead of rebuilding unnecessarily.
- If a bug can be exposed by a small Scala test or narrow top elaboration, that is preferred over any whole-core Linux/program boot flow.

---

## Recommended workflow

### 1. Read metadata and classify the bug

Use `{PR_DIR}/pr_meta.json` and `{PR_DIR}/fix.patch` to answer:

- Which subsystem is affected: frontend, FTQ, uncache, config generation, cache, CSR, etc.
- Is there already a small test target near the changed Scala files?
- Can you prove the bug with unit-test/elaboration failure instead of full-chip simulation?

### 2. Start one persistent work container

Recommended:

```bash
CTR="tbgen-xiangshan-{NUMBER}-$$"
docker run -d --rm --init --name "$CTR" \
  -v {PR_DIR}:/workspace/pr \
  {BASE_IMAGE} \
  tail -f /dev/null
```

Use the same container for all iterations. Save container setup output to `{PR_DIR}/logs/docker_build.txt`.

### 3. Recreate the default prepare flow in-container

The default XiangShan harness already does:

- checkout `base_sha`
- update submodules
- detect `MILL_VERSION`
- detect/select `JAVA_HOME`
- detect/select `VERILATOR_ROOT`
- install Python deps and toolchain

Only write `prepare_script.sh` if that default flow is insufficient for the specific commit.

### 4. Build the smallest real reproducer

Good candidates:

- existing Scala/Chisel tests under `src/test/scala`
- `mill ... testOnly ...`
- `Makefile.test`
- wrapper-top elaboration of the directly affected subsystem
- direct Verilator build of a small generated DUT

Bad default choice:

- full `SimTop` / `make emu` / full-program difftest flow, unless there is no smaller honest reproducer

### 5. Validate base FAIL and fix PASS

Within the same container:

1. reset to base
2. run `tb_script.sh`, capture `{PR_DIR}/logs/test_run_base.txt`
3. reset to base again
4. apply `{PR_DIR}/fix.patch`
5. run `tb_script.sh`, capture `{PR_DIR}/logs/test_run_fix.txt`

### 6. Write result.json

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
  "failure": null
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
    "stage": "analysis|prepare|build_image|test_run|fix_run|timeout|no_repro",
    "reason": "short explanation",
    "last_error_excerpt": "key log excerpt",
    "attempts": [
      "attempt 1 summary",
      "attempt 2 summary"
    ]
  }
}
```

Always write the final script contents into `result.json`, even on failure.

---

## Final reminder

Your benchmark case must test **observable behavior**, not the exact implementation in `fix.patch`.

If a different but functionally correct Scala fix would still pass your `tb_script.sh`, the design is good.
If your script only passes with the ground-truth patch structure, rewrite it.
