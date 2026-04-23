You are performing **s10 verify**: fix and validate an existing `openhwgroup/cva6` case so that it passes the standard harness's f2p verification.

This is not script generation from scratch. The current directory already contains `tb_script.sh` / `prepare_script.sh` / the patch / case metadata; your job is to understand how the standard harness runs, make the minimum necessary repairs, and complete a real verification.

You may attempt at most **3 rounds**. If it still fails after three rounds, you must emit a failure result — infinite iteration is forbidden.

---

## Your goal

You need to make the current case satisfy the following under the standardized harness:

- `test-run.sh` stage: on base, running `tb_script.sh` fails, and the log contains at least one `TEST: ... FAIL`
- `fix-run.sh` stage: on base + fix patch, `tb_script.sh` succeeds, and the log contains at least one `TEST: ... PASS`

Finally, you must write the fixed scripts and the verification verdict to `result.json`.

---

## Files in the current directory

- `case.json`: full case record (from s09 output, contains `tb_script` / `prepare_script` / patch / PR info)
- `pr_meta.json`: summary and paths for this verify task
- `tb_script.sh`: the runtime script currently awaiting verification
- `prepare_script.sh`: the prepare script currently awaiting verification
- `fix.patch`

You should edit `tb_script.sh` / `prepare_script.sh` in the current directory directly. Do not modify `case.json`.

---

## Underlying harness facts

You must rely on the following facts to understand why certain constructs are illegal:

1. `prepare_script.sh` is baked into the image at **build image** time; the framework also automatically appends the canonical finalize stage to its end.
2. `tb_script.sh` is NOT baked into the image; it is read-only bind-mounted to `/home/tb_script.sh` at runtime.
3. The semantics of `test-run.sh`:
   - `cd /home/cva6`
   - `git reset --hard && git clean -fdx`
   - checkout the baseline recorded in `/home/cva6_base_commit.txt`
   - `bash /home/tb_script.sh`
4. The semantics of `fix-run.sh`:
   - `cd /home/cva6`
   - `git reset --hard && git clean -fdx`
   - checkout the baseline
   - apply `fix.patch`
   - `bash /home/tb_script.sh`
5. Runtime files and the patch are read-only bind-mounted to `/home/*.sh` / `/home/*.patch`; **you may only write build artifacts, logs, and temporary files under `/home/cva6`**.
6. A fresh container is launched for every verification; **do not assume previous build artifacts are still present**.
7. The harness's log parser only recognizes markers of the form `TEST: <name> ... PASS|FAIL|SKIP`. All `TEST:` markers must be enclosed between the boundary markers `echo "HWE_BENCH_RESULTS_START"` and `echo "HWE_BENCH_RESULTS_END"`; the parser only parses content within those boundaries.

If the summary above is not enough to understand a particular failure, you can read the harness source code directly:

- cva6 image build and runtime script definitions: `{REPO_ROOT}/hwe_bench/harness/repos/verilog/cva6/cva6.py`
- docker_runner's build_image / run_instance logic: `{REPO_ROOT}/hwe_bench/harness/docker_runner.py`
- docker build / run wrapper: `{REPO_ROOT}/hwe_bench/utils/docker_util.py`
- log parser (TEST marker parsing): `{REPO_ROOT}/hwe_bench/harness/repos/verilog/common.py`

---

## Static check rules

Do the static checks first, then proceed to real verification. The point of static checks is not "formalism" but to prevent conflicts with the underlying harness semantics.

The following must hold:

- `tb_script.sh` must `cd /home/cva6`
- `tb_script.sh` must contain at least one literal `TEST:`, and must contain the boundary markers `HWE_BENCH_RESULTS_START` and `HWE_BENCH_RESULTS_END`
- `tb_script.sh` / `prepare_script.sh` must not reference `/workspace/pr`
- `tb_script.sh` must not read `fix.patch` / `pr_meta.json` contents to decide PASS/FAIL
- `tb_script.sh` must not execute `git checkout` / `git reset` / `git clean` / `git apply`
- Only writes under `/home/cva6` are allowed for working files; do not write to `/home/tb_script.sh` or `/home/fix.patch`
- If `tb_script.sh` uses `grep` on logs to decide PASS/FAIL, check whether this can be replaced by the simulation program's exit code directly (e.g., `return 0/1` from a C++ harness, or `$fatal` / `$finish` in SystemVerilog). Exit-code-based decisions are more reliable and preferred.
- **Implementation-independence**: `tb_script.sh` must verify **observable behavior**, not a **specific implementation**. If a tb_script can only pass under the specific implementation in the ground-truth `fix.patch`, and other functionally equivalent correct fixes would cause compilation or test failures, then the tb_script is unacceptable. Specifically:
  - A standalone testbench wrapper created by tb_script must be as self-contained as possible; it must not depend on a set of DV helper files whose interface changes frequently across different fix implementations
  - If tb_script needs to instantiate `cva6`, `cva6_tb_wrapper`, or a submodule, it should fix the external interface through a wrapper so that any implementation with the correct external semantics can compile
  - If tb_script is found to require changes to certain repo DV files or wrapper files (alongside the ground-truth fix) in order to compile, this indicates implementation coupling, and the tb_script must be rewritten
  - Such issues should be flagged as `"stage": "implementation_coupled"` in the failure

Repair priority:

1. **Prefer modifying only `tb_script.sh`**
2. Only modify `prepare_script.sh` when the default environment is clearly insufficient
3. If you modify `prepare_script.sh`, the next verification round must force a rebuild of the image

---

## How to run the standard verification

Use the following helper script:

```bash
{PYTHON_BIN} {RUN_CASE} --case-dir {PR_DIR}
```

If you modified `prepare_script.sh`, you must instead use:

```bash
{PYTHON_BIN} {RUN_CASE} --case-dir {PR_DIR} --force-build
```

The helper will write the standard verification artifacts to:

- `{PR_DIR}/workdir/openhwgroup/cva6/images/pr-<n>/build_image.log`
- `{PR_DIR}/workdir/openhwgroup/cva6/instances/pr-<n>/run.log`
- `{PR_DIR}/workdir/openhwgroup/cva6/instances/pr-<n>/test-patch-run.log`
- `{PR_DIR}/workdir/openhwgroup/cva6/instances/pr-<n>/fix-patch-run.log`
- `{PR_DIR}/workdir/openhwgroup/cva6/instances/pr-<n>/report.json`

Exit codes of `run_case.py`:

- `0`: verification passed
- non-`0`: verification failed; inspect the logs and `report.json`

---

## Your workflow

Follow this order:

1. Read the current `tb_script.sh` / `prepare_script.sh`
2. Do the static checks and fix obvious problems first
3. Run one round of standard verification
4. Inspect `build_image.log` / `test-patch-run.log` / `fix-patch-run.log` / `report.json`
5. Prefer modifying only `tb_script.sh`, then rerun
6. Only modify `prepare_script.sh` when absolutely necessary, and add `--force-build` on the next round
7. At most 3 rounds

Do not:

- Invent your own docker build / run flow
- Explore freely in an ad-hoc container and bypass the standard harness
- Read patch text or git diff in order to "pass" — this is cheating

---

## The final result.json you must emit

On success:

```json
{
  "status": "success",
  "org": "openhwgroup",
  "repo": "cva6",
  "number": 2015,
  "base_sha": "xxxxxxxx",
  "tb_script": "#!/bin/bash\n...",
  "prepare_script": "",
  "failure": null,
  "artifacts": {
    "report_path": ".../report.json",
    "instance_dir": ".../instances/pr-2015"
  }
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
  "tb_script": "#!/bin/bash\n...",
  "prepare_script": "",
  "failure": {
    "stage": "static_check|build_image|test_run|fix_run|report_parse|no_repair|implementation_coupled",
    "reason": "one-sentence explanation of the failure",
    "last_error_excerpt": "key excerpt from the last failing log",
    "attempts": [
      "what round 1 did and why it failed",
      "what round 2 did and why it failed"
    ]
  },
  "artifacts": {
    "report_path": ".../report.json",
    "instance_dir": ".../instances/pr-2015"
  }
}
```

Notes:

- `tb_script` / `prepare_script` must contain the **full final content**
- Whether you succeed or fail, write whatever scripts you ended up with back into `result.json`
