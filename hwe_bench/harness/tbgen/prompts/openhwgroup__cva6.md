You will complete the "script generation + in-container verification" loop for a **single** `openhwgroup/cva6` PR, and write a structured result to `{PR_DIR}/result.json`.

Execute strictly in order, at most **3 iteration rounds**. If it still fails after three rounds, you must stop and emit a failure report — no infinite loops.

---

## Input

Input data comes from files under `{PR_DIR}` (prepared by an external script). During the analysis phase you may read these files on the host, use `gh` to inspect PR/issue discussions, and search public technical material; **the core work (checkout, environment setup, compilation, simulation, verification) must be done inside a Docker container**.

- `{PR_DIR}/pr_meta.json`: metadata including `org/repo/number/base_sha/title/body/resolved_issues`
- `{PR_DIR}/fix.patch`: the ground-truth fix patch (used to verify PASS)
- `{BASE_IMAGE}`: the pre-built cva6 base image. **You do not need to build a base image yourself.**

---

## Known container facts (reuse first, install later)

`{BASE_IMAGE}` comes pre-installed with the main Verilator versions commonly used by cva6. Before starting, assume these tools are already present and reuse them rather than recompiling from scratch.

- Pre-installed Verilator: `/tools/verilator-v5.008`, `/tools/verilator-v5.018`
- Default symlink: `/tools/verilator -> /tools/verilator-v5.008`
- Tool manifest: `/tools/cva6_tool_manifest.txt`
- Environment variable already set: `NUM_JOBS=4`

The `RISC-V` toolchain and `Spike` are **not** pre-installed and must still be installed in the container on demand.

You must first check the Verilator version required by the current `base_sha`:

1) First check `verif/regress/install-verilator.sh`
2) If absent, check `ci/install-verilator.sh`
3) Read out the required Verilator version from the script (e.g., `VERILATOR_HASH="v5.008"` or `VERILATOR_HASH="v5.018"`)
4) Prefer switching to the matching pre-installed version; only when the required version is not under `/tools` should you emit a `prepare_script.sh` that installs the missing version

Hard prohibitions:

- **Do not** `apt install verilator`
- **Do not** delete or overwrite an existing `/tools/verilator-*`
- **Do not** install Spike for an unnecessary full-core flow

For cva6, prefer a **module-level / subsystem-level direct Verilator** approach that does not depend on Spike; only when the bug's observable behavior can only be reliably triggered via a full-core software / `Variane_testharness` flow should you fall back to the full-core testharness + Spike as a last resort.

---

## Output (you must write files)

You must produce the following under `{PR_DIR}`:

1) `{PR_DIR}/tb_script.sh`: the test script that triggers the bug
2) (optional) `{PR_DIR}/prepare_script.sh`: write this only when the default environment preparation is insufficient; otherwise do not create it or leave it empty
3) `{PR_DIR}/result.json`: the final structured result (see schema below)

Also save key logs under `{PR_DIR}/logs/`:

- `docker_build.txt`: container startup and environment-preparation output
- `test_run_base.txt`: the log from the base FAIL run
- `test_run_fix.txt`: the log from the fix PASS run

---

## Hard constraints

1) **`tb_script.sh` must `cd /home/cva6`**, and must emit `TEST:` markers in the exact format `TEST: <name> ... PASS|FAIL|SKIP`. All `TEST:` markers must be enclosed between the boundary markers `echo "HWE_BENCH_RESULTS_START"` and `echo "HWE_BENCH_RESULTS_END"`, so that the log parser can extract test results precisely and exclude build-log noise.
2) Running on `base_sha` must FAIL (non-zero exit code, and at least one `TEST: ... FAIL` in the log).
3) After applying `fix.patch`, it must PASS (exit code 0, and at least one `TEST: ... PASS` in the log).
4) Networking is allowed (environment preparation and build phases may download toolchains / dependencies).
5) `prepare_script` **must default to an empty string** unless the default environment preparation is insufficient. If you do output a `prepare_script`, it represents a **full replacement of stage 1-4**; **stage 5 is appended automatically by the framework** (truncating git history + recording the baseline commit). You must not and need not write stage 5 yourself.
6) **All file paths in `tb_script.sh` and `prepare_script` must be based on `/home/cva6`.** Build artifacts, logs, and temporary files must all go under `/home/cva6/` (e.g., `/home/cva6/.tb_xxx/`). **Never** reference `/workspace/pr` or any container mount path inside your scripts — those paths exist only during the generation phase and are not available during the formal harness run.
7) **`tb_script.sh` must decide PASS/FAIL through a real build/simulation flow.** The script must invoke commands that build or run the cva6 DUT (such as `make verilate`, `verilator`, `gcc`, `python3 verif/sim/cva6.py`, `bash verif/regress/*.sh`, etc.). **Decision precedence**: prefer the exit code of the build/simulation command; only when the exit code cannot distinguish PASS/FAIL should you fall back to `grep` on the runtime log. Prefer having the simulation program itself reflect the result via its exit code (e.g., `return 0/1` from a C++ harness, or `$fatal` / `$finish(0)` vs `$finish(1)` in SystemVerilog), rather than relying on fragile string matching. **Never** read, parse, or match any static file content to decide PASS/FAIL, including but not limited to: RTL/SV/C source, fix.patch, test.patch, pr_meta.json, `git diff`/`git status` output, or any text file in the repository. A script that performs only static text checks is considered a failure.
8) **If you cannot reliably reproduce "base FAIL / fix PASS" through a real build/simulation, you must emit a failure.** Do not cobble together a "success" via static text matching, patch inspection, `git diff`, or other surrogates. For scenarios unsupported by the current environment (e.g., lint/formal only, extremely complex stimulus required, incompatible Verilator version), honestly mark the case as failure.
9) **If a single execution of `tb_script.sh` (compilation + simulation combined) exceeds 20 minutes (1200s), the case must be marked as failure** because such runs are unsuitable as benchmark instances. Use `failure.stage = "timeout"` for this type of failure.
10) **Implementation-independence (extremely important)**: `tb_script.sh` must verify **observable behavior**, not a **specific implementation**. The generated tb_script must let any functionally equivalent correct fix pass, not just the specific implementation in `fix.patch`. Specifically:
  - If tb_script instantiates `cva6`, `cva6_tb_wrapper`, or related submodules, it **must not** assume the module's port list is identical to the golden fix. Different correct fixes may add, rename, or remove internal ports. Use a wrapper to fix external interface connections, or use a more stable top-level integration for testing.
  - Checkers/assertions in tb_script **must not** directly reference internal signal paths of the DUT (e.g., `dut.u_csr_regfile.some_internal_signal`), because different implementations may use different internal signal names or structures. Prefer decisions based on ISA-observable behavior, program execution results, top-level ports, existing trace outputs, or behavioral comparison against a Spike golden reference.
  - If tb_script uses struct literals, interface bundles, or large aggregate assignments, **do not** assume the field count and order match the golden fix. Use a more robust field-by-field assignment or behavior-level interaction.
  - **Self-check**: after generating tb_script, ask yourself: "If a different developer fixed the same bug with a completely different implementation (different signal names, different conditional expressions, different module decomposition), could this tb_script still correctly decide PASS?" If the answer is no, rewrite it.

If you need to understand harness implementation details, you can read the following source files:

- cva6 image build and runtime script definitions: `{REPO_ROOT}/hwe_bench/harness/repos/verilog/cva6/cva6.py`
- docker_runner's build_image / run_instance logic: `{REPO_ROOT}/hwe_bench/harness/docker_runner.py`
- docker build / run wrapper: `{REPO_ROOT}/hwe_bench/utils/docker_util.py`
- log parser (TEST marker parsing): `{REPO_ROOT}/hwe_bench/harness/repos/verilog/common.py`

---

## Common tb_script patterns (choose the most appropriate one for the bug type)

The three patterns below escalate by "how much existing test infrastructure you reuse". **Selection priority**: first see whether an existing build/test command in the project can trigger the bug directly (Pattern A); then see whether you can compose a reproducer based on an existing Verilator testbench / simulation script (Pattern B); and only then consider building a test from scratch (Pattern C).

Practical experience on cva6: if the bug lands in a local module or subsystem such as `decoder.sv`, `alu.sv`, `frontend/*`, `cache_subsystem/*`, you should usually **prefer a minimal wrapper + direct Verilator** approach; do not jump straight to full-core testharness + Spike.

### Pattern A: Run an existing command directly

**When to use**: the bug causes an existing `make` target or `verif/regress/` script to fail directly, or there is an existing command in the repo that already covers the buggy path.
**Core strategy**: find an existing Verilator flow that triggers the bug, run it directly, and decide based on the exit code or the existing test framework's output.

```bash
#!/bin/bash
cd /home/cva6
set +e
make -j$(nproc) verilate target=cv32a6_imac_sv32
rc=$?
echo "HWE_BENCH_RESULTS_START"
if [ $rc -eq 0 ]; then
  echo "TEST: verilator_build_cv32a6_imac_sv32 ... PASS"
else
  echo "TEST: verilator_build_cv32a6_imac_sv32 ... FAIL"
fi
echo "HWE_BENCH_RESULTS_END"
exit $rc
```

### Pattern B: Compose an existing framework + post-processing

**When to use**: the project has a usable Verilator/testharness framework, but no existing test directly covers the bug.
**Core strategy**: reuse the existing `verif/sim/` / `verif/regress/` flow, add a minimal C/asm stimulus, optionally enable `veri-testharness,spike` for behavioral comparison; prefer exit-code-based decisions.

```bash
#!/bin/bash
cd /home/cva6

TEST_DIR=/home/cva6/.tb_<test_name>
mkdir -p "$TEST_DIR"
cat > "$TEST_DIR/test.c" <<'EOF'
int main(void) {
    // Write the smallest trigger program here.
    // Return 0 on correct behavior, non-zero on buggy behavior.
    return 0;
}
EOF

source verif/sim/setup-env.sh
cd verif/sim

set +e
python3 cva6.py \
  --target cv32a6_imac_sv32 \
  --iss=veri-testharness,spike \
  --iss_yaml=cva6.yaml \
  --c_tests "$TEST_DIR/test.c"
rc=$?
set -e

echo "HWE_BENCH_RESULTS_START"
if [ "$rc" -eq 0 ]; then
  echo "TEST: <test_name> ... PASS"
else
  echo "TEST: <test_name> ... FAIL"
fi
echo "HWE_BENCH_RESULTS_END"
exit "$rc"
```

### Pattern C: Build a test from scratch

**When to use**: the bug cannot be reliably triggered via existing scripts, and you need to instantiate the DUT directly and construct hardware-level stimulus.
**Core strategy**: build the "three pieces" yourself (SV testbench + C++ harness + Verilator build command) → run Verilator directly → decide by exit code. The C++ harness should `return 0` on pass and `return 1` on fail.

```bash
#!/bin/bash
cd /home/cva6

TB_DIR=/home/cva6/.tb_<test_name>
mkdir -p "$TB_DIR"

cat > "$TB_DIR/tb.sv" <<'EOF'
// Instantiate cva6_tb_wrapper or cva6 directly.
// Drive the minimum stimulus needed to trigger the bug.
EOF

cat > "$TB_DIR/tb.cpp" <<'EOF'
// Verilator harness: drive clock/reset and return 0/1 by observed behavior.
EOF

set +e
verilator --cc --exe --build \
  -Icore/include \
  -Iverif/tb/core \
  "$TB_DIR/tb.sv" "$TB_DIR/tb.cpp"
rc=$?
set -e

echo "HWE_BENCH_RESULTS_START"
if [ "$rc" -eq 0 ]; then
  echo "TEST: <test_name> ... PASS"
else
  echo "TEST: <test_name> ... FAIL"
fi
echo "HWE_BENCH_RESULTS_END"
exit "$rc"
```

---

## Recommended workflow (follow it)

### Step 0: Read PR info

Read and parse `{PR_DIR}/pr_meta.json` to obtain:

- `base_sha`
- PR description (title/body/issues)
- Path to the input patch (fix.patch)

### Step 1: Understand the bug on the host

On the host (outside the container) you may:

1) Read `{PR_DIR}/fix.patch` to understand the fix point and trigger condition. If `fix.patch` contains modifications to regression tests, testbenches, or helper files, they can inform your test design.
2) Use `gh` or web search for more context, for example:
   - `gh pr view {NUMBER} -R openhwgroup/cva6 --json body,comments,reviews`
   - `gh issue view <issue_number> -R openhwgroup/cva6`
   - Check whether later commits on top of this PR provide tests or alternative fix ideas
   - Search relevant technical material (e.g., RISC-V Privileged Spec, PMP/CSR specs, CVA6 docs)
3) Select the most appropriate `tb_script` pattern (A/B/C)

Requirement: the script must be fast and deterministic; avoid long random testing.

### Step 2: Start a persistent work container

Start a persistent container from `{BASE_IMAGE}` with `{PR_DIR}` mounted. Recommended command:

```bash
CTR="tbgen-cva6-{NUMBER}-$$"
docker run -d --rm --init --name "$CTR" \
  -v {PR_DIR}:/workspace/pr \
  {BASE_IMAGE} \
  tail -f /dev/null
```

All subsequent checkout, environment setup, compilation, simulation, and verification must happen inside this container. Delete the container when done.

### Step 3: Set up the environment inside the container (stage 1-4)

Via `docker exec`, execute inside the container the steps corresponding to the default `prepare_dev` semantics:

1) Checkout `base_sha` and update submodules
2) Install minimal Python dependencies
3) Install a pre-built RISC-V toolchain
4) Detect the Verilator version required by the current commit and pick the matching pre-installed version; install the missing version only if needed. The `RISC-V` toolchain and Spike are still installed dynamically on demand, and PATH / library path / include path must be persisted.

Output rules for `prepare_script`:

- If default environment preparation works: leave `prepare_script.sh` empty
- If customization is needed: output the **full replacement of stage 1-4**
- **Do not** hand-write stage 5; the framework automatically appends the canonical finalize part (truncate git history + record baseline commit) at the end of the final `prepare.sh`

Additional constraints:

- If you need a custom `prepare_script.sh`, first reuse `/tools/verilator-v5.008` and `/tools/verilator-v5.018`
- Only install a new Verilator version when the version required by the current `base_sha` is not under `/tools`
- If you install Spike for an old commit, check both `ci/install-spike.sh` and `verif/regress/install-spike.sh` — don't look at only one

### Step 4: Iterate on tb_script inside the same container

Write and debug `tb_script.sh` inside the container. Place all build artifacts and temporary files under `/home/cva6/` (e.g., `/home/cva6/.tb_xxx/`):

1) Write or modify `tb_script.sh`
2) Run the compilation / simulation
3) Check the exit code and logs
4) Modify and rerun

It is recommended to save logs under `/home/cva6/` (e.g., `/home/cva6/.tb_xxx/build.log`).

### Step 5: Verify f2p (base FAIL / fix PASS)

Complete the verification inside the same container. Recommended order:

1) Return to `base_sha` and clean the worktree
2) Run `tb_script.sh`; write the log to `{PR_DIR}/logs/test_run_base.txt`; must FAIL
3) Return to `base_sha` again and clean the worktree
4) Apply `fix.patch`
5) Run `tb_script.sh` again; write the log to `{PR_DIR}/logs/test_run_fix.txt`; must PASS
6) Write the final `tb_script` / `prepare_script` / failure info into `{PR_DIR}/result.json`

Tip: if you only modified `tb_script.sh`, no image rebuild is needed; just rerun in the current work container.

### Step 6: Finish and clean up

After confirming all output files under `{PR_DIR}` are complete, delete the work container.

---

## Iteration strategy (at most 3 rounds)

After round 1 fails, prefer to fix in the following order:

1) Only tweak the trigger condition and decision logic in `tb_script.sh` (fastest)
2) If there are missing-tool / missing-dependency issues, consider emitting a custom `prepare_script.sh`
3) If after three rounds you still cannot get a stable dynamic reproducer, emit `failure.stage = "no_dynamic_reproducer"`
4) If a single execution exceeds 1200s, stop and emit `failure.stage = "timeout"`

---

## The final result.json you must emit (strict schema)

You must write `{PR_DIR}/result.json` with all fields present:

```json
{
  "status": "success",
  "org": "openhwgroup",
  "repo": "cva6",
  "number": 2015,
  "base_sha": "xxxxxxxx",
  "tb_script": "#!/bin/bash\n...",
  "prepare_script": "",
  "failure": null
}
```

On failure:

```json
{
  "status": "failure",
  "org": "openhwgroup",
  "repo": "cva6",
  "number": 2015,
  "base_sha": "xxxxxxxx",
  "tb_script": "",
  "prepare_script": "",
  "failure": {
    "stage": "tb_script|docker_build|base_fail_check|fix_pass_check|prepare_env|no_dynamic_reproducer|timeout",
    "reason": "one-sentence explanation of the root cause",
    "last_error_excerpt": "key excerpt from the last failing log",
    "attempts": [
      "what round 1 did / why it failed",
      "what round 2 did / why it failed"
    ]
  }
}
```

Note: you must write the **full content** of the final `tb_script` / `prepare_script` (as strings) back into the corresponding fields in `result.json`, so that external merge scripts can fold it back into the dataset.
