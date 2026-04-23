from __future__ import annotations

from typing import Optional, Union

from hwe_bench.harness.base import Config, File, Image, Instance, PullRequest, TestResult
from hwe_bench.harness.repos.common import parse_test_markers, render_finalize_script


class IbexImageBase(Image):
    """Base environment image for lowRISC/ibex.

    Generates a Dockerfile equivalent to the provided template:
    - FROM base image (default "ubuntu:22.04"), configurable via Config.global_env["IBEX_BASE_IMG"]
    - Install minimal OS packages and micromamba (Python 3.10)
    - Create and auto-activate a micromamba env named "ibex"
    - Clone ibex repository to /home/ibex (toolchains installed later in prepare.sh)
    - WORKDIR /home/ibex, CMD ["/bin/bash"]
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
        # Allow override via global_env
        base_img = "ubuntu:22.04"
        if self.config.global_env and self.config.global_env.get("IBEX_BASE_IMG"):
            base_img = str(self.config.global_env["IBEX_BASE_IMG"]).strip()
            if not base_img or any(ch.isspace() for ch in base_img):
                raise ValueError(f"Invalid IBEX_BASE_IMG: {base_img!r}")
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

        return f"""FROM {image_name}

{self.global_env}

USER root

# Avoid any interactive prompts during apt installs
ENV DEBIAN_FRONTEND=noninteractive

# Install base OS packages (toolchains installed later in prepare.sh)
# Includes packages needed by CLI agents (Claude Code, Codex CLI) when
# dynamically installed via Harbor — see terminal-bench-2-verified fixes.
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      autoconf \
      bison \
      build-essential \
      ca-certificates \
      clang-format \
      curl \
      device-tree-compiler \
      flex \
      git \
      libcairo2-dev \
      libelf-dev \
      python3 \
      python3-dev \
      python3-pip \
      python3-setuptools \
      python3-wheel \
      python3-yaml \
      srecord \
      tar \
      wget \
      xz-utils \
      bzip2 \
      procps \
      file \
      jq \
      less \
      lsof \
      tmux && \
    rm -rf /var/lib/apt/lists/*

# Prevent nproc from returning host CPU count inside containers.
# Without this, make -j$(nproc) on a 256-core host causes OOM.
# See: terminal-bench-2-verified (zai-org) caffe-cifar-10 fix.
RUN printf '#!/bin/bash\\necho 4\\n' > /usr/local/bin/nproc && \
    chmod +x /usr/local/bin/nproc

# Install micromamba (pinned-ish: latest) and create an env for ibex workflows
ENV MAMBA_ROOT_PREFIX=/opt/micromamba
RUN curl -Ls https://micro.mamba.pm/api/micromamba/linux-64/latest | \
      tar -xvj -C /usr/local/bin --strip-components=1 bin/micromamba && \
    /usr/local/bin/micromamba create -y -n ibex python=3.10 pip && \
    /usr/local/bin/micromamba clean --all --yes

# Auto-activate micromamba env for both interactive and non-interactive bash.
# NOTE: Avoid multi-line heredocs here to keep compatibility with legacy docker builders.
RUN printf '%s\\n' \\
  'export MAMBA_ROOT_PREFIX=/opt/micromamba' \\
  'eval "$(/usr/local/bin/micromamba shell hook --shell=bash)"' \\
  'if [[ "${{CONDA_DEFAULT_ENV:-}}" != "ibex" ]]; then' \\
  '  micromamba activate ibex' \\
  'fi' \\
  > /etc/ibex_bash_env
ENV BASH_ENV=/etc/ibex_bash_env
RUN echo 'source /etc/ibex_bash_env' >> /root/.bashrc

# Clone ibex repository (full history; checked out/truncated in prepare.sh)
RUN git clone https://github.com/lowRISC/ibex.git /home/ibex
WORKDIR /home/ibex

# Set bash as default shell
RUN ln -sf /bin/bash /bin/sh

{self.clear_env}

CMD ["/bin/bash"]
"""


class IbexImageDefault(Image):
    """Default image which builds on IbexImageBase and injects patches/scripts."""

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
        """Load test bench script content from tb_script field.

        Return empty string if field doesn't exist or is empty.
        """
        if hasattr(self.pr, "tb_script") and isinstance(self.pr.tb_script, str):
            return self.pr.tb_script
        return ""

    def dependency(self) -> Image | None:
        return IbexImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _prepare_dev_script(self) -> str:
        base_sha = self.pr.base.sha
        return """#!/bin/bash
set -e

# Stage 1: checkout base_sha in a clean workspace (+submodules)
cd /home/ibex
git reset --hard
git clean -fdx
git checkout __BASE_SHA__
git submodule update --init --recursive

# Stage 2: install toolchains (default: run repo script if present; fallback if missing/incompatible)
export DEBIAN_FRONTEND=noninteractive

install_ok=0
if [[ -f ci/install-build-deps.sh ]]; then
  echo "[INFO] Installing toolchains via ci/install-build-deps.sh"

  if [[ -f ci/vars.env ]]; then
    echo "[INFO] Loading tool versions from ci/vars.env"
    set -a
    # shellcheck source=/dev/null
    source ci/vars.env
    set +a
  elif [[ -f ci/vars.yml || -f ci/vars.yaml ]]; then
    echo "[INFO] Loading tool versions from ci/vars.yml"
    /usr/bin/python3 - <<'PY' > /tmp/ibex_vars.sh
import os
import shlex
import yaml

path = "ci/vars.yml"
if not os.path.exists(path):
    path = "ci/vars.yaml"

with open(path, "r", encoding="utf-8") as f:
    data = yaml.safe_load(f) or {}

if not isinstance(data, dict):
    raise SystemExit(f"Unexpected YAML structure in {path}: {type(data).__name__}")

vars_dict = data
if isinstance(data.get("variables"), dict):
    vars_dict = data["variables"]

for key, value in vars_dict.items():
    if isinstance(value, (str, int, float)) and key.isidentifier():
        print(f"export {key}={shlex.quote(str(value))}")
PY
    set -a
    # shellcheck source=/dev/null
    source /tmp/ibex_vars.sh
    set +a
  else
    echo "[WARN] No ci/vars.env or ci/vars.yml found; running install script without explicit version vars"
  fi

  chmod +x ci/install-build-deps.sh
  set +e
  bash ci/install-build-deps.sh
  rc=$?
  set -e
  if [[ $rc -eq 0 ]]; then
    install_ok=1
  else
    echo "[WARN] ci/install-build-deps.sh failed (rc=$rc) on this OS; will try fallback installer"
  fi
else
  echo "[WARN] ci/install-build-deps.sh not found at __BASE_SHA__; will use fallback installer"
fi

if [[ $install_ok -ne 1 ]]; then
  echo "[INFO] Fallback toolchain install: minimal deps for Verilator + fusesoc flows"
  apt-get update
  apt-get install -y --no-install-recommends pkg-config && rm -rf /var/lib/apt/lists/*

  if [[ -n "${VERILATOR_VERSION:-}" ]]; then
    echo "[INFO] Installing Verilator ${VERILATOR_VERSION} (prebuilt)"
    mkdir -p /tools/verilator
    verilator_tag="${VERILATOR_VERSION:-v4.210}"
    if [[ "$verilator_tag" != v* ]]; then
      verilator_tag="v${verilator_tag}"
    fi
    min_verilator_tag="v4.210"
    if [[ "$(printf '%s\n' "${verilator_tag#v}" "${min_verilator_tag#v}" | sort -V | tail -n1)" == "${min_verilator_tag#v}" ]]; then
      verilator_tag="${min_verilator_tag}"
    fi
    curl -fLs -o /tmp/verilator.tar.gz \
      "https://storage.googleapis.com/verilator-builds/verilator-${verilator_tag}.tar.gz"
    tar -C /tools/verilator -xzf /tmp/verilator.tar.gz
    ln -sfn "/tools/verilator/${verilator_tag}" /tools/verilator/current
    if [[ -n "${VERILATOR_VERSION:-}" && "$verilator_tag" != "${VERILATOR_VERSION}" ]]; then
      ln -sfn "/tools/verilator/${verilator_tag}" "/tools/verilator/${VERILATOR_VERSION}"
    fi
    verilated_cpp="/tools/verilator/${verilator_tag}/share/verilator/include/verilated.cpp"
    if [[ -f "$verilated_cpp" ]] && ! grep -q '^#include <limits>$' "$verilated_cpp"; then
      echo "[INFO] Patching verilator include for Ubuntu 22.04 compatibility: $verilated_cpp"
      sed -i '/^#include <sstream>$/a #include <limits>' "$verilated_cpp"
    fi
  else
    echo "[INFO] Installing Verilator via apt (no VERILATOR_VERSION set)"
    apt-get update
    apt-get install -y --no-install-recommends verilator && rm -rf /var/lib/apt/lists/*
  fi

  python -m pip install -U pip
  if [[ -f python-requirements.txt ]]; then
    echo "[INFO] Installing Python deps from python-requirements.txt (filtered)"
    grep -v -E '^\\s*-r\\s+vendor/google_riscv-dv/requirements\\.txt\\s*$' \
      python-requirements.txt > /tmp/ibex_python_requirements.txt
    python -m pip install -r /tmp/ibex_python_requirements.txt
  else
    python -m pip install -U fusesoc edalize mako
  fi

  # RISC-V toolchain: use a known-good prebuilt toolchain (version match not required)
  mkdir -p /tools/riscv
  curl -Ls -o /tmp/rv32-toolchain.tar.xz \
    https://github.com/lowRISC/lowrisc-toolchains/releases/download/20220210-1/lowrisc-toolchain-gcc-rv32imcb-20220210-1.tar.xz
  tar -C /tools/riscv -xf /tmp/rv32-toolchain.tar.xz --strip-components=1
fi

# Stage 3: persist /tools PATH (verilator/riscv/verible, etc.)
if [[ -n "${VERILATOR_VERSION:-}" && -d "/tools/verilator/${VERILATOR_VERSION}/bin" ]]; then
  ln -sfn "/tools/verilator/${VERILATOR_VERSION}" /tools/verilator/current
fi

cat > /etc/ibex_tools_path.sh <<'EOF'
for d in /tools/riscv-isa-sim/bin /tools/verilator/current/bin /tools/verible/bin /tools/riscv/bin; do
  if [ -d "$d" ]; then
    export PATH="$d:$PATH"
  fi
done
true
EOF

if ! grep -q "/etc/ibex_tools_path.sh" /etc/ibex_bash_env; then
  echo "source /etc/ibex_tools_path.sh" >> /etc/ibex_bash_env
fi

# Stage 4: apply PATH for current shell
source /etc/ibex_tools_path.sh
""".replace("__BASE_SHA__", base_sha)

    def _prepare_finalize_script(self) -> str:
        return render_finalize_script("/home/ibex", "/home/ibex_base_commit.txt")

    def files(self) -> list[File]:
        # Only bake prepare.sh into the image (executed during docker build and then removed).
        # All other per-instance artifacts are mounted at runtime.
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


@Instance.register("lowRISC", "ibex")
class Ibex(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return IbexImageDefault(self.pr, self._config)

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

cd /home/ibex
git reset --hard
git clean -fdx

BASE_FILE="/home/ibex_base_commit.txt"
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

cd /home/ibex
git reset --hard
git clean -fdx

BASE_FILE="/home/ibex_base_commit.txt"
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
