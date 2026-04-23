You will complete the "script generation + in-container verification" loop for a **single** `lowRISC/ibex` PR, and write a structured result to `{PR_DIR}/result.json`.

Execute strictly in order, at most **3 iteration rounds**. If it still fails after three rounds, you must stop and emit a failure report — no infinite loops.

---

## Input

Input data comes from files under `{PR_DIR}` (prepared by an external script). During the analysis phase you may read these files on the host, use `gh` to inspect PR/issue discussions, and search public technical material; **the core work (checkout, environment setup, compilation, simulation, verification) must be done inside a Docker container**.

- `{PR_DIR}/pr_meta.json`: metadata including `org/repo/number/base_sha/title/body/resolved_issues`
- `{PR_DIR}/fix.patch`: the ground-truth fix patch (used to verify PASS)
- `{BASE_IMAGE}`: the pre-built ibex base image. **You do not need to build a base image yourself.**

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

1) **`tb_script.sh` must `cd /home/ibex`**, and must emit `TEST:` markers in the exact format `TEST: <name> ... PASS|FAIL|SKIP`. All `TEST:` markers must be enclosed between the boundary markers `echo "HWE_BENCH_RESULTS_START"` and `echo "HWE_BENCH_RESULTS_END"`, so that the log parser can extract test results precisely and exclude build-log noise.
2) Running on `base_sha` must FAIL (non-zero exit code, and at least one `TEST: ... FAIL` in the log).
3) After applying `fix.patch`, it must PASS (exit code 0, and at least one `TEST: ... PASS` in the log).
4) Networking is allowed (environment preparation and build phases may download toolchains / dependencies).
5) `prepare_script` **must default to an empty string** unless the default environment preparation is insufficient. If you do output a `prepare_script`, it represents a **full replacement of stage 1-4**; **stage 5 is appended automatically by the framework** (truncating git history + recording the baseline commit). You must not and need not write stage 5 yourself.
6) **All file paths in `tb_script.sh` and `prepare_script` must be based on `/home/ibex`.** Build artifacts, logs, and temporary files must all go under `/home/ibex/` (e.g., `/home/ibex/.tb_xxx/`). **Never** reference `/workspace/pr` or any container mount path inside your scripts — those paths exist only during the generation phase and are not available during the formal harness run.
7) **`tb_script.sh` must decide PASS/FAIL through a real build/simulation flow.** The script must invoke commands that build or run the ibex DUT (such as `fusesoc`, `verilator`, `make`, `gcc`, etc.). **Decision precedence**: prefer the exit code of the build/simulation command (most reliable); only when the exit code cannot distinguish PASS/FAIL (e.g., the simulator always returns 0) should you fall back to `grep` on the runtime log. Testbench design should prefer having the simulation program reflect the result via its exit code (e.g., `return pass ? 0 : 1` in a C++ harness, or `$fatal` / `$finish(0)` vs `$finish(1)` in SystemVerilog), rather than relying on fragile string matching — such matching is brittle and easily breaks on format changes. **Never** read, parse, or match any static file content to decide PASS/FAIL, including but not limited to: RTL/SV/C source, fix.patch, test.patch, pr_meta.json, `git diff`/`git status` output, or any text file in the repository. A script that performs only static text checks is considered a failure.
8) **If you cannot reliably reproduce "base FAIL / fix PASS" through a real build/simulation, you must emit a failure.** Do not cobble together a "success" via static text matching, patch inspection, `git diff`, or other surrogates. For scenarios unsupported by the current environment (e.g., lint/formal only, extremely complex stimulus required, toolchain version incompatibility), honestly mark the case as failure.
9) **If a single execution of `tb_script.sh` (compilation + simulation combined) exceeds 20 minutes (1200s), the case must be marked as failure** because such runs are unsuitable as benchmark instances. Use `failure.stage = "timeout"` for this type of failure.
10) **Implementation-independence (extremely important)**: `tb_script.sh` must verify **observable behavior**, not a **specific implementation**. The generated tb_script must let any functionally equivalent correct fix pass, not just the specific implementation in `fix.patch`. Specifically:
  - If tb_script instantiates the module being fixed (e.g., `ibex_id_stage`, `ibex_core`, etc.), it **must not** assume the module's port list is identical to the golden fix. Different correct fixes may add, rename, or remove internal ports. Use a wrapper to fix external interface connections, or test via top-level integration.
  - Checkers/assertions in tb_script **must not** directly reference internal signal paths of the module being fixed (e.g., `dut.u_id_stage.regfile_we`), because different implementations may use different internal signal names or structures. Decide via RVFI trace interfaces, external read ports of the register file, or top-level observable signals.
  - If tb_script uses struct literals to drive signals (e.g., `'{field1, field2}`), **do not** assume the struct's field count and order match the golden fix. Use a clear-then-assign-field-by-field approach, or interact at a behavioral protocol level.
  - **Self-check**: after generating tb_script, ask yourself: "If a different developer fixed the same bug with a completely different implementation (different signal names, different conditional expressions, different module decomposition), could this tb_script still correctly decide PASS?" If the answer is no, rewrite it.

If you need to understand harness implementation details, you can read the following source files:

- ibex image build and runtime script definitions: `{REPO_ROOT}/hwe_bench/harness/repos/verilog/ibex/ibex.py`
- docker_runner's build_image / run_instance logic: `{REPO_ROOT}/hwe_bench/harness/docker_runner.py`
- docker build / run wrapper: `{REPO_ROOT}/hwe_bench/utils/docker_util.py`
- log parser (TEST marker parsing): `{REPO_ROOT}/hwe_bench/harness/repos/verilog/common.py`

---

## Common tb_script patterns (choose the most appropriate one for the bug type)

The three patterns below escalate by "how much existing test infrastructure you reuse". **Selection priority**: first see whether an existing build/test command in the project can trigger the bug directly (Pattern A); then see whether you can compose a reproducer based on the existing test framework (Pattern B); and only then consider building a test from scratch (Pattern C).

### Pattern A: Run an existing command directly

**When to use**: the bug causes an existing target to fail to build, or an existing regression test already covers the buggy path.
**Core strategy**: find a `fusesoc` target / `make` target / existing test command that triggers the bug, run it directly, and decide based on the exit code or the existing test framework's output.

```bash
#!/bin/bash
cd /home/ibex
set +e
fusesoc --cores-root=. run --target=sim --setup --build \
  lowrisc:ibex:ibex_simple_system [--relevant config parameters]
rc=$?
echo "HWE_BENCH_RESULTS_START"
if [ $rc -eq 0 ]; then
  echo "TEST: <descriptive_test_name> ... PASS"
else
  echo "TEST: <descriptive_test_name> ... FAIL"
fi
echo "HWE_BENCH_RESULTS_END"
exit $rc
```

### Pattern B: Compose an existing framework + post-processing

**When to use**: the project has a usable simulation framework (e.g., `simple_system`), but no existing test directly covers the bug.
**Core strategy**: reuse the existing framework (Makefile template, simulation target, runtime library) → add a minimal stimulus program → run the simulation → decide by exit code preferentially.

```bash
#!/bin/bash
cd /home/ibex

mkdir -p examples/sw/simple_system/<test_dir>
cat > examples/sw/simple_system/<test_dir>/<test>.c << 'EOF'
#include "simple_system_common.h"
int main(void) {
    // Write the minimum code that triggers the bug.
    // Use return 1 or raise an exception when the bug is triggered; return 0 after fix.
    return 0;
}
EOF
cat > examples/sw/simple_system/<test_dir>/Makefile << 'EOF'
PROGRAM = <test>
PROGRAM_DIR := $(shell dirname $(realpath $(lastword $(MAKEFILE_LIST))))
include ${PROGRAM_DIR}/../common/common.mk
EOF
make -C examples/sw/simple_system/<test_dir>

cd /home/ibex
fusesoc --cores-root=. run --target=sim --tool=verilator --setup --build \
  lowrisc:ibex:ibex_simple_system

set +e
./build/lowrisc_ibex_ibex_simple_system_0/sim-verilator/Vibex_simple_system \
  --meminit=ram,examples/sw/simple_system/<test_dir>/<test>.elf
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

**When to use**: the bug cannot be triggered through an existing framework (e.g., debug-mode logic, specific signal timing), and you need to instantiate the DUT directly and construct hardware-level stimulus.
**Core strategy**: build the "three pieces" yourself (SV testbench + C++ harness + `.core` file) → build and run via `fusesoc` → decide by exit code. The C++ harness should `return 0` on pass and `return 1` on fail, so that the simulation program's exit code directly reflects the test result. Check the DUT port list against the `base_sha` version.

```bash
#!/bin/bash
cd /home/ibex

TB_DIR=/home/ibex/.tb_<test_name>
mkdir -p "$TB_DIR"

cat > "$TB_DIR/tb.sv" <<'EOF'
...  // Instantiate DUT + minimal stimulus logic
// Report result to the C++ harness via output pass_o / done_o
EOF
cat > "$TB_DIR/tb.cc" <<'EOF'
// Verilator harness: drive clock, observe behavior.
// Key point: let the exit code reflect the test result.
// return top.pass_o ? 0 : 1;
EOF
cat > /home/ibex/<test_name>.core <<'EOF'
...  // fusesoc core description: depend on ibex_core, specify testbench files
EOF

fusesoc --cores-root=. run --target=sim --setup --build lowrisc:ibex:<test_name>
SIM_BIN="$(find build -path '*/sim-verilator/V*' | head -n 1)"

set +e
"$SIM_BIN"
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
   - `gh pr view {NUMBER} -R lowRISC/ibex --json body,comments,reviews`
   - `gh issue view <issue_number> -R lowRISC/ibex`
   - Check whether later commits on top of this PR provide tests or alternative fix ideas
   - Search relevant technical material (e.g., RISC-V Debug Spec, ibex documentation)
3) Select the most appropriate `tb_script` pattern (A/B/C)

Requirement: the script must be fast and deterministic; avoid long random testing.

### Step 2: Start a persistent work container

Start a persistent container from `{BASE_IMAGE}` with `{PR_DIR}` mounted. Recommended command:

```bash
CTR="tbgen-ibex-{NUMBER}-$$"
docker run -d --rm --init --name "$CTR" \
  -v {PR_DIR}:/workspace/pr \
  {BASE_IMAGE} \
  tail -f /dev/null
```

All subsequent checkout, environment setup, compilation, simulation, and verification must happen inside this container. Delete the container when done.

### Step 3: Set up the environment inside the container (stage 1-4)

Via `docker exec`, execute inside the container the steps corresponding to the default `prepare_dev` semantics:

1) Checkout `base_sha` and update submodules
2) Install the toolchain (prefer the official `ci/install-build-deps.sh`; only fall back to manual debugging and fixes if it fails)
3) Persist PATH
4) Apply PATH in the current shell

You can run these stages step by step for debugging, or execute an equivalent batch command once you have confirmed the default template works.

Output rules for `prepare_script`:

- If default environment preparation works: leave `prepare_script.sh` empty
- If customization is needed: output the **full replacement of stage 1-4**
- **Do not** hand-write stage 5; the framework automatically appends the canonical finalize part (truncate git history + record baseline commit) at the end of the final `prepare.sh`

### Step 4: Iterate on tb_script inside the same container

Write and debug `tb_script.sh` inside the container. Place all build artifacts and temporary files under `/home/ibex/` (e.g., `/home/ibex/.tb_xxx/`):

1) Write or modify `tb_script.sh`
2) Run the compilation / simulation
3) Check the exit code and logs
4) Modify and rerun

It is recommended to save logs under `/home/ibex/` (e.g., `/home/ibex/.tb_xxx/build.log`).

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
  "org": "lowRISC",
  "repo": "ibex",
  "number": 2261,
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
  "org": "lowRISC",
  "repo": "ibex",
  "number": 2261,
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
