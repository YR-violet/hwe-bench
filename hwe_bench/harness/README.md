# HWE-bench Harness

This directory starts where `hwe_bench/collect/` ends. The collect pipeline produces scored pull-request records; the harness turns those records into executable fail-to-pass benchmark cases, packages them as Harbor tasks, and scores agent patches offline.

```
s08 scored JSONL -> tbgen (s09) -> verify (s10) -> psgen (s11) -> adapter -> agent eval -> evaluator
```

The data-construction stages that call Codex directly use `codex exec -p normal`. Before running collect s07/s08, tbgen, verify, psgen, or audit, define a `normal` profile in `$CODEX_HOME/config.toml` or `~/.codex/config.toml` with the model and account settings intended for dataset construction. These subprocesses close stdin explicitly, so they do not inherit non-terminating input from the parent process.

## Stage Overview

`tbgen/prepare_input.py` is the bridge from collect into the harness. It joins the s08 scores with the s05 raw dataset and writes the s09 candidate JSONL used by tbgen.

`tbgen/` implements s09. It asks Codex to generate a `tb_script.sh` and optional `prepare_script.sh` for each PR, then requires the generated scripts to demonstrate base FAIL and fix PASS. Per-case artifacts live under `artifacts/s09_tbgen/{org}__{repo}/pr-{N}/`. The successful script fields are merged back into the dataset for the next stage.

`verify/` implements s10. It starts from the s09 output, asks Codex to repair weak or broken scripts, and validates each case through the standard Docker harness. Per-case artifacts live under `artifacts/s10_verify/{org}__{repo}/pr-{N}/`. A successful verify result means the case passed the full fail-to-pass flow.

`psgen/` implements s11. It generates and reviews the `problem_statement`, which is the only task description the evaluation agent sees. The final output is the s11 eval-ready JSONL.

`harbor/adapter.py` converts s11 records into Harbor task directories. The adapter reads the runtime base SHA from Docker images and writes task files such as `task.toml`, `instruction.md`, held-out `tests/test.sh`, and `solution/solve.sh`.

`harbor/verify_bridge.py` extracts agent diffs from Harbor job directories into `patches.jsonl`. `evaluator.py` then applies those patches to clean containers and produces the final resolved/unresolved report.

`audit/` contains the trajectory and testbench audit pipeline. It is separate from dataset construction but consumes the same artifacts, patches, jobs, and evaluator reports.

`repos/` contains the repository-specific harness code. These classes define how each supported project builds its base image, prepares a per-PR image, runs the generated `tb_script.sh` on the baseline and fixed tree, records the runtime base SHA, and parses `TEST:` markers from logs. See `repos/README.md` for the interface contract and repo-specific caveats.

## Artifact Layout

The main generated paths are:

| Path | Purpose |
|------|---------|
| `datasets/pipeline/{org}/` | JSONL datasets from collect through s11 |
| `artifacts/s09_tbgen/{org}__{repo}/pr-{N}/` | tbgen prompt, logs, scripts, and `result.json` |
| `artifacts/s10_verify/{org}__{repo}/pr-{N}/` | verify prompt, scripts, `workdir/`, logs, and `result.json` |
| `tasks/hwe-bench-{repo}/` | Harbor task directories generated from s11 |
| `jobs/{run-name}/` | Raw Harbor agent runs |
| `results/{run-name}/patches/` | Extracted agent patches |
| `results/{run-name}/eval_workdir/` | Persistent per-case evaluator reports |
| `results/{run-name}/eval/` | Aggregate evaluator output |

`eval_workdir` must be persistent. Per-case `report.json` files live there and are reused by resume, scoring checks, and audit.

## Docker Images

Harness images are named:

```text
hwebench/{org}_m_{repo}:{tag}
```

The `:base` image contains the shared OS, toolchain, and repository clone. Each `:pr-{N}` image checks out the PR baseline, runs the prepare flow, truncates future git history, and records the runtime baseline SHA in `/home/base_commit.txt` or the legacy `/home/{repo}_base_commit.txt`.

Regenerate Harbor task directories after rebuilding images, because task `test.sh` files embed the image's runtime base SHA.

## Source Files

### Core Framework

| File | Purpose |
|------|---------|
| `base.py` | Core dataclasses, image abstraction, and instance registry |
| `docker_runner.py` | Docker image build and fail-to-pass validation engine |
| `reporting.py` | Per-case and aggregate report models |
| `evaluator.py` | Offline scoring for agent patches |
| `codex_batch.py` | Shared Codex batch executor and prompt helpers |

### Stage Modules

| Path | Purpose |
|------|---------|
| `tbgen/` | s09 candidate preparation, tb script generation, and merge utilities |
| `verify/` | s10 script repair and single-case fail-to-pass verification |
| `psgen/` | s11 problem-statement generation and review |
| `harbor/` | s11-to-Harbor adapter and patch extraction |
| `audit/` | coarse audit, flagged-case bundling, detailed investigation, fix, and review |

### Repository Harnesses

Each supported repository provides `ImageBase`, `ImageDefault`, and `Instance` implementations.

| File | Repo | HDL | Build System |
|------|------|-----|--------------|
| `repos/verilog/ibex/ibex.py` | lowRISC/ibex | Verilog | Verilator + FuseSoC |
| `repos/verilog/opentitan/opentitan.py` | lowRISC/opentitan | SystemVerilog | VCS + dvsim/FuseSoC |
| `repos/verilog/cva6/cva6.py` | openhwgroup/cva6 | SystemVerilog | Verilator + Spike |
| `repos/verilog/caliptra/caliptra.py` | chipsalliance/caliptra-rtl | SystemVerilog | Verilator |
| `repos/chisel/xiangshan/xiangshan.py` | OpenXiangShan/XiangShan | Chisel/Scala | Mill + Verilator |
| `repos/chisel/rocketchip/rocketchip.py` | chipsalliance/rocket-chip | Chisel/Scala | SBT + Verilator |
| `repos/common.py` | shared | - | Marker parsing and finalize helpers |
