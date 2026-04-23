# Repository Harnesses

`repos/` contains the per-repository implementations used by tbgen, verify, the Harbor adapter, and the evaluator. Each supported repository defines how to build Docker images, prepare a PR baseline, run the generated test script, apply the ground-truth fix, and parse fail-to-pass results.

The shared helper code is in `common.py`. Repository-specific code is split by HDL family:

| File | Repo | HDL | Build System |
|------|------|-----|--------------|
| `verilog/ibex/ibex.py` | lowRISC/ibex | Verilog | Verilator + FuseSoC |
| `verilog/opentitan/opentitan.py` | lowRISC/opentitan | SystemVerilog | VCS + dvsim/FuseSoC |
| `verilog/cva6/cva6.py` | openhwgroup/cva6 | SystemVerilog | Verilator + Spike |
| `verilog/caliptra/caliptra.py` | chipsalliance/caliptra-rtl | SystemVerilog | Verilator |
| `chisel/xiangshan/xiangshan.py` | OpenXiangShan/XiangShan | Chisel/Scala | Mill + Verilator |
| `chisel/rocketchip/rocketchip.py` | chipsalliance/rocket-chip | Chisel/Scala | SBT + Verilator |

## Interface Contract

Each repo implements three pieces:

`ImageBase` builds the shared base image. It installs host packages, language/toolchain dependencies, helper scripts, and a repository clone. tbgen calls this first so Codex has a base image to work inside.

`ImageDefault` builds the per-PR image. It checks out the PR base SHA, runs the default prepare flow or a user-provided `prepare_script`, appends the shared finalize script, and records the runtime base commit. This image is what verify, adapter-generated tasks, and evaluator runs use.

`Instance` registers the repo with `@Instance.register(org, repo)`. It supplies the dependency image, runtime scripts for baseline and fixed runs, and the log parser used to turn `TEST: ... PASS|FAIL|SKIP` markers into `TestResult`.

The runtime scripts follow the same shape across repos:

1. Reset and clean the repository.
2. Check out the runtime base commit recorded during image preparation.
3. Run `/home/tb_script.sh` for the baseline test.
4. Apply `/home/fix.patch` and run the same script for the fixed test.

The generated `tb_script.sh` is mounted at runtime and is not baked into the image. `prepare_script` is baked into the per-PR image and should only be used when the default prepare flow is insufficient.

## Finalize Script

`common.render_finalize_script()` is appended to every per-PR prepare flow. It removes remotes and extra refs, expires reflogs, prunes unreachable objects, flattens submodule git metadata where possible, commits any prepared workspace residue as the new clean baseline, and writes the runtime base SHA to a file.

Most repos use `/home/base_commit.txt`. Legacy harnesses use repo-specific paths:

| Repo | Runtime base SHA file |
|------|-----------------------|
| ibex | `/home/ibex_base_commit.txt` |
| cva6 | `/home/cva6_base_commit.txt` |
| opentitan | `/home/opentitan_base_commit.txt` |
| caliptra-rtl | `/home/base_commit.txt` |
| XiangShan | `/home/base_commit.txt` |
| rocket-chip | `/home/base_commit.txt` |

The Harbor adapter checks both conventions when generating task directories. If per-PR images are rebuilt, regenerate task directories because `tests/test.sh` embeds this runtime SHA.

## Log Parsing

All current repos use `common.parse_test_markers()`. The parser reads only lines between:

```bash
HWE_BENCH_RESULTS_START
HWE_BENCH_RESULTS_END
```

Within that region it recognizes lines shaped as:

```text
TEST: <name> ... PASS
TEST: <name> ... FAIL
TEST: <name> ... SKIP
```

Generated scripts should make PASS/FAIL reflect real build, elaboration, simulation, or test behavior. They should not decide based on static patch text, `git diff`, or metadata files.

## Repo Notes

OpenTitan depends on a VCS-capable base image and should use real VCS / `dvsim` / UVM-style flows when applicable. Verilator is not the intended simulator for OpenTitan benchmark validation.

XiangShan and rocket-chip are Chisel projects. Their harnesses install Java, Mill or SBT, Verilator, and RISC-V toolchains. Prompts prefer focused Chisel/Mill or SBT targets over full-system simulation when a smaller real reproducer is possible.

CVA6 uses Verilator-based flows and may build Spike dynamically when a full-core software path requires it.

Caliptra uses open-source Verilator flows and forces `MAKEFLAGS=-j1` at runtime to avoid known parallel Makefile races in upstream scripts.

ibex uses Verilator and FuseSoC style flows with a legacy runtime base SHA file.

One known caveat for vendored submodules remains: when a repo stores a dependency as a gitlink but the prepared runtime tree has flattened submodule contents, changes inside that dependency can be invisible to the top-level `git diff`. XiangShan PR #4249 was excluded from the curated benchmark for this reason. Do not assume a correct edit under a vendored submodule path will necessarily be captured by the current patch extraction path.
