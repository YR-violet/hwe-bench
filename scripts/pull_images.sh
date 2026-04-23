#!/usr/bin/env bash
set -euo pipefail

REGISTRY="${HWE_BENCH_IMAGE_REGISTRY:-ghcr.io/pku-liang}"
LOCAL_PREFIX="${HWE_BENCH_LOCAL_IMAGE_PREFIX:-hwebench}"
DATASET_ROOT="${HWE_BENCH_DATASET_ROOT:-datasets/pipeline}"

usage() {
  cat <<'EOF'
Usage:
  scripts/pull_images.sh <repo> [--dataset PATH]

For downloaded datasets, prefer passing --dataset explicitly. The file name and
directory layout do not matter; the script reads org/repo/number from JSONL.

Repos:
  ibex
  cva6
  caliptra
  rocketchip
  xiangshan

OpenTitan images are not distributed because the evaluation flow requires VCS.
Build OpenTitan locally after preparing a vcs:minimal base image.

Environment:
  HWE_BENCH_IMAGE_REGISTRY      Remote registry prefix (default: ghcr.io/pku-liang)
  HWE_BENCH_LOCAL_IMAGE_PREFIX  Local runtime prefix (default: hwebench)
  HWE_BENCH_DATASET_ROOT        Dataset root (default: datasets/pipeline)
EOF
}

if [[ $# -lt 1 ]]; then
  usage
  exit 2
fi

if [[ "$1" == "-h" || "$1" == "--help" ]]; then
  usage
  exit 0
fi

repo_key="$1"
shift

dataset=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dataset)
      if [[ $# -lt 2 ]]; then
        echo "[ERROR] --dataset requires a path" >&2
        exit 2
      fi
      dataset="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "[ERROR] Unknown argument: $1" >&2
      usage
      exit 2
      ;;
  esac
done

case "$repo_key" in
  ibex)
    org="lowRISC"
    repo="ibex"
    default_dataset="$DATASET_ROOT/lowRISC/lowRISC__ibex_s11_eval_ready.jsonl"
    ;;
  cva6)
    org="openhwgroup"
    repo="cva6"
    default_dataset="$DATASET_ROOT/openhwgroup/openhwgroup__cva6_s11_eval_ready.jsonl"
    ;;
  caliptra|caliptra-rtl)
    org="chipsalliance"
    repo="caliptra-rtl"
    default_dataset="$DATASET_ROOT/chipsalliance/chipsalliance__caliptra-rtl_s11_eval_ready.jsonl"
    ;;
  rocketchip|rocket-chip)
    org="chipsalliance"
    repo="rocket-chip"
    default_dataset="$DATASET_ROOT/chipsalliance/chipsalliance__rocket-chip_s11_eval_ready.jsonl"
    ;;
  xiangshan|XiangShan)
    org="OpenXiangShan"
    repo="XiangShan"
    default_dataset="$DATASET_ROOT/OpenXiangShan/OpenXiangShan__XiangShan_s11_eval_ready.jsonl"
    ;;
  opentitan)
    cat >&2 <<'EOF'
[ERROR] OpenTitan images are not distributed with HWE-bench because the
evaluation flow requires Synopsys VCS. Build OpenTitan locally after creating
a vcs:minimal base image. See docs/building-images.md.
EOF
    exit 1
    ;;
  *)
    echo "[ERROR] Unknown repo: $repo_key" >&2
    usage
    exit 2
    ;;
esac

if [[ -z "$dataset" ]]; then
  dataset="$default_dataset"
fi

if [[ ! -f "$dataset" ]]; then
  echo "[ERROR] Dataset not found: $dataset" >&2
  echo "        Pass --dataset PATH if the dataset is stored elsewhere." >&2
  exit 1
fi

dataset_meta="$(
  python - "$dataset" "$org" "$repo" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
expected_org = sys.argv[2]
expected_repo = sys.argv[3]
org = None
repo = None
count = 0

with path.open("r", encoding="utf-8") as f:
    for line in f:
        if not line.strip():
            continue
        row = json.loads(line)
        row_org = str(row.get("org", ""))
        row_repo = str(row.get("repo", ""))
        if not row_org or not row_repo:
            raise SystemExit(f"missing org/repo in dataset row {count + 1}")
        if org is None:
            org = row_org
            repo = row_repo
        elif row_org != org or row_repo != repo:
            raise SystemExit(
                f"mixed org/repo in dataset: first={org}/{repo}, row={row_org}/{row_repo}"
            )
        count += 1

if count == 0:
    raise SystemExit("dataset has no records")

if org != expected_org or repo != expected_repo:
    raise SystemExit(
        f"dataset is for {org}/{repo}, but repo argument expects {expected_org}/{expected_repo}"
    )

print(f"{org}\t{repo}\t{count}")
PY
)" || {
  echo "[ERROR] Dataset validation failed: $dataset" >&2
  exit 1
}

IFS=$'\t' read -r dataset_org dataset_repo dataset_count <<< "$dataset_meta"

image_name="$(printf '%s_m_%s' "$org" "$repo" | tr '[:upper:]' '[:lower:]')"
remote_image="${REGISTRY}/${image_name}"
local_image="${LOCAL_PREFIX}/${image_name}"

pull_and_tag() {
  local tag="$1"
  local remote="${remote_image}:${tag}"
  local local="${local_image}:${tag}"

  echo "[PULL] $remote"
  docker pull "$remote"
  echo "[TAG]  $remote -> $local"
  docker tag "$remote" "$local"
}

echo "[INFO] repo=${org}/${repo}"
echo "[INFO] dataset=${dataset}"
echo "[INFO] dataset_records=${dataset_count}"
echo "[INFO] remote=${remote_image}"
echo "[INFO] local=${local_image}"

pull_and_tag "base"

python - "$dataset" <<'PY' | while read -r number; do
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
numbers = []
with path.open("r", encoding="utf-8") as f:
    for line in f:
        if not line.strip():
            continue
        row = json.loads(line)
        numbers.append(int(row["number"]))

for number in sorted(set(numbers)):
    print(number)
PY
  pull_and_tag "pr-${number}"
done

echo "[DONE] Pulled and retagged images for ${org}/${repo}"
