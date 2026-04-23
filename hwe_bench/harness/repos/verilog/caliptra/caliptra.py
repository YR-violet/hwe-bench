from __future__ import annotations

from typing import Optional, Union

from hwe_bench.harness.base import Config, File, Image, Instance, PullRequest, TestResult
from hwe_bench.harness.repos.common import parse_test_markers, render_finalize_script


class CaliptraImageBase(Image):
    """Base environment image for chipsalliance/caliptra-rtl."""

    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Union[str, "Image"]:
        base_img = "ubuntu:22.04"
        if self.config.global_env and self.config.global_env.get("CALIPTRA_BASE_IMG"):
            base_img = str(self.config.global_env["CALIPTRA_BASE_IMG"]).strip()
            if not base_img or any(ch.isspace() for ch in base_img):
                raise ValueError(f"Invalid CALIPTRA_BASE_IMG: {base_img!r}")
        return base_img

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        install_verilator = """#!/bin/bash
set -euo pipefail

version="${1:?usage: install-verilator-version VERSION [LINK_PATH]}"
link_path="${2:-/opt/verilator}"
jobs="${NUM_JOBS:-4}"

version_tag="$version"
if [[ "$version_tag" != v* ]]; then
  version_tag="v${version_tag}"
fi

version_clean="${version_tag#v}"
install_dir="${link_path}-${version_clean}"

if [[ -x "${install_dir}/bin/verilator" ]]; then
  ln -sfn "${install_dir}" "${link_path}"
  exit 0
fi

tmp_dir="$(mktemp -d /tmp/verilator-${version_clean}.XXXXXX)"
archive="${tmp_dir}/verilator.tar.gz"
src_dir="${tmp_dir}/src"

curl -fLs "https://github.com/verilator/verilator/archive/refs/tags/${version_tag}.tar.gz" -o "${archive}"
mkdir -p "${src_dir}"
tar -C "${src_dir}" --strip-components=1 -xzf "${archive}"

cd "${src_dir}"
autoconf
./configure --prefix="${install_dir}" CXX="ccache g++"
make -j"${jobs}"
make install

if [[ -d "${install_dir}/share/verilator/bin" ]]; then
  mkdir -p "${install_dir}/bin"
  for helper in "${install_dir}"/share/verilator/bin/*; do
    [[ -e "${helper}" ]] || continue
    helper_name="$(basename "${helper}")"
    if [[ ! -e "${install_dir}/bin/${helper_name}" ]]; then
      ln -sfn "${helper}" "${install_dir}/bin/${helper_name}"
    fi
  done
fi

verilated_cpp="${install_dir}/share/verilator/include/verilated.cpp"
if [[ -f "${verilated_cpp}" ]] && ! grep -q '^#include <limits>$' "${verilated_cpp}"; then
  sed -i '/^#include <sstream>$/a #include <limits>' "${verilated_cpp}" || true
fi

ln -sfn "${install_dir}" "${link_path}"
rm -rf "${tmp_dir}"
"""

        install_riscv = """#!/bin/bash
set -euo pipefail

version="${1:-v12.1.0}"
link_path="${2:-/opt/riscv}"

version_tag="$version"
if [[ "$version_tag" != v* ]]; then
  version_tag="v${version_tag}"
fi

version_clean="${version_tag#v}"
install_dir="${link_path}-${version_clean}"
url="${CALIPTRA_RISCV_TOOLCHAIN_URL:-https://github.com/chipsalliance/caliptra-tools/releases/download/gcc-${version_tag}/riscv64-unknown-elf.gcc-${version_clean}.tar.gz}"

if [[ -x "${install_dir}/bin/riscv64-unknown-elf-gcc" ]]; then
  ln -sfn "${install_dir}" "${link_path}"
  exit 0
fi

tmp_dir="$(mktemp -d /tmp/caliptra-riscv-${version_clean}.XXXXXX)"
archive="${tmp_dir}/toolchain.tar.gz"
extract_dir="${tmp_dir}/extract"

curl -fLs "${url}" -o "${archive}"
mkdir -p "${extract_dir}"
tar -C "${extract_dir}" -xzf "${archive}"

src_dir="$(find "${extract_dir}" -mindepth 1 -maxdepth 1 -type d | head -n1)"
if [[ -z "${src_dir}" ]]; then
  src_dir="${extract_dir}"
fi

rm -rf "${install_dir}"
mkdir -p "${install_dir}"
cp -a "${src_dir}"/. "${install_dir}"/
ln -sfn "${install_dir}" "${link_path}"
rm -rf "${tmp_dir}"
"""

        return [
            File(".", "install-verilator-version", install_verilator),
            File(".", "install-caliptra-riscv-toolchain", install_riscv),
        ]

    @property
    def need_copy_code(self) -> bool:
        return False

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        verilator_version = "v5.044"
        riscv_version = "v12.1.0"
        if self.config.global_env and self.config.global_env.get("CALIPTRA_VERILATOR_VERSION"):
            verilator_version = str(self.config.global_env["CALIPTRA_VERILATOR_VERSION"]).strip()
        if self.config.global_env and self.config.global_env.get("CALIPTRA_RISCV_VERSION"):
            riscv_version = str(self.config.global_env["CALIPTRA_RISCV_VERSION"]).strip()

        if not verilator_version or any(ch.isspace() for ch in verilator_version):
            raise ValueError(f"Invalid CALIPTRA_VERILATOR_VERSION: {verilator_version!r}")
        if not riscv_version or any(ch.isspace() for ch in riscv_version):
            raise ValueError(f"Invalid CALIPTRA_RISCV_VERSION: {riscv_version!r}")

        return """FROM __IMAGE_NAME__

__GLOBAL_ENV__

USER root

ENV DEBIAN_FRONTEND=noninteractive
ENV MAMBA_ROOT_PREFIX=/opt/micromamba
ENV NUM_JOBS=4
ENV MAKEFLAGS=-j4

COPY install-verilator-version /usr/local/bin/install-verilator-version
COPY install-caliptra-riscv-toolchain /usr/local/bin/install-caliptra-riscv-toolchain

RUN apt-get update && \\
    apt-get install -y --no-install-recommends \\
      autoconf \\
      automake \\
      bison \\
      build-essential \\
      ca-certificates \\
      ccache \\
      curl \\
      file \\
      flex \\
      g++ \\
      git \\
      help2man \\
      jq \\
      less \\
      libfl-dev \\
      libfl2 \\
      make \\
      numactl \\
      patch \\
      perl \\
      perl-doc \\
      pkg-config \\
      procps \\
      python3 \\
      tmux \\
      wget \\
      xz-utils \\
      zlib1g \\
      zlib1g-dev && \\
    rm -rf /var/lib/apt/lists/*

RUN chmod +x /usr/local/bin/install-verilator-version \\
             /usr/local/bin/install-caliptra-riscv-toolchain && \\
    printf '#!/bin/bash\\necho 4\\n' > /usr/local/bin/nproc && \\
    chmod +x /usr/local/bin/nproc

RUN curl -Ls https://micro.mamba.pm/api/micromamba/linux-64/latest | \\
      tar -xvj -C /usr/local/bin --strip-components=1 bin/micromamba && \\
    /usr/local/bin/micromamba create -y -n caliptra -c conda-forge python=3.10 pip pyyaml && \\
    /usr/local/bin/micromamba clean --all --yes

RUN /usr/local/bin/install-verilator-version __VERILATOR_VERSION__
RUN /usr/local/bin/install-caliptra-riscv-toolchain __RISCV_VERSION__

RUN printf '%s\\n' \\
  'export MAMBA_ROOT_PREFIX=/opt/micromamba' \\
  'export CALIPTRA_ROOT=/home/caliptra-rtl' \\
  'export CALIPTRA_HOME=/home/caliptra-rtl' \\
  'export CALIPTRA_WORKSPACE=/home' \\
  'export ADAMSBRIDGE_ROOT=/home/caliptra-rtl/submodules/adams-bridge' \\
  'export CALIPTRA_AXI4PC_DIR=/home/caliptra-rtl/src/integration/tb' \\
  'export CALIPTRA_PRIM_ROOT=/home/caliptra-rtl/src/caliptra_prim_generic' \\
  'export CALIPTRA_PRIM_MODULE_PREFIX=caliptra_prim_generic' \\
  'export PKG_CONFIG_PATH=/opt/verilator/share/pkgconfig${PKG_CONFIG_PATH:+:$PKG_CONFIG_PATH}' \\
  'export PATH=/opt/verilator/bin:/opt/riscv/bin:$PATH' \\
  'eval "$(/usr/local/bin/micromamba shell hook --shell=bash)"' \\
  'if [[ "${CONDA_DEFAULT_ENV:-}" != "caliptra" ]]; then' \\
  '  micromamba activate caliptra' \\
  'fi' \\
  > /etc/caliptra_bash_env
ENV BASH_ENV=/etc/caliptra_bash_env
RUN echo 'source /etc/caliptra_bash_env' >> /root/.bashrc

RUN git clone https://github.com/chipsalliance/caliptra-rtl.git /home/caliptra-rtl
WORKDIR /home/caliptra-rtl
RUN git submodule update --init --recursive

RUN ln -sf /bin/bash /bin/sh

__CLEAR_ENV__

CMD ["/bin/bash"]
""".replace("__IMAGE_NAME__", image_name).replace(
            "__GLOBAL_ENV__", self.global_env
        ).replace(
            "__VERILATOR_VERSION__", verilator_version
        ).replace(
            "__RISCV_VERSION__", riscv_version
        ).replace(
            "__CLEAR_ENV__", self.clear_env
        )


class CaliptraImageDefault(Image):
    """Per-PR Caliptra image with the prepared baseline checked out."""

    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def _load_prepare_script(self) -> str:
        if hasattr(self.pr, "prepare_script") and isinstance(self.pr.prepare_script, str):
            return self.pr.prepare_script
        return ""

    def _load_tb_script(self) -> str:
        if hasattr(self.pr, "tb_script") and isinstance(self.pr.tb_script, str):
            return self.pr.tb_script
        return ""

    def dependency(self) -> Image | None:
        return CaliptraImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _prepare_dev_script(self) -> str:
        base_sha = self.pr.base.sha
        return """#!/bin/bash
set -e

# Stage 1: checkout base_sha in a clean workspace
cd /home/caliptra-rtl
git reset --hard
git clean -fdx
git checkout __BASE_SHA__

# Stage 2: sync submodules to the checked-out commit
git submodule sync --recursive || true
git submodule update --init --recursive

# Stage 3: detect tool versions from the checked-out repo and align the environment
verilator_version="v5.044"
riscv_version="v12.1.0"
workflow_file=".github/workflows/build-test-verilator.yml"

if [[ -f "$workflow_file" ]]; then
  detected_verilator="$(awk -F': ' '/^[[:space:]]+VERILATOR_VERSION:/ {gsub(/"/, "", $2); print $2; exit}' "$workflow_file")"
  detected_riscv="$(awk -F': ' '/^[[:space:]]+RISCV_VERSION:/ {gsub(/"/, "", $2); print $2; exit}' "$workflow_file")"
  if [[ -n "$detected_verilator" ]]; then
    verilator_version="$detected_verilator"
  fi
  if [[ -n "$detected_riscv" ]]; then
    riscv_version="$detected_riscv"
  fi
fi

echo "[INFO] Requested Verilator version: ${verilator_version}"
echo "[INFO] Requested RISC-V toolchain version: ${riscv_version}"

if ! command -v verilator >/dev/null 2>&1 || ! verilator --version | head -n1 | grep -q "${verilator_version#v}"; then
  /usr/local/bin/install-verilator-version "${verilator_version}"
fi

if ! command -v riscv64-unknown-elf-gcc >/dev/null 2>&1 || ! riscv64-unknown-elf-gcc --version | head -n1 | grep -q "${riscv_version#v}"; then
  /usr/local/bin/install-caliptra-riscv-toolchain "${riscv_version}"
fi

hash -r

# Stage 4: install minimal Python deps used by the open-source smoke/regression scripts
python -m pip install -U pip setuptools wheel pyyaml
python -m pip show pyyaml >/dev/null

if [[ ! -f tools/scripts/Makefile ]]; then
  echo "[ERROR] tools/scripts/Makefile not found at __BASE_SHA__"
  exit 1
fi

verilator --version | head -n1
riscv64-unknown-elf-gcc --version | head -n1
python --version
""".replace("__BASE_SHA__", base_sha)

    def _prepare_finalize_script(self) -> str:
        return render_finalize_script("/home/caliptra-rtl")

    def files(self) -> list[File]:
        override_prepare = self._load_prepare_script().strip()
        prepare_finalize = self._prepare_finalize_script()
        if override_prepare:
            prepare_script = f"{override_prepare.rstrip()}\n\n{prepare_finalize}\n"
        else:
            prepare_dev = self._prepare_dev_script().rstrip()
            prepare_script = f"{prepare_dev}\n\n{prepare_finalize}\n"

        return [
            File(
                ".",
                "prepare.sh",
                prepare_script,
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            src = (
                f"{file.dir.rstrip('/')}/{file.name}"
                if file.dir and file.dir not in (".", "./")
                else file.name
            )
            copy_commands += f"COPY {src} /home/\n"

        prepare_commands = "RUN bash /home/prepare.sh && rm -f /home/prepare.sh"

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

{prepare_commands}

{self.clear_env}

"""


@Instance.register("chipsalliance", "caliptra-rtl")
class Caliptra(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return CaliptraImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        if run_cmd:
            return run_cmd
        return "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        if test_patch_run_cmd:
            return test_patch_run_cmd
        return "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        if fix_patch_run_cmd:
            return fix_patch_run_cmd
        return "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        return parse_test_markers(test_log)

    def runtime_files(self) -> list[File]:
        tb_script = (
            self.pr.tb_script
            if isinstance(getattr(self.pr, "tb_script", ""), str)
            else ""
        )
        return [
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

true
""",
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e
export MAKEFLAGS="-j1"

cd /home/caliptra-rtl
git reset --hard
git clean -fdx

BASE_FILE="/home/base_commit.txt"
if [[ -f "$BASE_FILE" ]]; then
  git checkout "$(cat "$BASE_FILE")"
else
  echo "[ERROR] Missing $BASE_FILE (image not prepared?)"
  exit 1
fi

git submodule update --init --recursive || true

if [[ -s /home/tb_script.sh ]]; then
  bash /home/tb_script.sh
  exit $?
fi

echo "TEST: test_execution ... SKIP"
echo "[ERROR] /home/tb_script.sh missing or empty"
exit 1
""",
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e
export MAKEFLAGS="-j1"

cd /home/caliptra-rtl
git reset --hard
git clean -fdx

BASE_FILE="/home/base_commit.txt"
if [[ -f "$BASE_FILE" ]]; then
  git checkout "$(cat "$BASE_FILE")"
else
  echo "[ERROR] Missing $BASE_FILE (image not prepared?)"
  exit 1
fi

git submodule update --init --recursive || true

if [[ -s /home/fix.patch ]]; then
  git apply /home/fix.patch || true
fi

if [[ -s /home/tb_script.sh ]]; then
  bash /home/tb_script.sh
  exit $?
fi

echo "TEST: test_execution ... SKIP"
echo "[ERROR] /home/tb_script.sh missing or empty"
exit 1
""",
            ),
            File(
                ".",
                "tb_script.sh",
                tb_script,
            ),
        ]
