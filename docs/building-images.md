# Building Docker Images

HWE-bench can use either published Docker images or images built locally from the harness code. Published images are the recommended path for score reproduction. Local builds are useful when images are unavailable, when modifying a repository harness, or when inspecting how the benchmark environment is constructed.

The build flow uses the same benchmark JSONL that is later passed to the Harbor adapter. The JSONL location and file name do not matter as long as the records contain the expected `org`, `repo`, `number`, `base`, `fix_patch`, `tb_script`, and `prepare_script` fields.

## Prerequisites

Before building images, install the project dependencies and make sure Docker is available:

```bash
uv sync
uv tool install --editable ./deps/harbor --force
docker info
```

The non-OpenTitan repositories build from public dependencies. OpenTitan is different because it requires Synopsys VCS; see [OpenTitan](#opentitan) below.

## Build Images

Use `build-images` to build the shared `:base` image and all per-PR `:pr-N` images for one dataset:

```bash
uv run python -m hwe_bench.harness.docker_runner build-images \
  --input /path/to/<dataset>.jsonl
```

Build a small subset while testing the local environment:

```bash
uv run python -m hwe_bench.harness.docker_runner build-images \
  --input /path/to/<dataset>.jsonl \
  --only 1383,2232
```

Images are tagged with the local runtime names expected by HWE-bench:

```text
hwebench/{org_lower}_m_{repo_lower}:base
hwebench/{org_lower}_m_{repo_lower}:pr-{number}
```

For example:

```text
hwebench/lowrisc_m_ibex:base
hwebench/lowrisc_m_ibex:pr-1383
```

## What Gets Built

Each supported repository has a harness under `hwe_bench/harness/repos/`. The harness defines:

- a shared base image with OS packages, toolchains, helper scripts, and a repository clone
- a per-PR image that checks out the PR baseline, runs the default or custom prepare flow, truncates future git history, and records the runtime baseline SHA
- runtime scripts for base FAIL and fix PASS validation

The runtime baseline SHA is written to `/home/base_commit.txt` for newer harnesses, or to a legacy path such as `/home/ibex_base_commit.txt` for older ones. The Harbor adapter reads this SHA when generating task directories.

## Verify Rebuilt Images

After building images, run golden fail-to-pass validation before using them for agent evaluation:

```bash
uv run python -m hwe_bench.harness.docker_runner \
  --mode instance \
  --workdir "$(pwd)/results/golden-<repo>/eval_workdir" \
  --raw_dataset_files /path/to/<dataset>.jsonl \
  --stop_on_error false \
  --max_workers_build_image 4 \
  --max_workers_run_instance 4
```

This validation applies the ground-truth `fix_patch` and runs the dataset `tb_script` to confirm base FAIL and fix PASS. A Docker build that completes successfully but fails golden validation should not be used for scoring.

## Regenerate Task Directories

After a successful image build and golden validation, regenerate Harbor task directories:

```bash
uv run python -m hwe_bench.harness.harbor.adapter \
  --input /path/to/<dataset>.jsonl \
  --output tasks/hwe-bench-<repo>/
```

This step is required after every rebuild. The adapter embeds the runtime baseline SHA from each image into `tests/test.sh`. Reusing task directories generated against a different image build can silently produce empty patches or `git diff` failures.

## OpenTitan

OpenTitan images are not distributed with HWE-bench because the evaluation flow requires Synopsys VCS. HWE-bench does not publish VCS binaries, license files, license-server configuration, or a VCS-enabled base image.

To build OpenTitan locally, first create a local Docker image named `vcs:minimal`. The `minimal` name means HWE-bench expects only the VCS layer to be present there; public OpenTitan dependencies are installed by `hwe_bench/harness/repos/verilog/opentitan/opentitan.py` during the HWE-bench build.

The `vcs:minimal` image should satisfy this contract:

- it is compatible with Ubuntu/Debian-style package installation used by the OpenTitan harness
- `vcs` and `vlogan` are available in `PATH`
- the VCS license environment is configured so commands can obtain a license inside the container
- it does not need OpenTitan, RISC-V toolchains, Verible, micromamba, or Python project dependencies preinstalled

Quick sanity check:

```bash
docker run --rm vcs:minimal bash -lc 'command -v vcs && command -v vlogan'
```

Once `vcs:minimal` exists, build OpenTitan images normally:

```bash
uv run python -m hwe_bench.harness.docker_runner build-images \
  --input /path/to/lowRISC__opentitan.jsonl
```

Then run golden fail-to-pass validation and regenerate task directories before launching any OpenTitan agent run.

## Reproducibility Boundary

Source builds are not guaranteed to be bit-for-bit identical over time, and they are not guaranteed to keep working indefinitely. Several dependencies are resolved at build time:

- base image tags such as `ubuntu:22.04` and the local OpenTitan `vcs:minimal` image
- apt package versions from Ubuntu mirrors
- micromamba downloaded from the current `latest` endpoint
- pip / PyPI dependencies, including dependencies from historical repository requirements files
- GitHub release assets for Verilator, SBT, JDK, Mill, and RISC-V toolchains
- Maven / Coursier / Ivy dependencies used by Chisel repositories
- upstream git repositories and submodule URLs

For score reproduction, prefer the published images when available. For source rebuilds, record the build date, base image digests, and build logs. Always rerun golden validation and regenerate task directories before launching Harbor tasks.

## Related Documents

Use [images.md](images.md) to pull published non-OpenTitan images from `ghcr.io/pku-liang` and retag them for local HWE-bench use.
