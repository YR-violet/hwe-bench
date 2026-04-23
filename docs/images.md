# Prebuilt Docker Images

This document describes how to download published HWE-bench Docker images. Images are hosted under `ghcr.io/pku-liang`.

## Usage

The intended interface is a repository-level pull script:

```bash
./scripts/pull_images.sh ibex --dataset /path/to/lowRISC__ibex_s11_eval_ready.jsonl
./scripts/pull_images.sh cva6 --dataset /path/to/openhwgroup__cva6_s11_eval_ready.jsonl
./scripts/pull_images.sh caliptra --dataset /path/to/chipsalliance__caliptra-rtl_s11_eval_ready.jsonl
./scripts/pull_images.sh rocketchip --dataset /path/to/chipsalliance__rocket-chip_s11_eval_ready.jsonl
./scripts/pull_images.sh xiangshan --dataset /path/to/OpenXiangShan__XiangShan_s11_eval_ready.jsonl
```

The script pulls the shared base image and all per-PR images needed by that repository's dataset. It then retags each remote image into the local name expected by the harness:

```text
ghcr.io/pku-liang/<org>_m_<repo>:<tag> -> hwebench/<org>_m_<repo>:<tag>
```

For example:

```bash
docker pull ghcr.io/pku-liang/lowrisc_m_ibex:pr-1383
docker tag ghcr.io/pku-liang/lowrisc_m_ibex:pr-1383 hwebench/lowrisc_m_ibex:pr-1383
```

The script derives the per-PR tags from the local dataset JSONL, so there is no separate image tag manifest in the repository. The dataset file can live anywhere and can have any file name; the script validates `org` and `repo` from the JSONL contents.

If you keep the repository's default `datasets/pipeline/` layout, the `--dataset` argument can be omitted for convenience.

OpenTitan images are not distributed because the evaluation flow requires Synopsys VCS. Build OpenTitan images locally from a user-provided `vcs:minimal` base image; see [building-images.md](building-images.md).

The pull script supports a few environment overrides:

| Variable | Meaning |
|----------|---------|
| `HWE_BENCH_IMAGE_REGISTRY` | Remote registry prefix, default `ghcr.io/pku-liang` |
| `HWE_BENCH_LOCAL_IMAGE_PREFIX` | Local runtime prefix, default `hwebench` |
| `HWE_BENCH_DATASET_ROOT` | Default dataset root used when `--dataset` is omitted |

## After Pulling Images

Generate Harbor task directories after images are available:

```bash
uv run python -m hwe_bench.harness.harbor.adapter \
  --input datasets/pipeline/<ORG>/<ORG>__<REPO>_s11_eval_ready.jsonl \
  --output tasks/hwe-bench-<repo>/
```

The adapter reads each image's runtime baseline SHA and embeds it into the task `test.sh`. Do not reuse task directories generated against a different image build.

## Source Builds

If the published images are unavailable or you need to customize a harness, see [building-images.md](building-images.md). Source builds are useful, but they may differ over time because apt, PyPI, Conda/Mamba, GitHub release assets, Maven/Coursier dependencies, and base image tags can change. After a successful rebuild, run the golden fail-to-pass validation locally before using the rebuilt images for agent evaluation.

For score reproduction, the published images should be treated as the reference environment.
