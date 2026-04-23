You will complete the "script generation + in-container verification" loop for a **single** `lowRISC/opentitan` PR, and write a structured result to `{PR_DIR}/result.json`.

Execute strictly in order, at most **3 iteration rounds**. If it still fails after three rounds, you must stop and emit a failure report — no infinite loops.

---

## Input

Input data comes from files under `{PR_DIR}` (prepared by an external script). During the analysis phase you may read these files on the host, use `gh` to inspect PR/issue discussions, and search public technical material; **the core work (checkout, environment setup, compilation, simulation, verification) must be done inside a Docker container**.

- `{PR_DIR}/pr_meta.json`: metadata including `org/repo/number/base_sha/title/body/resolved_issues`
- `{PR_DIR}/fix.patch`: the ground-truth fix patch (used to verify PASS)
- `{BASE_IMAGE}`: the pre-built OpenTitan base image. **You do not need to build a base image yourself.**

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

1) **`tb_script.sh` must `cd /home/opentitan`**, and must emit `TEST:` markers in the exact format `TEST: <name> ... PASS|FAIL|SKIP`. All `TEST:` markers must be enclosed between the boundary markers `echo "HWE_BENCH_RESULTS_START"` and `echo "HWE_BENCH_RESULTS_END"`, so that the log parser can extract test results precisely and exclude build-log noise.
2) Running on `base_sha` must FAIL (non-zero exit code, and at least one `TEST: ... FAIL` in the log).
3) After applying `fix.patch`, it must PASS (exit code 0, and at least one `TEST: ... PASS` in the log).
4) Networking is allowed (environment preparation and build phases may download toolchains / dependencies).
5) `prepare_script` **must default to an empty string** unless the default environment preparation is insufficient. If you do output a `prepare_script`, it represents a **full replacement of stage 1-4**; **stage 5 is appended automatically by the framework** (truncating git history + recording the baseline commit into `/home/opentitan_base_commit.txt`). You must not and need not write stage 5 yourself.
6) **All file paths in `tb_script.sh` and `prepare_script` must be based on `/home/opentitan`.** Build artifacts, logs, and temporary files must all go under `/home/opentitan/` (e.g., `/home/opentitan/.tb_xxx/`). **Never** reference `/workspace/pr` or any container mount path inside your scripts — those paths are only visible during the generation phase and are not available during the formal harness run.
7) **`tb_script.sh` must decide PASS/FAIL through a real build/simulation flow.** The script must invoke commands that build or run the OpenTitan DUT (preferably `./util/dvsim/dvsim.py ... -t vcs`, but also `fusesoc --tool=vcs`, `vcs`, `vlogan`, or other real VCS flows). **Decision precedence**: prefer the exit code of the build/simulation command (most reliable); only when the exit code cannot distinguish PASS/FAIL (e.g., the simulator always returns 0) should you fall back to `grep` on the runtime log. Testbench design should prefer having the simulation program reflect the result via its exit code (e.g., `$fatal` / `$finish(1)` triggers a non-zero exit code; `$finish(0)` triggers a zero exit code), rather than relying on fragile string matching — such matching is brittle and easily breaks on format changes. **Never** read, parse, or match any static file content to decide PASS/FAIL, including but not limited to: RTL/SV/C source, fix.patch, test.patch, pr_meta.json, `git diff`/`git status` output, or any text file in the repository. A script that performs only static text checks is considered a failure.
8) **If you cannot reliably reproduce "base FAIL / fix PASS" through a real build/simulation, you must emit a failure.** Do not cobble together a "success" via static text matching, patch inspection, `git diff`, or other surrogates. For scenarios unsupported by the current environment (e.g., lint/formal only, extremely complex stimulus required, DV config that strongly depends on a closed-source simulator unavailable here), honestly mark the case as failure.
9) **If a single execution of `tb_script.sh` (compilation + simulation combined) exceeds 30 minutes (1800s), the case must be marked as failure.** Use `failure.stage = "timeout"` for this type of failure.
10) **Only VCS is allowed as the simulator.** OpenTitan's `dvsim.py` supports `vcs/xcelium/questa/dsim/verilator`, but the current environment has only VCS. If a `sim_cfg.hjson` defaults to another simulator, first try to switch to VCS via `-t vcs`; if that is not possible or the flow explicitly depends on a simulator/tool that is unavailable here, report failure.
11) The container's micromamba `opentitan` environment is auto-activated via `BASH_ENV`; the VCS license server is started automatically by the image `ENTRYPOINT`. **Do not** manually `eval micromamba` / `micromamba activate` in your scripts, and do not manually manage the license server.
12) The default `prepare_script` does only four things: checkout `base_sha` + submodule update, install system packages per the repo's `apt-requirements.txt`, install Python dependencies per the repo's `python-requirements.txt`, install the RISC-V toolchain + Verible and persist PATH. If this is not enough to support `dvsim.py`, you must analyze the missing dependencies based on the specific `base_sha` and emit a custom `prepare_script.sh`.
13) **Implementation-independence (extremely important)**: `tb_script.sh` must verify **observable behavior**, not a **specific implementation**. The generated tb_script must let any functionally equivalent correct fix pass, not just the specific implementation in `fix.patch`. Specifically:
  - If tb_script uses a fallback path (a self-built testbench) to instantiate the module being fixed, it **must not** use `.*` (wildcard) bindings or assume the module's port list is identical to the golden fix. Different correct fixes may add, rename, or remove internal ports. Use a wrapper to fix external interface connections, so that any module with correct external behavior will compile.
  - If tb_script extends tests inside an existing DV env, it **must not** reference internal signal paths of the module being fixed (e.g., `tb.dut.u_xxx.signal_name`) in custom vseqs or checkers, because different implementations may use different internal signal names. Decide via CSR reads, external port observation, or external scoreboard behavior.
  - If tb_script runs an existing dvsim test directly, it **must not** depend on DV config fields or class members that only exist after applying the full golden fix via `test.patch`.
  - **Self-check**: after generating tb_script, ask yourself: "If a different developer fixed the same bug with a completely different implementation (different port names, different FSM encoding, different signal decomposition), could this tb_script still correctly decide PASS?" If the answer is no, rewrite it.
  - **Red flags (stop immediately and reassess)**: any of the following indicates coupling with a specific implementation, and you should switch to a full-IP DV path or rewrite tb_script:
    - Needing to overwrite or modify shared DV files in the repo (vseq, scoreboard, interface, autogen top, etc.)
    - A self-built local DV core starts importing internal repo DV packages, macros, or class details heavily
    - Needing to modify scoreboard semantics to "adapt" to the golden fix
    - Using signals newly added or renamed by fix.patch as observation or injection points
    - Binding a new monitor using `.*` to an unstable port list
14) **bind/force/monitor usage rules**: in a full-IP DV environment, `uvm_hdl_read`/`uvm_hdl_force`/`uvm_hdl_release` or a small bind monitor are legitimate and commonly used techniques, especially for fault injection, alert, counter corruption, and similar scenarios. But you must stay within the following bounds:
  - Use them only inside an existing DV env; do not introduce UVM HDL backdoors into a standalone tb
  - Prefer binding to long-term-stable, semantically clear state/counter/alert nodes; do not bind to signals newly added by the fix
  - Always release after force
  - Do not adapt scoreboard semantics to match the golden fix's specific behavior
  - A bind monitor should prefer observation and must not rely on an unstable port list

If you need to understand harness implementation details, you can read the following source files:

- OpenTitan image build and runtime script definitions: `{REPO_ROOT}/hwe_bench/harness/repos/verilog/opentitan/opentitan.py`
- docker_runner's build_image / run_instance logic: `{REPO_ROOT}/hwe_bench/harness/docker_runner.py`
- docker build / run wrapper: `{REPO_ROOT}/hwe_bench/utils/docker_util.py`
- log parser (TEST marker parsing): `{REPO_ROOT}/hwe_bench/harness/repos/verilog/common.py`

---

## tb_script path selection

**Core principle**: prefer the full-IP DV path (driving an existing DV environment via `dvsim.py`); only fall back to a standalone VCS testbench when it is clearly not applicable.

Practical experience shows that the full-IP DV path has much higher success rate and stability than a custom testbench: an existing DV environment comes with scoreboard, alert handler, CSR checker, TL agent, and other mature verification infrastructure, which you don't have to build from scratch, and which inherently avoids implementation coupling (because that infrastructure verifies the module's external behavior contract rather than internal implementation details).

### Pre-check: does fix.patch modify public ports of the DUT module?

Before choosing a path, first check whether `fix.patch` adds, renames, or removes public ports of the affected IP. If so:

- All dvsim tests routed through that IP's **stale DV wrapper / interface / tb.sv instantiation chain** will fail to compile due to port mismatches (`Error-[UPIMI-E] Undefined port in module instantiation`)
- Do not select a test path that goes through the stale wrapper chain
- Prefer a path that **does not go through the stale wrapper chain**: a Tier 2/3 custom vseq (observing external behavior only via CSR/interrupt/alert), or an existing chip-level/top-level test that does not pass through the wrapper
- **Do not** pre-patch the wrapper ports via `prepare_script` to work around this — doing so would turn the base FAIL into a compilation failure rather than a behavioral failure, breaking f2p semantics
- If no path can reliably reproduce base FAIL / fix PASS, emit a failure

### Preferred path: full-IP DV (dvsim-driven)

The following three tiers escalate by "amount of change". Try Tier 1 first; only move down if it doesn't work.

#### Tier 1: Directly reuse an existing test

**When to use**: an existing DV regression test for the affected IP / top-level can directly trigger the bug, or the PR/issue has already hinted at a specific test name.

**Core strategy**: locate the affected IP's `sim_cfg.hjson` and the test name, and run:

```bash
#!/bin/bash
cd /home/opentitan
set +e

./util/dvsim/dvsim.py hw/ip/<ip>/dv/<sim_cfg>.hjson \
  -i <test_name> \
  -t vcs \
  --fixed-seed=<seed> \
  --build-seed=<seed>
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

How to find the right test:

- See which RTL / DV files `fix.patch` modifies, to determine the affected IP block or top-level
- Look at the corresponding `dv/*sim_cfg*.hjson` and its listed testlist
- Look for test names mentioned in PR / issue discussions
- Note that some IPs have multiple configurations (e.g., different masking / top configurations); do not pick the wrong `sim_cfg`

#### Tier 2: Clone the closest vseq and make minimal targeted changes

**When to use**: no existing test directly covers the bug, but the IP already has a complete DV env, and you only need to start from the most similar vseq and make minimal changes (modify body / constraints / configuration).

**Core strategy**:

- Under the IP's `dv/env/seq_lib/`, find the most similar vseq
- Copy it, and make minimal changes so it triggers the bug's boundary condition
- **Do not modify** existing shared vseqs or scoreboards; only add new files
- Register the new vseq in `*_vseq_list.sv`, the testlist, and `sim_cfg.hjson`
- Still drive it via `dvsim.py -t vcs`

#### Tier 3: Add a minimal new vseq

**When to use**: the IP has a DV env but no vseq that can be cloned directly; you need to start from `<ip>_base_vseq` and write a new minimal targeted vseq.

**Core strategy**:

- Inherit `<ip>_base_vseq` and put only the minimum trigger logic in `body()`
- Include it in `*_vseq_list.sv`; register it in the testlist and `sim_cfg.hjson`
- If `fix.patch` contains a test skeleton (vseq, testlist entry, etc.), you can use it as reference
- If the RTL lives under an auto-generated directory such as `hw/top_earlgrey/ip_autogen/`, first confirm whether you need to run the generation flow before simulating

**When Tier 3 is especially recommended**: when the golden patch also modifies scoreboard / deep vseq but the bug itself has clear external observable behavior, prefer building a minimal vseq that decides based only on CSR / interrupt / alert / err_code. Success case: HMAC's `invalid_config_priority` test — a self-built vseq that only checks `err_code == SwInvalidConfig`, bypassing the golden-modified scoreboard/base_vseq/smoke_vseq entirely, and all four evaluation agents passed it.

**Addendum: using bind/force/monitor in full-IP DV**

For fault injection, alert, counter corruption, and similar scenarios, using `uvm_hdl_read`/`uvm_hdl_force`/`uvm_hdl_release` or a small bind monitor inside a DV env is legitimate and common. When doing so, respect the boundary rules in constraint 14.

The decision must still rely on the real exit code and runtime log of `dvsim.py` / VCS; do not degrade into static file checks.

### Fallback path: build a minimal VCS testbench

**Strict limit: use this path only when ALL of the following hold**:

- The bug is a pure local combinational or sequential logic bug
- It can be reliably triggered and observed via the module's external ports alone
- It does not depend on DV infrastructure such as CSR read/write, alert, interrupt, fault escalation, save/restore, lifecycle, TL integrity, UVM scoreboard, etc.

If you find yourself needing to import internal DV packages/macros from the repo, modify scoreboard semantics, or introduce UVM HDL backdoors, that indicates you should switch back to the full-IP DV path rather than keep patching holes in the standalone tb.

**Core strategy**: build a minimal SV testbench (with a `.core` file if needed), and compile/run via `fusesoc --tool=vcs` or directly via `vcs/vlogan`. The testbench should let `$fatal` / `$finish(0)` vs `$finish(1)` make the exit code directly reflect the test result, rather than relying on grep-matching log strings.

```bash
#!/bin/bash
cd /home/opentitan

TB_DIR=/home/opentitan/.tb_<test_name>
mkdir -p "$TB_DIR"

cat > "$TB_DIR/tb.sv" <<'EOF'
...  // instantiate DUT + minimal stimulus
// Use $finish(0) for pass, $fatal or $finish(1) for fail
EOF

vcs -full64 -sverilog \
  -f "$TB_DIR/files.f" \
  -l "$TB_DIR/compile.log"

set +e
./simv
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

Note: Verilator is not used. Even on the fallback path, the result must still be decided by a real VCS run.

---

## Recommended workflow (follow it)

### Step 0: Read PR info

Read and parse `{PR_DIR}/pr_meta.json` to obtain:

- `base_sha`
- PR description (title/body/issues)
- Path to the input patch (fix.patch)

### Step 1: Understand the bug on the host

On the host (outside the container) you may:

1) Read `{PR_DIR}/fix.patch` to understand the fix point and trigger condition. `fix.patch` may contain both RTL modifications and DV/test modifications; both can inform your understanding of the bug and your test design.
2) Localize the affected module from the patch: OpenTitan is a SoC project, so changes may land in an independent IP (`aes`, `lc_ctrl`, `pwrmgr`, `flash_ctrl`, etc.) or in top-level integration (e.g., `top_earlgrey`, `top_darjeeling`).
3) Use `gh` or web search for more context, for example:
   - `gh pr view {NUMBER} -R lowRISC/opentitan --json body,comments,reviews`
   - `gh issue view <issue_number> -R lowRISC/opentitan`
   - Check whether later commits on top of this PR provide tests, environment fixes, or alternative reproducer ideas
   - Search relevant technical material (IP spec, DV docs, dvsim usage, etc.)
4) Select the most appropriate `tb_script` mode (A/B/C)

Requirement: the script must be fast and deterministic, preferably use fixed seeds, and avoid long random testing.

### Step 2: Start a persistent work container

Start a persistent container from `{BASE_IMAGE}` with `{PR_DIR}` mounted. Recommended command:

```bash
CTR="tbgen-opentitan-{NUMBER}-$$"
docker run -d --rm --init --name "$CTR" \
  -v {PR_DIR}:/workspace/pr \
  {BASE_IMAGE} \
  tail -f /dev/null
```

All subsequent checkout, environment setup, compilation, simulation, and verification must happen inside this container. Delete the container when done.

### Step 3: Set up the environment inside the container (stage 1-4)

Via `docker exec`, execute inside the container the steps corresponding to the default `prepare_dev` semantics:

1) Checkout `base_sha` and update submodules
2) If `apt-requirements.txt` exists, install system packages per the repo's list (watch out for comments and empty lines)
3) `pip install -r python-requirements.txt` to install Python dependencies; when needed, remain compatible with the legacy VCS requirement syntax in old commits
4) Install the RISC-V toolchain (`util/get-toolchain.py`) and Verible, and persist `/tools/riscv/bin` and `/tools/verible/bin` into PATH

You can run these stages step by step for debugging, or execute an equivalent batch command once you have confirmed the default template works.

**OpenTitan note**: after checkout, **immediately read `/home/opentitan/util/container/Dockerfile` inside the container**. This is the "source of truth" for the environment at this commit — its `ARG` declarations specify the exact versions of Verilator, Verible, RISC-V toolchain, and Clang required by this version, and the apt and pip install steps are CI-validated. The default prepare template already extracts version numbers from it to install toolchains and Verible, but when the default prepare is not enough, **prefer consulting this file to fill in missing dependencies rather than guessing versions**.

The default prepare is not always enough. Some commits may additionally require:

- A different or additional toolchain version
- Additional system packages
- Repo-specific environment configuration steps
- Other tools present in `util/container/Dockerfile` but not covered by the default template (e.g., Clang / Bazelisk); add them only when your case actually needs them

When the default prepare is insufficient, the correct way to proceed is:

1) First try the default prepare, and confirm where `dvsim.py` gets stuck
2) Analyze the missing items
3) Read the environment-configuration files at the current `base_sha` version inside the container: `util/container/Dockerfile`, `apt-requirements.txt`, `python-requirements.txt`, `util/get-toolchain.py`, etc.
4) Emit a `prepare_script.sh` that is a **full replacement of stage 1-4**

Output rules for `prepare_script`:

- If default environment preparation works: leave `prepare_script.sh` empty
- If customization is needed: output the **full replacement of stage 1-4**
- **Do not** hand-write stage 5; the framework automatically appends the canonical finalize part at the end of the final `prepare.sh`

### Step 4: Iterate on tb_script inside the same container

Write and debug `tb_script.sh` inside the container. Place all build artifacts and temporary files under `/home/opentitan/` (e.g., `/home/opentitan/.tb_xxx/`):

1) Write or modify `tb_script.sh`
2) Run the build / simulation
3) Check the exit code and logs
4) Modify and rerun

It is recommended to save logs under `/home/opentitan/` (e.g., `/home/opentitan/.tb_xxx/build.log`). The default output directory of `dvsim.py` is also inside the repo, so no extra configuration is usually needed.

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
4) If a single execution exceeds 1800s, stop and emit `failure.stage = "timeout"`

---

## The final result.json you must emit (strict schema)

You must write `{PR_DIR}/result.json` with all fields present:

```json
{
  "status": "success",
  "org": "lowRISC",
  "repo": "opentitan",
  "number": 12345,
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
  "repo": "opentitan",
  "number": 12345,
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
