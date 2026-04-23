You are generating a benchmark case for **one** `{ORG}/{REPO}` pull request, PR **#{NUMBER}**, base commit **{BASE_SHA}**.

Your job is to produce a real `tb_script.sh` and, only if necessary, a `prepare_script.sh`, then validate that they reproduce the bug under the standard harness flow and write `{PR_DIR}/result.json`.

You must complete a closed loop:

1. Read `{PR_DIR}/pr_meta.json` and `{PR_DIR}/fix.patch`
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
- `{BASE_IMAGE}`: prebuilt rocket-chip base image

You may read repository code under `{REPO_ROOT}` on the host, use `gh`, and search public docs. All actual checkout, build, elaboration, testing, and validation must happen inside Docker.

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

## Rocket-chip container facts

Inside `{BASE_IMAGE}`:

- OS: Ubuntu 22.04
- Repo root: `/home/rocket-chip`
- JDK 8, JDK 11, and JDK 17 are installed
- `JAVA_HOME` is selected during prepare from repo metadata, primarily `project/build.properties`
- `sbt` is available and prefers the repo-local `sbt-launch.jar`; the effective SBT version is auto-detected from `project/build.properties`
- Verilator `v4.210` is preinstalled at `/tools/verilator`
- Additional Verilator versions can be built on demand by the harness if needed
- RISC-V toolchain is under `/tools/riscv`
- Common environment variables you may rely on:
  - `JAVA_HOME`
  - `VERILATOR_ROOT=/tools/verilator`
  - `RISCV=/tools/riscv`
  - if a repo script expects `RISCV_HOME`, set `RISCV_HOME=/tools/riscv` in your own script

Important runtime fact:

- `/workspace/pr` is mounted only during **tbgen generation**. It does **not** exist during benchmark evaluation. Your final `tb_script.sh` and `prepare_script.sh` must not reference `/workspace/pr` or any host mount path.

---

## Rocket-chip-specific strategy

rocket-chip is a **Chisel/Scala + SBT** project. The repair agent usually edits `.scala` files, and your `tb_script.sh` should use a **real SBT-driven build or test flow** to distinguish base from fixed behavior.

Strong preference:

- Prefer **module-level or subsystem-level** validation
- Prefer `sbt testOnly ...` for existing Scala/Chisel tests
- Prefer `sbt runMain ...` when the bug is easiest to expose through a narrow elaboration or generator entry point
- Keep the reproducer tied to the affected module, package, or config

Avoid by default:

- the full `regression/` suite
- `make -C regression ...` over broad buckets
- whole-repo or whole-SoC regressions when a smaller real reproducer exists

Reason:

- the first SBT compile can be slow because dependency resolution, Zinc analysis, and Scala compilation all happen together
- a full rocket-chip regression is much slower and much less stable than a focused `testOnly` or `runMain`

If a bug cannot be reproduced honestly with a reasonably small real flow, report failure instead of faking success.

---

## Hard constraints

1. `tb_script.sh` must `cd /home/rocket-chip`
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
6. `tb_script.sh` and `prepare_script.sh` may only write temporary files, build outputs, and logs under `/home/rocket-chip`
7. Do not read `fix.patch`, `pr_meta.json`, `git diff`, or any static file to decide PASS/FAIL
8. PASS/FAIL must come from a real SBT compile, SBT test, elaboration, Verilator build, or simulation path
9. If one `tb_script.sh` run takes more than **1200 seconds**, mark the case as failure with `failure.stage = "timeout"`
10. `prepare_script.sh`, if you output it, replaces the default **stage 1-4** completely. The framework appends stage 5 automatically. Do not write stage 5 yourself.

---

## Common rocket-chip pitfalls

- The first `sbt` invocation can spend a long time downloading dependencies and compiling build definitions. This is normal; do not mistake that for a functional failure.
- SBT caches are persistent across the prepared image, but `sbt clean` or broad target selection can still make runs much slower than necessary.
- Some repo scripts assume submodules are present. Missing submodules are a real failure mode for old commits.
- Prefer quoting SBT commands explicitly, for example:
  - `sbt "testOnly freechips.rocketchip.rocket.CSRSpec"`
  - `sbt "runMain freechips.rocketchip.system.Generator ..."`
- Avoid using a full repository regression when a narrow elaboration or a specific test class already covers the changed logic.
- Early PRs, especially roughly **#18-#500**, may have broken historical submodule URLs. If checkout cannot fully restore those repos, report failure honestly instead of inventing a fake reproducer.

---

## Recommended workflow

### 1. Read metadata and classify the bug

Use `{PR_DIR}/pr_meta.json` and `{PR_DIR}/fix.patch` to answer:

- Which subsystem is affected: CSR, TLB/PTW, cache/coherence, diplomacy/config elaboration, interrupt/exception logic, tile parameterization, or a local utility
- Is there already a small Scala test near the changed files?
- Can you prove the bug with a small `testOnly` target or a focused `runMain` elaboration instead of a regression script?

### 2. Start one persistent work container

Recommended:

```bash
CTR="tbgen-rocketchip-{NUMBER}-$$"
docker run -d --rm --init --name "$CTR" \
  -v {PR_DIR}:/workspace/pr \
  {BASE_IMAGE} \
  tail -f /dev/null
```

Use the same container for all iterations. Save container setup output to `{PR_DIR}/logs/docker_build.txt`.

### 3. Recreate the default prepare flow in-container

The default rocket-chip harness already does:

- checkout `base_sha`
- update submodules recursively
- detect the required `JAVA_HOME`
- detect the effective SBT version from `project/build.properties`
- select `VERILATOR_ROOT`
- install Python helper deps and expose the RISC-V toolchain

Only write `prepare_script.sh` if that default flow is insufficient for the specific commit.

### 4. Build the smallest real reproducer

Good candidates:

- existing Scala tests under `src/test/scala`
- `sbt "testOnly ..."`
- `sbt "runMain ..."` for narrow elaboration or config generation checks
- a small generator invocation that reaches the changed module without launching the full regression suite

Bad default choice:

- `regression/run-test-bucket`
- broad `make -C regression ...`
- large end-to-end regressions when the bug is local to one generator path

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
