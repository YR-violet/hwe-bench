from __future__ import annotations

from typing import Optional, Union

from hwe_bench.harness.base import Config, File, Image, Instance, PullRequest, TestResult
from hwe_bench.harness.repos.common import parse_test_markers, render_finalize_script


class Cva6ImageBase(Image):
    """Base environment image for openhwgroup/cva6.

    Uses Ubuntu 22.04 + apt packages + micromamba, clones the repository,
    and preinstalls the three dominant Verilator versions used by the dataset.
    RISC-V toolchain and Spike remain dynamic in the per-PR prepare flow.
    """

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
        if self.config.global_env and self.config.global_env.get("CVA6_BASE_IMG"):
            base_img = str(self.config.global_env["CVA6_BASE_IMG"]).strip()
            if not base_img or any(ch.isspace() for ch in base_img):
                raise ValueError(f"Invalid CVA6_BASE_IMG: {base_img!r}")
        return base_img

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    @property
    def need_copy_code(self) -> bool:
        return False

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        return """FROM __IMAGE_NAME__

__GLOBAL_ENV__

USER root

ENV DEBIAN_FRONTEND=noninteractive
ENV MAMBA_ROOT_PREFIX=/opt/micromamba
ENV NUM_JOBS=4
ENV MAKEFLAGS=-j4

RUN apt-get update && \\
    apt-get install -y --no-install-recommends \\
      autoconf \\
      automake \\
      bison \\
      build-essential \\
      ca-certificates \\
      cmake \\
      curl \\
      device-tree-compiler \\
      file \\
      flex \\
      g++ \\
      gawk \\
      git \\
      help2man \\
      jq \\
      less \\
      libboost-dev \\
      libboost-filesystem-dev \\
      libboost-iostreams-dev \\
      libboost-program-options-dev \\
      libboost-regex-dev \\
      libboost-serialization-dev \\
      libboost-system-dev \\
      libboost-thread-dev \\
      libelf-dev \\
      libfl-dev \\
      libfl2 \\
      libtool \\
      libyaml-cpp-dev \\
      make \\
      ninja-build \\
      patch \\
      pkg-config \\
      procps \\
      python3 \\
      python3-dev \\
      python3-pip \\
      python3-setuptools \\
      python3-wheel \\
      rsync \\
      tmux \\
      unzip \\
      xz-utils \\
      zlib1g-dev && \\
    rm -rf /var/lib/apt/lists/*

RUN printf '#!/bin/bash\\necho 4\\n' > /usr/local/bin/nproc && \\
    chmod +x /usr/local/bin/nproc

RUN curl -Ls https://micro.mamba.pm/api/micromamba/linux-64/latest | \\
      tar -xvj -C /usr/local/bin --strip-components=1 bin/micromamba && \\
    /usr/local/bin/micromamba create -y -n cva6 -c conda-forge python=3.10 pip && \\
    /usr/local/bin/micromamba clean --all --yes

RUN printf '%s\\n' \\
  'export MAMBA_ROOT_PREFIX=/opt/micromamba' \\
  'eval "$(/usr/local/bin/micromamba shell hook --shell=bash)"' \\
  'if [[ "${CONDA_DEFAULT_ENV:-}" != "cva6" ]]; then' \\
  '  micromamba activate cva6' \\
  'fi' \\
  > /etc/cva6_bash_env
ENV BASH_ENV=/etc/cva6_bash_env
RUN echo 'source /etc/cva6_bash_env' >> /root/.bashrc

RUN git clone https://github.com/openhwgroup/cva6.git /home/cva6
WORKDIR /home/cva6

RUN ln -sf /bin/bash /bin/sh

RUN bash -lc 'set -euo pipefail && \\
    cd /home/cva6 && \\
    git checkout v5.2.0 && \\
    export VERILATOR_INSTALL_DIR=/tools/verilator-v5.008 && \\
    export VERILATOR_BUILD_DIR=/tmp/verilator-build-v5.008 && \\
    bash verif/regress/install-verilator.sh && \\
    rm -rf "$VERILATOR_BUILD_DIR" && \\
    if [ -d /tools/verilator-v5.008/share/verilator/include ]; then \\
      ln -sfn /tools/verilator-v5.008/share/verilator/include /tools/verilator-v5.008/include; \\
    fi'

RUN set -e && \\
    work_dir="$(mktemp -d /tmp/verilator-v5018-XXXXXX)" && \\
    git clone --depth 1 --branch v5.018 https://github.com/verilator/verilator.git "$work_dir/src" && \\
    cd "$work_dir/src" && \\
    git apply /home/cva6/verif/regress/verilator-v5.patch || true && \\
    autoconf && \\
    ./configure --prefix=/tools/verilator-v5.018 && \\
    make -j4 && \\
    make install && \\
    if [ -d /tools/verilator-v5.018/share/verilator/include ]; then \\
      ln -sfn /tools/verilator-v5.018/share/verilator/include /tools/verilator-v5.018/include; \\
    fi && \\
    rm -rf "$work_dir"

RUN ln -sfn /tools/verilator-v5.008 /tools/verilator && \\
    bash -lc 'set -euo pipefail; \\
      default_branch="$(git symbolic-ref --short refs/remotes/origin/HEAD | sed "s@^origin/@@")"; \\
      git -C /home/cva6 checkout "$default_branch"; \\
      git -C /home/cva6 reset --hard; \\
      git -C /home/cva6 clean -fdx'

RUN cat > /tools/cva6_tool_manifest.txt <<'EOF'
CVA6 base image tool manifest
=============================

Preinstalled Verilator versions:
- /tools/verilator-v5.008
- /tools/verilator-v5.018

Default Verilator symlink:
- /tools/verilator -> /tools/verilator-v5.008

Dynamic tools installed during prepare stage:
- /tools/riscv (xPack riscv-none-elf toolchain)
- /tools/spike or /tools/riscv (depending on repo's Spike install script)

Environment defaults:
- NUM_JOBS=4
- /etc/cva6_tools_path.sh
EOF

RUN cat > /etc/cva6_tools_path.sh <<'EOF'
export NUM_JOBS="${NUM_JOBS:-4}"
export RISCV="${RISCV:-/tools/riscv}"
export CV_SW_PREFIX="${CV_SW_PREFIX:-riscv-none-elf-}"
export VERILATOR_INSTALL_DIR="${VERILATOR_INSTALL_DIR:-/tools/verilator}"
export VERILATOR_ROOT="${VERILATOR_ROOT:-${VERILATOR_INSTALL_DIR}}"
export VERILATOR_BIN="${VERILATOR_BIN:-${VERILATOR_INSTALL_DIR}/bin/verilator_bin}"
export SPIKE_INSTALL_DIR="${SPIKE_INSTALL_DIR:-/tools/spike}"
export SPIKE_PATH="${SPIKE_PATH:-${SPIKE_INSTALL_DIR}/bin}"

for d in "${VERILATOR_INSTALL_DIR}/bin" "${RISCV}/bin" "${SPIKE_INSTALL_DIR}/bin"; do
  if [[ -d "$d" ]]; then
    export PATH="$d:$PATH"
  fi
done

for d in "${SPIKE_INSTALL_DIR}/lib" "${RISCV}/lib"; do
  if [[ -d "$d" ]]; then
    export LIBRARY_PATH="$d${LIBRARY_PATH:+:$LIBRARY_PATH}"
    export LD_LIBRARY_PATH="$d${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
  fi
done

if [[ -d "${VERILATOR_ROOT}/include" ]]; then
  export C_INCLUDE_PATH="${VERILATOR_ROOT}/include:${VERILATOR_ROOT}/include/vltstd${C_INCLUDE_PATH:+:$C_INCLUDE_PATH}"
  export CPLUS_INCLUDE_PATH="${VERILATOR_ROOT}/include:${VERILATOR_ROOT}/include/vltstd${CPLUS_INCLUDE_PATH:+:$CPLUS_INCLUDE_PATH}"
fi
EOF

RUN grep -q "/etc/cva6_tools_path.sh" /etc/cva6_bash_env || \\
    echo 'source /etc/cva6_tools_path.sh' >> /etc/cva6_bash_env

__CLEAR_ENV__

CMD ["/bin/bash"]
""".replace("__IMAGE_NAME__", str(image_name)).replace("__GLOBAL_ENV__", self.global_env).replace(
            "__CLEAR_ENV__", self.clear_env
        )


class Cva6ImageDefault(Image):
    """Default image which builds on Cva6ImageBase and injects prepare.sh."""

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
        """Load per-instance prepare script content.

        If pr.prepare_script is non-empty, it overrides stages 1-4 of the default
        prepare flow. The framework always appends the canonical finalize
        section (stage 5) automatically.
        """
        if hasattr(self.pr, "prepare_script") and isinstance(self.pr.prepare_script, str):
            return self.pr.prepare_script
        return ""

    def _load_tb_script(self) -> str:
        """Load test bench script content from tb_script field."""
        if hasattr(self.pr, "tb_script") and isinstance(self.pr.tb_script, str):
            return self.pr.tb_script
        return ""

    def dependency(self) -> Image | None:
        return Cva6ImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _prepare_dev_script(self) -> str:
        base_sha = self.pr.base.sha
        return """#!/bin/bash
set -euo pipefail

export NUM_JOBS="${NUM_JOBS:-4}"

# Stage 1: checkout base_sha in a clean workspace (+submodules)
cd /home/cva6
git reset --hard
git clean -fdx
git checkout __BASE_SHA__
git submodule sync --recursive
git submodule update --init --recursive

# Stage 2: install the minimal Python deps needed by verif/sim/cva6.py
python -m pip install -U pip PyYAML bitstring

# Stage 3: install a prebuilt RISC-V toolchain instead of building GCC from source
toolchain_dir="/tools/riscv"
toolchain_url="${CVA6_RISCV_TOOLCHAIN_URL:-https://github.com/xpack-dev-tools/riscv-none-elf-gcc-xpack/releases/download/v14.2.0-3/xpack-riscv-none-elf-gcc-14.2.0-3-linux-x64.tar.gz}"
if [[ ! -x "$toolchain_dir/bin/riscv-none-elf-gcc" ]]; then
  echo "[INFO] Installing prebuilt RISC-V toolchain from $toolchain_url"
  rm -rf "$toolchain_dir"
  mkdir -p "$toolchain_dir"
  curl -fLs -o /tmp/cva6-riscv-toolchain.tar.gz "$toolchain_url"
  tar -C "$toolchain_dir" -xf /tmp/cva6-riscv-toolchain.tar.gz --strip-components=1
  rm -f /tmp/cva6-riscv-toolchain.tar.gz
else
  echo "[INFO] Reusing existing RISC-V toolchain at $toolchain_dir"
fi

# Stage 4: select repo-required Verilator, persist tool env vars, then build Spike dynamically
detect_verilator_script() {
  if [[ -f verif/regress/install-verilator.sh ]]; then
    echo "verif/regress/install-verilator.sh"
  elif [[ -f ci/install-verilator.sh ]]; then
    echo "ci/install-verilator.sh"
  fi
}

detect_verilator_version() {
  local script="$1"
  local version=""
  if [[ -n "$script" ]]; then
    version="$(sed -n 's/.*VERILATOR_HASH=\"\\([^\"]*\\)\".*/\\1/p' "$script" | head -n1)"
    if [[ -z "$version" ]]; then
      version="$(sed -n 's@.*verilator-\\([0-9][0-9.]*\\)\\.t.*@\\1@p' "$script" | head -n1)"
    fi
  fi
  echo "$version"
}

preinstalled_verilator_dir() {
  case "$1" in
    v5.008|v5.018)
      echo "/tools/verilator-$1"
      ;;
    *)
      return 1
      ;;
  esac
}

build_verilator_on_demand() {
  local script="$1"
  local version="$2"
  local prefix="$3"
  if [[ "$script" == "verif/regress/install-verilator.sh" ]]; then
    echo "[INFO] Building Verilator via $script into $prefix"
    export VERILATOR_INSTALL_DIR="$prefix"
    export VERILATOR_BUILD_DIR="/tmp/verilator-build-${version}"
    bash "$script"
    rm -rf "$VERILATOR_BUILD_DIR"
  else
    echo "[INFO] Building Verilator via $script into $prefix"
    mkdir -p /home/cva6/tmp
    export VERILATOR_ROOT="$prefix"
    bash "$script"
    rm -rf /home/cva6/tmp/verilator-*
  fi
  if [[ -d "$prefix/share/verilator/include" ]]; then
    ln -sfn "$prefix/share/verilator/include" "$prefix/include"
  fi
}

detect_spike_script() {
  if [[ -f verif/regress/install-spike.sh ]]; then
    echo "verif/regress/install-spike.sh"
  elif [[ -f ci/install-spike.sh ]]; then
    echo "ci/install-spike.sh"
  fi
}

write_tools_env() {
  local spike_dir="$1"
  cat > /etc/cva6_tools_path.sh <<EOF
export NUM_JOBS="${NUM_JOBS}"
export RISCV=/tools/riscv
export CV_SW_PREFIX="\\${CV_SW_PREFIX:-riscv-none-elf-}"
export VERILATOR_INSTALL_DIR=/tools/verilator
export VERILATOR_ROOT=/tools/verilator
export VERILATOR_BIN=/tools/verilator/bin/verilator_bin
export SPIKE_INSTALL_DIR=${spike_dir}
export SPIKE_PATH=${spike_dir}/bin
for d in /tools/verilator/bin /tools/riscv/bin ${spike_dir}/bin; do
  if [[ -d "\\$d" ]]; then
    export PATH="\\$d:\\$PATH"
  fi
done
for d in ${spike_dir}/lib /tools/riscv/lib; do
  if [[ -d "\\$d" ]]; then
    export LIBRARY_PATH="\\$d\\${LIBRARY_PATH:+:\\$LIBRARY_PATH}"
    export LD_LIBRARY_PATH="\\$d\\${LD_LIBRARY_PATH:+:\\$LD_LIBRARY_PATH}"
  fi
done
if [[ -d /tools/verilator/include ]]; then
  export C_INCLUDE_PATH="/tools/verilator/include:/tools/verilator/include/vltstd\\${C_INCLUDE_PATH:+:\\$C_INCLUDE_PATH}"
  export CPLUS_INCLUDE_PATH="/tools/verilator/include:/tools/verilator/include/vltstd\\${CPLUS_INCLUDE_PATH:+:\\$CPLUS_INCLUDE_PATH}"
fi
EOF
}

verilator_script="$(detect_verilator_script)"
required_verilator="$(detect_verilator_version "$verilator_script")"
if [[ -z "$verilator_script" || -z "$required_verilator" ]]; then
  echo "[ERROR] Unable to determine required Verilator version"
  exit 1
fi

selected_verilator_dir=""
if selected_verilator_dir="$(preinstalled_verilator_dir "$required_verilator" 2>/dev/null)"; then
  if [[ -x "$selected_verilator_dir/bin/verilator" ]]; then
    echo "[INFO] Reusing preinstalled Verilator $required_verilator from $selected_verilator_dir"
  else
    build_verilator_on_demand "$verilator_script" "$required_verilator" "$selected_verilator_dir"
  fi
else
  selected_verilator_dir="/tools/verilator-$required_verilator"
  if [[ -x "$selected_verilator_dir/bin/verilator" ]]; then
    echo "[INFO] Reusing dynamically installed Verilator from $selected_verilator_dir"
  else
    build_verilator_on_demand "$verilator_script" "$required_verilator" "$selected_verilator_dir"
  fi
fi

ln -sfn "$selected_verilator_dir" /tools/verilator

spike_script="$(detect_spike_script)"
selected_spike_dir="/tools/spike"
if [[ "$spike_script" == "ci/install-spike.sh" ]]; then
  selected_spike_dir="/tools/riscv"
fi

write_tools_env "$selected_spike_dir"
if ! grep -q "/etc/cva6_tools_path.sh" /etc/cva6_bash_env; then
  echo "source /etc/cva6_tools_path.sh" >> /etc/cva6_bash_env
fi
source /etc/cva6_tools_path.sh

if [[ -n "$spike_script" ]]; then
  if [[ "$spike_script" == "verif/regress/install-spike.sh" ]]; then
    if [[ ! -x "/tools/spike/bin/spike" || ! -x "/tools/spike/bin/spike-dasm" ]]; then
      echo "[INFO] Building Spike via $spike_script"
      rm -rf verif/core-v-verif/vendor/riscv/riscv-isa-sim/build
      export SPIKE_INSTALL_DIR=/tools/spike
      bash "$spike_script"
    else
      echo "[INFO] Reusing existing Spike install at /tools/spike"
    fi
  else
    if [[ ! -x "/tools/riscv/bin/spike" || ! -x "/tools/riscv/bin/spike-dasm" ]]; then
      echo "[INFO] Building Spike via $spike_script"
      export RISCV=/tools/riscv
      bash "$spike_script"
    else
      echo "[INFO] Reusing existing Spike install under /tools/riscv"
    fi
    ln -sfn /tools/riscv /tools/spike
    selected_spike_dir="/tools/spike"
  fi
else
  echo "[INFO] No repo-provided Spike install script for this commit"
fi

write_tools_env "$selected_spike_dir"
source /etc/cva6_tools_path.sh

echo "[INFO] Verilator ready: $(verilator --version | head -n1)"
echo "[INFO] Toolchain ready: $(riscv-none-elf-gcc --version | head -n1)"
if command -v spike >/dev/null 2>&1; then
  echo "[INFO] Spike ready: $(spike --version | head -n1 || true)"
else
  echo "[INFO] Spike ready: not-installed"
fi
""".replace("__BASE_SHA__", base_sha)

    def _prepare_finalize_script(self) -> str:
        return render_finalize_script("/home/cva6", "/home/cva6_base_commit.txt")

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


@Instance.register("openhwgroup", "cva6")
class Cva6(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return Cva6ImageDefault(self.pr, self._config)

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
        tb_script = self.pr.tb_script if isinstance(getattr(self.pr, "tb_script", ""), str) else ""
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

cd /home/cva6
git reset --hard
git clean -fdx

BASE_FILE="/home/cva6_base_commit.txt"
if [[ -f "$BASE_FILE" ]]; then
  git checkout "$(cat "$BASE_FILE")"
else
  echo "[ERROR] Missing $BASE_FILE (image not prepared?)"
  exit 1
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
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/cva6
git reset --hard
git clean -fdx

BASE_FILE="/home/cva6_base_commit.txt"
if [[ -f "$BASE_FILE" ]]; then
  git checkout "$(cat "$BASE_FILE")"
else
  echo "[ERROR] Missing $BASE_FILE (image not prepared?)"
  exit 1
fi

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
