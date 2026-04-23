from __future__ import annotations

from typing import Optional, Union

from hwe_bench.harness.base import Config, File, Image, Instance, PullRequest, TestResult
from hwe_bench.harness.repos.common import parse_test_markers, render_finalize_script


class XiangShanImageBase(Image):
    """Base environment image for OpenXiangShan/XiangShan."""

    build_timeout_sec = 600

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
        if self.config.global_env and self.config.global_env.get("XIANGSHAN_BASE_IMG"):
            base_img = str(self.config.global_env["XIANGSHAN_BASE_IMG"]).strip()
            if not base_img or any(ch.isspace() for ch in base_img):
                raise ValueError(f"Invalid XIANGSHAN_BASE_IMG: {base_img!r}")
        return base_img

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        install_mill = """#!/bin/bash
set -euo pipefail

version="${1:?usage: install-mill-version VERSION [DEST]}"
dest="${2:-/tools/mill/bin/mill-${version}}"

mkdir -p "$(dirname "$dest")"
if [[ -x "$dest" ]]; then
  exit 0
fi

tmp="$(mktemp "${TMPDIR:-/tmp}/mill-${version}.XXXXXX")"
urls=(
  "https://github.com/com-lihaoyi/mill/releases/download/${version}/${version}"
  "https://repo1.maven.org/maven2/com/lihaoyi/mill-dist/${version}/mill-dist-${version}-mill.sh"
  "https://repo.maven.apache.org/maven2/com/lihaoyi/mill-dist/${version}/mill-dist-${version}-mill.sh"
  "https://github.com/com-lihaoyi/mill/releases/download/${version}/${version}-assembly"
)

for url in "${urls[@]}"; do
  if curl -fL --retry 3 --retry-all-errors "$url" -o "$tmp"; then
    mv "$tmp" "$dest"
    chmod +x "$dest"
    exit 0
  fi
done

rm -f "$tmp"
echo "[ERROR] Unable to download mill launcher for version ${version}" >&2
exit 1
"""

        mill_wrapper = """#!/bin/bash
set -euo pipefail

find_mill_version() {
  local dir="${PWD}"

  while [[ "$dir" != "/" ]]; do
    if [[ -f "$dir/.mill-version" ]]; then
      tr -d ' \\t\\r\\n' < "$dir/.mill-version"
      return 0
    fi
    dir="$(dirname "$dir")"
  done

  if [[ -f "/home/xiangshan/.mill-version" ]]; then
    tr -d ' \\t\\r\\n' < /home/xiangshan/.mill-version
    return 0
  fi

  echo "0.12.15"
}

version="${MILL_VERSION:-$(find_mill_version)}"
/usr/local/bin/install-mill-version "$version"
exec "/tools/mill/bin/mill-${version}" "$@"
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

url="${XSHAN_RISCV_TOOLCHAIN_URL:-https://github.com/xpack-dev-tools/riscv-none-elf-gcc-xpack/releases/download/v14.2.0-3/xpack-riscv-none-elf-gcc-14.2.0-3-linux-x64.tar.gz}"
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
            File(".", "install-mill-version", install_mill),
            File(".", "mill", mill_wrapper),
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
ENV JAVA11_HOME=/usr/lib/jvm/java-11-openjdk-amd64
ENV JAVA17_HOME=/usr/lib/jvm/java-17-openjdk-amd64

COPY install-mill-version /usr/local/bin/install-mill-version
COPY mill /usr/local/bin/mill
COPY install-verilator-version /usr/local/bin/install-verilator-version
COPY install-riscv-toolchain /usr/local/bin/install-riscv-toolchain

RUN apt-get update && \\
    apt-get install -y --no-install-recommends \\
      autoconf \\
      automake \\
      bison \\
      build-essential \\
      ca-certificates \\
      clang \\
      curl \\
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
      libgoogle-perftools-dev \\
      libreadline-dev \\
      libsdl2-dev \\
      libsqlite3-dev \\
      libtool \\
      make \\
      ninja-build \\
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
      ripgrep \\
      rsync \\
      tmux \\
      unzip \\
      wget \\
      xz-utils \\
      zip \\
      zlib1g-dev && \\
    rm -rf /var/lib/apt/lists/*

RUN chmod +x /usr/local/bin/install-mill-version \\
             /usr/local/bin/mill \\
             /usr/local/bin/install-verilator-version \\
             /usr/local/bin/install-riscv-toolchain && \\
    printf '#!/bin/bash\\necho 4\\n' > /usr/local/bin/nproc && \\
    chmod +x /usr/local/bin/nproc

RUN curl -Ls https://micro.mamba.pm/api/micromamba/linux-64/latest | \\
      tar -xvj -C /usr/local/bin --strip-components=1 bin/micromamba && \\
    /usr/local/bin/micromamba create -y -n xiangshan -c conda-forge python=3.10 pip pyyaml && \\
    /usr/local/bin/micromamba clean --all --yes

RUN printf '%s\\n' \\
  'export MAMBA_ROOT_PREFIX=/opt/micromamba' \\
  'export JAVA11_HOME=/usr/lib/jvm/java-11-openjdk-amd64' \\
  'export JAVA17_HOME=/usr/lib/jvm/java-17-openjdk-amd64' \\
  'eval "$(/usr/local/bin/micromamba shell hook --shell=bash)"' \\
  'if [[ "${CONDA_DEFAULT_ENV:-}" != "xiangshan" ]]; then' \\
  '  micromamba activate xiangshan' \\
  'fi' \\
  'export PATH=/tools/bin:/tools/mill/bin:$PATH' \\
  'if [[ -f /etc/xiangshan_tools_path.sh ]]; then' \\
  '  source /etc/xiangshan_tools_path.sh' \\
  'fi' \\
  > /etc/xiangshan_bash_env
ENV BASH_ENV=/etc/xiangshan_bash_env
RUN echo 'source /etc/xiangshan_bash_env' >> /root/.bashrc

RUN mkdir -p /tools/bin /tools/mill/bin /tools/nemu-shim/build && \\
    /usr/local/bin/install-mill-version 0.7.4 && \\
    /usr/local/bin/install-mill-version 0.9.6 && \\
    /usr/local/bin/install-mill-version 0.9.8 && \\
    /usr/local/bin/install-mill-version 0.11.1 && \\
    /usr/local/bin/install-mill-version 0.11.7 && \\
    /usr/local/bin/install-mill-version 0.11.8 && \\
    /usr/local/bin/install-mill-version 0.12.3 && \\
    /usr/local/bin/install-mill-version 0.12.15 && \\
    /usr/local/bin/install-mill-version 1.0.4 && \\
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

RUN /usr/local/bin/install-verilator-version 5.008 /tools/verilator-v5.008

RUN printf '%s\\n' \\
  'export NUM_JOBS="${NUM_JOBS:-4}"' \\
  'export MAKEFLAGS="-j${NUM_JOBS}"' \\
  'export NOOP_HOME=/home/xiangshan' \\
  'export XIANGSHAN_HOME=/home/xiangshan' \\
  'export JAVA_HOME="${JAVA_HOME:-/usr/lib/jvm/java-11-openjdk-amd64}"' \\
  'export MILL_VERSION="${MILL_VERSION:-0.12.15}"' \\
  'export RISCV_HOME="${RISCV_HOME:-/tools/riscv}"' \\
  'export NEMU_HOME="${NEMU_HOME:-/tools/nemu-shim}"' \\
  'export VERILATOR_ROOT="${VERILATOR_ROOT:-/tools/verilator}"' \\
  'export VERILATOR_BIN="${VERILATOR_BIN:-verilator_bin}"' \\
  'for d in /tools/bin /tools/mill/bin "${VERILATOR_ROOT}/bin" "${RISCV_HOME}/bin"; do' \\
  '  if [[ -d "$d" ]]; then' \\
  '    export PATH="$d:$PATH"' \\
  '  fi' \\
  'done' \\
  'if [[ -d "${RISCV_HOME}/lib" ]]; then' \\
  '  export LIBRARY_PATH="${RISCV_HOME}/lib${LIBRARY_PATH:+:$LIBRARY_PATH}"' \\
  '  export LD_LIBRARY_PATH="${RISCV_HOME}/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"' \\
  'fi' \\
  'if [[ -d "${VERILATOR_ROOT}/include" ]]; then' \\
  '  export C_INCLUDE_PATH="${VERILATOR_ROOT}/include:${VERILATOR_ROOT}/include/vltstd${C_INCLUDE_PATH:+:$C_INCLUDE_PATH}"' \\
  '  export CPLUS_INCLUDE_PATH="${VERILATOR_ROOT}/include:${VERILATOR_ROOT}/include/vltstd${CPLUS_INCLUDE_PATH:+:$CPLUS_INCLUDE_PATH}"' \\
  'fi' \\
  > /etc/xiangshan_tools_path.sh
RUN grep -q "/etc/xiangshan_tools_path.sh" /etc/xiangshan_bash_env || \\
    echo 'source /etc/xiangshan_tools_path.sh' >> /etc/xiangshan_bash_env

RUN git clone https://github.com/OpenXiangShan/XiangShan.git /home/xiangshan
WORKDIR /home/xiangshan
RUN git config --global --add safe.directory /home/xiangshan

RUN ln -sf /bin/bash /bin/sh

__CLEAR_ENV__

CMD ["/bin/bash"]
""".replace("__IMAGE_NAME__", str(image_name)).replace("__GLOBAL_ENV__", self.global_env).replace(
            "__CLEAR_ENV__", self.clear_env
        )


class XiangShanImageDefault(Image):
    """Default image which builds on XiangShanImageBase and injects prepare.sh."""

    build_timeout_sec = 600

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

    def dependency(self) -> Image | None:
        return XiangShanImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _prepare_dev_script(self) -> str:
        base_sha = self.pr.base.sha
        return """#!/bin/bash
set -euo pipefail

export NUM_JOBS="${NUM_JOBS:-4}"
export NOOP_HOME=/home/xiangshan
export XIANGSHAN_HOME=/home/xiangshan

detect_mill_version() {
  if [[ -n "${XSHAN_MILL_VERSION:-}" ]]; then
    echo "$XSHAN_MILL_VERSION"
    return 0
  fi

  if [[ -f .mill-version ]]; then
    local version
    version="$(tr -d ' \\t\\r\\n' < .mill-version)"
    if [[ -n "$version" ]]; then
      echo "$version"
      return 0
    fi
  fi

  if [[ -f build.mill ]]; then
    echo "0.12.15"
    return 0
  fi

  if [[ -f build.sc ]]; then
    if grep -q "chiselModule\\." Makefile build.sc 2>/dev/null; then
      echo "0.7.4"
    else
      echo "0.11.8"
    fi
    return 0
  fi

  echo "0.12.15"
}

detect_java_version() {
  if [[ -n "${XSHAN_JAVA_VERSION:-}" ]]; then
    echo "$XSHAN_JAVA_VERSION"
    return 0
  fi

  if grep -RqsE "java-version:[[:space:]]*['\\\"]?17|temurin[^0-9]*17|openjdk[^0-9]*17|JAVA_HOME_17|--release[[:space:]]+17|sourceCompatibility[^0-9]*17|targetCompatibility[^0-9]*17" \
    .github/workflows build.mill build.sc project Makefile README.md Dockerfile 2>/dev/null; then
    echo "17"
    return 0
  fi

  echo "11"
}

detect_verilator_version() {
  if [[ -n "${XSHAN_VERILATOR_VERSION:-}" ]]; then
    echo "$XSHAN_VERILATOR_VERSION"
    return 0
  fi

  local hinted
  hinted="$(
    grep -RhoE 'verilator[^0-9]*v?([0-9]+\\.[0-9]+)' Dockerfile Makefile README.md .github/workflows scripts 2>/dev/null \
      | grep -oE '([0-9]+\\.[0-9]+)' \
      | head -n1 || true
  )"
  if [[ -n "$hinted" ]]; then
    echo "$hinted"
    return 0
  fi

  if [[ -f build.mill ]]; then
    echo "5.008"
    return 0
  fi

  echo "4.210"
}

setup_nemu_home() {
  if [[ -n "${XSHAN_NEMU_HOME:-}" && -f "${XSHAN_NEMU_HOME}/build/riscv64-nemu-interpreter-so" ]]; then
    echo "$XSHAN_NEMU_HOME"
    return 0
  fi

  local shim="/tools/nemu-shim"
  mkdir -p "$shim/build"

  if [[ -f /home/xiangshan/ready-to-run/riscv64-nemu-interpreter-so ]]; then
    ln -sfn /home/xiangshan/ready-to-run/riscv64-nemu-interpreter-so "$shim/build/riscv64-nemu-interpreter-so"
  fi
  if [[ -f /home/xiangshan/ready-to-run/riscv64-nemu-interpreter-dual-so ]]; then
    ln -sfn /home/xiangshan/ready-to-run/riscv64-nemu-interpreter-dual-so "$shim/build/riscv64-nemu-interpreter-dual-so"
  fi

  if [[ ! -f "$shim/Makefile" ]]; then
    cat > "$shim/Makefile" <<'EOF'
all:
	@echo "NEMU shim: provide XSHAN_NEMU_HOME or rely on ready-to-run/*.so" >&2
	@exit 1

build/riscv64-nemu-interpreter-so:
	@echo "NEMU shim: missing build/riscv64-nemu-interpreter-so" >&2
	@exit 1

build/riscv64-nemu-interpreter-dual-so:
	@echo "NEMU shim: missing build/riscv64-nemu-interpreter-dual-so" >&2
	@exit 1
EOF
  fi

  echo "$shim"
}

install_python_deps() {
  python -m pip install -U pip setuptools wheel

  local installed=0
  for req in requirements.txt scripts/requirements.txt scripts/xspdb/requirements.txt; do
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

prefetch_repo_deps() {
  if grep -qE '^[[:space:]]*deps:' Makefile 2>/dev/null; then
    echo "[INFO] Warming repository dependencies via make deps"
    make deps MILL_OUTPUT_DIR=/tmp/.mill-out
  fi
}

write_tools_env() {
  local java_version="$1"
  local mill_version="$2"
  local nemu_home="$3"

  cat > /etc/xiangshan_tools_path.sh <<EOF
export NUM_JOBS="${NUM_JOBS}"
export MAKEFLAGS="-j${NUM_JOBS}"
export NOOP_HOME=/home/xiangshan
export XIANGSHAN_HOME=/home/xiangshan
export JAVA11_HOME=/usr/lib/jvm/java-11-openjdk-amd64
export JAVA17_HOME=/usr/lib/jvm/java-17-openjdk-amd64
export JAVA_HOME=/usr/lib/jvm/java-${java_version}-openjdk-amd64
export MILL_VERSION=${mill_version}
export RISCV_HOME=/tools/riscv
export NEMU_HOME=${nemu_home}
export VERILATOR_ROOT=/tools/verilator
export VERILATOR_BIN=verilator_bin
for d in /tools/bin /tools/mill/bin /tools/verilator/bin /tools/riscv/bin; do
  if [[ -d "\\$d" ]]; then
    export PATH="\\$d:\\$PATH"
  fi
done
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
cd /home/xiangshan
git reset --hard
git clean -fdx
git checkout __BASE_SHA__
git submodule sync --recursive || true
git submodule update --init --recursive

# Stage 2: detect and select the required Mill + Java toolchain
selected_mill_version="$(detect_mill_version)"
selected_java_version="$(detect_java_version)"
if [[ "$selected_java_version" == "17" ]]; then
  export JAVA_HOME=/usr/lib/jvm/java-17-openjdk-amd64
else
  export JAVA_HOME=/usr/lib/jvm/java-11-openjdk-amd64
fi
export PATH="$JAVA_HOME/bin:/tools/bin:/tools/mill/bin:$PATH"

/usr/local/bin/install-mill-version "$selected_mill_version"
export MILL_VERSION="$selected_mill_version"
echo "[INFO] Selected Mill ${selected_mill_version}"
echo "[INFO] Selected Java ${selected_java_version}: $(java -version 2>&1 | head -n1)"
mill -i --version

# Stage 3: detect and select the required Verilator
selected_verilator_version="$(detect_verilator_version)"
selected_verilator_dir="/tools/verilator-v${selected_verilator_version}"
/usr/local/bin/install-verilator-version "$selected_verilator_version" "$selected_verilator_dir"
ln -sfn "$selected_verilator_dir" /tools/verilator
if [[ -d /tools/verilator/share/verilator/include ]]; then
  ln -sfn /tools/verilator/share/verilator/include /tools/verilator/include
fi

selected_nemu_home="$(setup_nemu_home)"
write_tools_env "$selected_java_version" "$selected_mill_version" "$selected_nemu_home"
if ! grep -q "/etc/xiangshan_tools_path.sh" /etc/xiangshan_bash_env; then
  echo "source /etc/xiangshan_tools_path.sh" >> /etc/xiangshan_bash_env
fi
source /etc/xiangshan_tools_path.sh
echo "[INFO] Selected Verilator: $(verilator --version | head -n1)"

# Stage 4: install per-commit runtime dependencies and persist tool PATH
/usr/local/bin/install-riscv-toolchain /tools/riscv
source /etc/xiangshan_tools_path.sh
install_python_deps
prefetch_repo_deps
echo "[INFO] RISC-V toolchain: $(riscv64-unknown-elf-gcc --version 2>/dev/null | head -n1 || riscv-none-elf-gcc --version | head -n1)"
""".replace("__BASE_SHA__", base_sha)

    def _prepare_finalize_script(self) -> str:
        return render_finalize_script("/home/xiangshan")

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


@Instance.register("OpenXiangShan", "XiangShan")
class XiangShan(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return XiangShanImageDefault(self.pr, self._config)

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

cd /home/xiangshan
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

cd /home/xiangshan
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
  git apply --whitespace=nowarn /home/fix.patch || true
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
