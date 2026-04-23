from __future__ import annotations

from typing import Optional, Union

from hwe_bench.harness.base import Config, File, Image, Instance, PullRequest, TestResult
from hwe_bench.harness.repos.common import parse_test_markers, render_finalize_script


class RocketChipImageBase(Image):
    """Base environment image for chipsalliance/rocket-chip."""

    build_timeout_sec = 1200

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
        if self.config.global_env and self.config.global_env.get("ROCKETCHIP_BASE_IMG"):
            base_img = str(self.config.global_env["ROCKETCHIP_BASE_IMG"]).strip()
            if not base_img or any(ch.isspace() for ch in base_img):
                raise ValueError(f"Invalid ROCKETCHIP_BASE_IMG: {base_img!r}")
        return base_img

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        sbt_wrapper = """#!/bin/bash
set -euo pipefail

find_project_root() {
  local dir="${ROCKETCHIP_HOME:-$PWD}"

  if [[ ! -d "$dir" ]]; then
    dir="$PWD"
  fi

  while [[ "$dir" != "/" ]]; do
    if [[ -f "$dir/sbt-launch.jar" || -f "$dir/build.sbt" || -f "$dir/project/build.properties" || -f "$dir/project/build.scala" ]]; then
      echo "$dir"
      return 0
    fi
    dir="$(dirname "$dir")"
  done

  echo "$PWD"
}

root="$(find_project_root)"

if [[ -f "$root/sbt-launch.jar" ]]; then
  cd "$root"
  exec java \
    -Dsbt.boot.directory="${SBT_BOOT_DIR:-/tools/sbt/boot}" \
    -Dsbt.ivy.home="${SBT_IVY_HOME:-/tools/sbt/ivy}" \
    -Dsbt.global.base="${SBT_GLOBAL_BASE:-/tools/sbt/global}" \
    -jar "$root/sbt-launch.jar" "$@"
fi

exec /usr/local/bin/sbt-system "$@"
"""

        install_verilator = """#!/bin/bash
set -euo pipefail

version="${1:?usage: install-verilator-version VERSION [PREFIX]}"
prefix="${2:-/tools/verilator-v${version}}"
jobs="${NUM_JOBS:-4}"

if [[ -x "$prefix/bin/verilator" ]]; then
  exit 0
fi

rm -rf "$prefix"
mkdir -p "$(dirname "$prefix")"

work_dir="$(mktemp -d /tmp/verilator-${version}.XXXXXX)"
archive="$work_dir/verilator.tar.gz"
src_dir="$work_dir/src"

echo "[INFO] Building Verilator ${version} into ${prefix}"
curl -fLs "https://github.com/verilator/verilator/archive/refs/tags/v${version}.tar.gz" -o "$archive"
mkdir -p "$src_dir"
tar -C "$src_dir" --strip-components=1 -xzf "$archive"

cd "$src_dir"
autoconf
./configure --prefix="$prefix"
make -j"$jobs"
make install

if [[ -d "$prefix/share/verilator/bin" ]]; then
  mkdir -p "$prefix/bin"
  for helper in "$prefix"/share/verilator/bin/*; do
    [[ -e "$helper" ]] || continue
    helper_name="$(basename "$helper")"
    if [[ ! -e "$prefix/bin/$helper_name" ]]; then
      ln -sfn "$helper" "$prefix/bin/$helper_name"
    fi
  done
fi

if [[ -d "$prefix/share/verilator/include" ]]; then
  ln -sfn "$prefix/share/verilator/include" "$prefix/include"
fi

verilated_cpp="$prefix/share/verilator/include/verilated.cpp"
if [[ -f "$verilated_cpp" ]] && ! grep -q '^#include <limits>$' "$verilated_cpp"; then
  sed -i '/^#include <sstream>$/a #include <limits>' "$verilated_cpp" || true
fi

rm -rf "$work_dir"
"""

        install_riscv_toolchain = """#!/bin/bash
set -euo pipefail

prefix="${1:-/tools/riscv}"
if [[ -x "$prefix/bin/riscv-none-elf-gcc" || -x "$prefix/bin/riscv64-unknown-elf-gcc" ]]; then
  exit 0
fi

url="${ROCKETCHIP_RISCV_TOOLCHAIN_URL:-https://github.com/xpack-dev-tools/riscv-none-elf-gcc-xpack/releases/download/v14.2.0-3/xpack-riscv-none-elf-gcc-14.2.0-3-linux-x64.tar.gz}"
tmp_dir="$(mktemp -d /tmp/riscv-toolchain.XXXXXX)"
archive="$tmp_dir/toolchain.tar.gz"

rm -rf "$prefix"
mkdir -p "$prefix"
curl -fLs "$url" -o "$archive"
tar -C "$prefix" --strip-components=1 -xf "$archive"

if [[ -d "$prefix/bin" ]]; then
  for bin in "$prefix"/bin/riscv-none-elf-*; do
    [[ -e "$bin" ]] || continue
    base="$(basename "$bin")"
    alt="${base/riscv-none-elf/riscv64-unknown-elf}"
    ln -sfn "$base" "$prefix/bin/$alt"
  done
fi

rm -rf "$tmp_dir"
"""

        return [
            File(".", "sbt", sbt_wrapper),
            File(".", "install-verilator-version", install_verilator),
            File(".", "install-riscv-toolchain", install_riscv_toolchain),
        ]

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
ENV JAVA8_HOME=/opt/jdk8
ENV JAVA11_HOME=/usr/lib/jvm/java-11-openjdk-amd64
ENV JAVA17_HOME=/usr/lib/jvm/java-17-openjdk-amd64
ENV COURSIER_CACHE=/tools/coursier
ENV SBT_BOOT_DIR=/tools/sbt/boot
ENV SBT_GLOBAL_BASE=/tools/sbt/global
ENV SBT_IVY_HOME=/tools/sbt/ivy

COPY sbt /usr/local/bin/sbt
COPY install-verilator-version /usr/local/bin/install-verilator-version
COPY install-riscv-toolchain /usr/local/bin/install-riscv-toolchain

RUN apt-get update && \\
    apt-get install -y --no-install-recommends \\
      autoconf \\
      automake \\
      autotools-dev \\
      bison \\
      build-essential \\
      ca-certificates \\
      curl \\
      device-tree-compiler \\
      file \\
      flex \\
      g++ \\
      gawk \\
      git \\
      gnupg \\
      gperf \\
      help2man \\
      jq \\
      less \\
      libelf-dev \\
      libfl-dev \\
      libfl2 \\
      libgmp-dev \\
      libgoogle-perftools-dev \\
      libmpc-dev \\
      libmpfr-dev \\
      libreadline-dev \\
      libtool \\
      libusb-1.0-0-dev \\
      make \\
      openjdk-11-jdk \\
      openjdk-17-jdk \\
      patch \\
      pkg-config \\
      procps \\
      python3 \\
      python3-dev \\
      python3-pip \\
      python3-setuptools \\
      python3-wheel \\
      rsync \\
      software-properties-common \\
      texinfo \\
      tmux \\
      unzip \\
      wget \\
      xz-utils \\
      zip \\
      zlib1g-dev && \\
    rm -rf /var/lib/apt/lists/*

RUN chmod +x /usr/local/bin/sbt \\
             /usr/local/bin/install-verilator-version \\
             /usr/local/bin/install-riscv-toolchain && \\
    printf '#!/bin/bash\\necho 4\\n' > /usr/local/bin/nproc && \\
    chmod +x /usr/local/bin/nproc

RUN curl -fLs -o /tmp/jdk8.tar.gz \\
      https://github.com/adoptium/temurin8-binaries/releases/download/jdk8u462-b08/OpenJDK8U-jdk_x64_linux_hotspot_8u462b08.tar.gz && \\
    mkdir -p /opt/jdk8 && \\
    tar -C /opt/jdk8 --strip-components=1 -xzf /tmp/jdk8.tar.gz && \\
    rm -f /tmp/jdk8.tar.gz

RUN curl -fLs -o /tmp/sbt.tgz \\
      https://github.com/sbt/sbt/releases/download/v1.10.7/sbt-1.10.7.tgz && \\
    mkdir -p /opt && \\
    tar -C /opt -xzf /tmp/sbt.tgz && \\
    mv /opt/sbt /opt/sbt-1.10.7 && \\
    ln -sfn /opt/sbt-1.10.7 /opt/sbt-current && \\
    ln -sfn /opt/sbt-current/bin/sbt /usr/local/bin/sbt-system && \\
    rm -f /tmp/sbt.tgz

RUN curl -Ls https://micro.mamba.pm/api/micromamba/linux-64/latest | \\
      tar -xvj -C /usr/local/bin --strip-components=1 bin/micromamba && \\
    /usr/local/bin/micromamba create -y -n rocketchip -c conda-forge python=3.10 pip pyyaml && \\
    /usr/local/bin/micromamba clean --all --yes

RUN printf '%s\\n' \\
  'export MAMBA_ROOT_PREFIX=/opt/micromamba' \\
  'export JAVA8_HOME=/opt/jdk8' \\
  'export JAVA11_HOME=/usr/lib/jvm/java-11-openjdk-amd64' \\
  'export JAVA17_HOME=/usr/lib/jvm/java-17-openjdk-amd64' \\
  'eval "$(/usr/local/bin/micromamba shell hook --shell=bash)"' \\
  'if [[ "${CONDA_DEFAULT_ENV:-}" != "rocketchip" ]]; then' \\
  '  micromamba activate rocketchip' \\
  'fi' \\
  'if [[ -f /etc/rocket_chip_tools_path.sh ]]; then' \\
  '  source /etc/rocket_chip_tools_path.sh' \\
  'fi' \\
  > /etc/rocket_chip_bash_env
ENV BASH_ENV=/etc/rocket_chip_bash_env
RUN echo 'source /etc/rocket_chip_bash_env' >> /root/.bashrc

RUN mkdir -p /tools/bin /tools/coursier /tools/sbt/boot /tools/sbt/global /tools/sbt/ivy && \\
    /usr/local/bin/install-riscv-toolchain /tools/riscv && \\
    curl -fLs -o /tmp/verilator-v4.210.tar.gz https://storage.googleapis.com/verilator-builds/verilator-v4.210.tar.gz && \\
    tar -C /tools -xzf /tmp/verilator-v4.210.tar.gz && \\
    ln -sfn /tools/v4.210 /tools/verilator-v4.210 && \\
    ln -sfn /tools/verilator-v4.210 /tools/verilator && \\
    if [ -d /tools/verilator-v4.210/share/verilator/bin ]; then \\
      mkdir -p /tools/verilator-v4.210/bin; \\
      for helper in /tools/verilator-v4.210/share/verilator/bin/*; do \\
        [ -e "$helper" ] || continue; \\
        helper_name="$(basename "$helper")"; \\
        if [ ! -e "/tools/verilator-v4.210/bin/$helper_name" ]; then \\
          ln -sfn "$helper" "/tools/verilator-v4.210/bin/$helper_name"; \\
        fi; \\
      done; \\
    fi && \\
    if [ -d /tools/verilator/share/verilator/include ]; then \\
      ln -sfn /tools/verilator/share/verilator/include /tools/verilator/include; \\
    fi && \\
    if [ -f /tools/verilator/share/verilator/include/verilated.cpp ] && ! grep -q '^#include <limits>$' /tools/verilator/share/verilator/include/verilated.cpp; then \\
      sed -i '/^#include <sstream>$/a #include <limits>' /tools/verilator/share/verilator/include/verilated.cpp || true; \\
    fi && \\
    rm -f /tmp/verilator-v4.210.tar.gz

RUN printf '%s\\n' \\
  'export NUM_JOBS="${NUM_JOBS:-4}"' \\
  'export MAKEFLAGS="-j${NUM_JOBS}"' \\
  'export ROCKETCHIP_HOME=/home/rocket-chip' \\
  'export JAVA8_HOME=/opt/jdk8' \\
  'export JAVA11_HOME=/usr/lib/jvm/java-11-openjdk-amd64' \\
  'export JAVA17_HOME=/usr/lib/jvm/java-17-openjdk-amd64' \\
  'export JAVA_HOME="${JAVA_HOME:-/usr/lib/jvm/java-11-openjdk-amd64}"' \\
  'export RISCV="${RISCV:-/tools/riscv}"' \\
  'export VERILATOR_ROOT="${VERILATOR_ROOT:-/tools/verilator}"' \\
  'export VERILATOR_BIN="${VERILATOR_BIN:-verilator_bin}"' \\
  'export COURSIER_CACHE="${COURSIER_CACHE:-/tools/coursier}"' \\
  'export SBT_BOOT_DIR="${SBT_BOOT_DIR:-/tools/sbt/boot}"' \\
  'export SBT_GLOBAL_BASE="${SBT_GLOBAL_BASE:-/tools/sbt/global}"' \\
  'export SBT_IVY_HOME="${SBT_IVY_HOME:-/tools/sbt/ivy}"' \\
  'for d in /tools/bin /tools/verilator/bin /tools/riscv/bin /opt/sbt-current/bin; do' \\
  '  if [[ -d "$d" ]]; then' \\
  '    export PATH="$d:$PATH"' \\
  '  fi' \\
  'done' \\
  'if [[ -d "${RISCV}/lib" ]]; then' \\
  '  export LIBRARY_PATH="${RISCV}/lib${LIBRARY_PATH:+:$LIBRARY_PATH}"' \\
  '  export LD_LIBRARY_PATH="${RISCV}/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"' \\
  'fi' \\
  'if [[ -d "${VERILATOR_ROOT}/include" ]]; then' \\
  '  export C_INCLUDE_PATH="${VERILATOR_ROOT}/include:${VERILATOR_ROOT}/include/vltstd${C_INCLUDE_PATH:+:$C_INCLUDE_PATH}"' \\
  '  export CPLUS_INCLUDE_PATH="${VERILATOR_ROOT}/include:${VERILATOR_ROOT}/include/vltstd${CPLUS_INCLUDE_PATH:+:$CPLUS_INCLUDE_PATH}"' \\
  'fi' \\
  > /etc/rocket_chip_tools_path.sh
RUN grep -q "/etc/rocket_chip_tools_path.sh" /etc/rocket_chip_bash_env || \\
    echo 'source /etc/rocket_chip_tools_path.sh' >> /etc/rocket_chip_bash_env

RUN git clone https://github.com/chipsalliance/rocket-chip.git /home/rocket-chip
WORKDIR /home/rocket-chip
RUN git config --global --add safe.directory /home/rocket-chip

RUN ln -sf /bin/bash /bin/sh

__CLEAR_ENV__

CMD ["/bin/bash"]
""".replace("__IMAGE_NAME__", str(image_name)).replace("__GLOBAL_ENV__", self.global_env).replace(
            "__CLEAR_ENV__", self.clear_env
        )


class RocketChipImageDefault(Image):
    """Default image which builds on RocketChipImageBase and injects prepare.sh."""

    build_timeout_sec = 1800

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
        return RocketChipImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _prepare_dev_script(self) -> str:
        base_sha = self.pr.base.sha
        return """#!/bin/bash
set -euo pipefail

export NUM_JOBS="${NUM_JOBS:-4}"
export ROCKETCHIP_HOME=/home/rocket-chip
export MAKEFLAGS="-j${NUM_JOBS}"
export COURSIER_CACHE="${COURSIER_CACHE:-/tools/coursier}"
export SBT_BOOT_DIR="${SBT_BOOT_DIR:-/tools/sbt/boot}"
export SBT_GLOBAL_BASE="${SBT_GLOBAL_BASE:-/tools/sbt/global}"
export SBT_IVY_HOME="${SBT_IVY_HOME:-/tools/sbt/ivy}"

detect_sbt_version() {
  if [[ -n "${ROCKETCHIP_SBT_VERSION:-}" ]]; then
    echo "$ROCKETCHIP_SBT_VERSION"
    return 0
  fi

  for props in project/build.properties build.properties; do
    if [[ -f "$props" ]]; then
      local version
      version="$(sed -n 's/^[[:space:]]*sbt.version[[:space:]]*=[[:space:]]*//p' "$props" | head -n1 | tr -d ' \\t\\r\\n')"
      if [[ -n "$version" ]]; then
        echo "$version"
        return 0
      fi
    fi
  done

  echo "1.10.7"
}

detect_java_candidates() {
  local sbt_version="$1"

  if [[ -n "${ROCKETCHIP_JAVA_VERSION:-}" ]]; then
    echo "$ROCKETCHIP_JAVA_VERSION"
    return 0
  fi

  if grep -RqsE 'java-version:[[:space:]]*17|temurin[^0-9]*17|openjdk[^0-9]*17|JAVA_HOME_17|--release[[:space:]]+17|sourceCompatibility[^0-9]*17|targetCompatibility[^0-9]*17' \
      .github/workflows build.sbt project README.md README_GITHUB_ACTIONS.md scripts 2>/dev/null; then
    echo "17 11 8"
    return 0
  fi

  local major="${sbt_version%%.*}"
  if [[ "$major" == "0" ]]; then
    echo "8 11 17"
    return 0
  fi

  if [[ "$(printf '%s\\n' "1.8.0" "$sbt_version" | sort -V | head -n1)" == "1.8.0" ]]; then
    echo "17 11 8"
    return 0
  fi

  echo "11 17 8"
}

configure_java_home() {
  local version="$1"
  case "$version" in
    8)
      export JAVA_HOME="$JAVA8_HOME"
      ;;
    11)
      export JAVA_HOME="$JAVA11_HOME"
      ;;
    17)
      export JAVA_HOME="$JAVA17_HOME"
      ;;
    *)
      return 1
      ;;
  esac

  export PATH="$JAVA_HOME/bin:/tools/bin:/tools/verilator/bin:/tools/riscv/bin:/opt/sbt-current/bin:$PATH"
}

detect_sbt_launcher_mode() {
  if [[ -f sbt-launch.jar ]]; then
    echo "repo-sbt-launch.jar"
  else
    echo "system-sbt"
  fi
}

probe_sbt() {
  timeout "${ROCKETCHIP_SBT_PROBE_TIMEOUT:-900}" sbt about >/tmp/rocketchip-sbt-probe.log 2>&1
}

select_java_home() {
  local sbt_version="$1"
  local candidates
  local first=""

  SELECTED_JAVA_VERSION=""
  candidates="$(detect_java_candidates "$sbt_version")"
  for version in $candidates; do
    configure_java_home "$version" || continue
    if [[ -z "$first" ]]; then
      first="$version"
    fi

    if probe_sbt; then
      SELECTED_JAVA_VERSION="$version"
      return 0
    fi

    echo "[WARN] SBT probe failed with Java ${version}; trying the next candidate" >&2
    tail -n 40 /tmp/rocketchip-sbt-probe.log >&2 || true
  done

  if [[ -n "$first" ]]; then
    configure_java_home "$first"
    SELECTED_JAVA_VERSION="$first"
    echo "[WARN] All SBT probes failed; falling back to Java ${first}" >&2
    tail -n 40 /tmp/rocketchip-sbt-probe.log >&2 || true
    return 0
  fi

  echo "[ERROR] Unable to select a Java runtime" >&2
  return 1
}

detect_verilator_version() {
  if [[ -n "${ROCKETCHIP_VERILATOR_VERSION:-}" ]]; then
    echo "$ROCKETCHIP_VERILATOR_VERSION"
    return 0
  fi

  local hinted
  hinted="$(
    grep -RhoE 'verilator[^0-9]*v?([0-9]+\\.[0-9]+)' \
      README.md README_GITHUB_ACTIONS.md Makefrag .github/workflows regression scripts verilator.hash 2>/dev/null \
      | grep -oE '([0-9]+\\.[0-9]+)' \
      | head -n1 || true
  )"
  if [[ -n "$hinted" ]]; then
    echo "$hinted"
    return 0
  fi

  echo "4.210"
}

install_python_deps() {
  python -m pip install -U pip setuptools wheel

  local installed=0
  for req in requirements.txt python-requirements.txt scripts/requirements.txt; do
    if [[ -f "$req" ]]; then
      echo "[INFO] Installing Python dependencies from $req"
      python -m pip install -r "$req"
      installed=1
    fi
  done

  if [[ "$installed" -eq 0 ]]; then
    python -m pip install -U PyYAML
  fi
}

write_tools_env() {
  local java_version="$1"
  local sbt_version="$2"
  local launcher_mode="$3"
  local verilator_version="$4"

  cat > /etc/rocket_chip_tools_path.sh <<EOF
export NUM_JOBS="${NUM_JOBS}"
export MAKEFLAGS="-j${NUM_JOBS}"
export ROCKETCHIP_HOME=/home/rocket-chip
export JAVA8_HOME=${JAVA8_HOME}
export JAVA11_HOME=${JAVA11_HOME}
export JAVA17_HOME=${JAVA17_HOME}
export JAVA_HOME=/usr/lib/jvm/java-${java_version}-openjdk-amd64
if [[ "${java_version}" == "8" ]]; then
  export JAVA_HOME=${JAVA8_HOME}
fi
export SBT_VERSION=${sbt_version}
export ROCKETCHIP_SBT_LAUNCHER=${launcher_mode}
export RISCV=/tools/riscv
export VERILATOR_ROOT=/tools/verilator
export VERILATOR_BIN=verilator_bin
export ROCKETCHIP_VERILATOR_VERSION=${verilator_version}
export COURSIER_CACHE=/tools/coursier
export SBT_BOOT_DIR=/tools/sbt/boot
export SBT_GLOBAL_BASE=/tools/sbt/global
export SBT_IVY_HOME=/tools/sbt/ivy
for d in /tools/bin /tools/verilator/bin /tools/riscv/bin /opt/sbt-current/bin; do
  if [[ -d "\\$d" ]]; then
    export PATH="\\$d:\\$PATH"
  fi
done
export PATH="\\${JAVA_HOME}/bin:\\$PATH"
if [[ -d /tools/riscv/lib ]]; then
  export LIBRARY_PATH="/tools/riscv/lib\\${LIBRARY_PATH:+:\\$LIBRARY_PATH}"
  export LD_LIBRARY_PATH="/tools/riscv/lib\\${LD_LIBRARY_PATH:+:\\$LD_LIBRARY_PATH}"
fi
if [[ -d /tools/verilator/include ]]; then
  export C_INCLUDE_PATH="/tools/verilator/include:/tools/verilator/include/vltstd\\${C_INCLUDE_PATH:+:\\$C_INCLUDE_PATH}"
  export CPLUS_INCLUDE_PATH="/tools/verilator/include:/tools/verilator/include/vltstd\\${CPLUS_INCLUDE_PATH:+:\\$CPLUS_INCLUDE_PATH}"
fi
EOF
}

# Stage 1: checkout base_sha in a clean workspace (+submodules)
cd /home/rocket-chip
git reset --hard
git clean -fdx
git checkout __BASE_SHA__
git submodule sync --recursive || true
git submodule update --init --recursive

# Stage 2: detect the required JDK from build metadata, then validate with SBT
selected_sbt_version="$(detect_sbt_version)"
select_java_home "$selected_sbt_version"
selected_java_version="$SELECTED_JAVA_VERSION"
echo "[INFO] Selected Java ${selected_java_version}: $(java -version 2>&1 | head -n1)"

# Stage 3: detect the required SBT version and ensure the matching launcher path
selected_sbt_launcher="$(detect_sbt_launcher_mode)"
echo "[INFO] Selected SBT ${selected_sbt_version} via ${selected_sbt_launcher}"
timeout "${ROCKETCHIP_SBT_PROBE_TIMEOUT:-900}" sbt about >/tmp/rocketchip-sbt-about.log 2>&1 || true
tail -n 20 /tmp/rocketchip-sbt-about.log || true

# Stage 4: install per-commit dependencies, select Verilator, and persist tool PATH
selected_verilator_version="$(detect_verilator_version)"
selected_verilator_dir="/tools/verilator-v${selected_verilator_version}"
if [[ "$selected_verilator_version" == "4.210" ]]; then
  selected_verilator_dir="/tools/verilator-v4.210"
else
  /usr/local/bin/install-verilator-version "$selected_verilator_version" "$selected_verilator_dir"
fi
ln -sfn "$selected_verilator_dir" /tools/verilator
if [[ -d /tools/verilator/share/verilator/include ]]; then
  ln -sfn /tools/verilator/share/verilator/include /tools/verilator/include
fi

/usr/local/bin/install-riscv-toolchain /tools/riscv
install_python_deps
write_tools_env "$selected_java_version" "$selected_sbt_version" "$selected_sbt_launcher" "$selected_verilator_version"
if ! grep -q "/etc/rocket_chip_tools_path.sh" /etc/rocket_chip_bash_env; then
  echo "source /etc/rocket_chip_tools_path.sh" >> /etc/rocket_chip_bash_env
fi
source /etc/rocket_chip_tools_path.sh

echo "[INFO] SBT ready: $(sbt --script-version 2>/dev/null || echo "launcher-managed")"
echo "[INFO] Verilator ready: $(verilator --version | head -n1)"
echo "[INFO] Toolchain ready: $(riscv64-unknown-elf-gcc --version 2>/dev/null | head -n1 || riscv-none-elf-gcc --version | head -n1)"
""".replace("__BASE_SHA__", base_sha)

    def _prepare_finalize_script(self) -> str:
        return render_finalize_script("/home/rocket-chip")

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

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

RUN bash /home/prepare.sh && rm -f /home/prepare.sh

{self.clear_env}

"""


@Instance.register("chipsalliance", "rocket-chip")
class RocketChip(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return RocketChipImageDefault(self.pr, self._config)

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

cd /home/rocket-chip
git reset --hard
git clean -fdx

BASE_FILE="/home/base_commit.txt"
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

cd /home/rocket-chip
git reset --hard
git clean -fdx

BASE_FILE="/home/base_commit.txt"
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
