from __future__ import annotations

from typing import Optional, Union

from hwe_bench.harness.base import Config, File, Image, Instance, PullRequest, TestResult
from hwe_bench.harness.repos.common import parse_test_markers, render_finalize_script


class OpenTitanImageBase(Image):
    """Base environment image for lowRISC/opentitan.

    Based on vcs:minimal, installs apt packages and micromamba env,
    clones the repository, and checks out a fixed commit by default.
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
        base_img = "vcs:minimal"
        if self.config.global_env and self.config.global_env.get("OPENTITAN_BASE_IMG"):
            base_img = str(self.config.global_env["OPENTITAN_BASE_IMG"]).strip()
            if not base_img or any(ch.isspace() for ch in base_img):
                raise ValueError(f"Invalid OPENTITAN_BASE_IMG: {base_img!r}")
        return base_img

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "opentitan-base"

    def files(self) -> list[File]:
        # Provide apt requirements and a minimal micromamba env to the build context.
        # Project-specific Python dependencies are installed after checkout in prepare.sh.
        # Includes packages needed by CLI agents (Claude Code, Codex CLI) when
        # dynamically installed via Harbor — see terminal-bench-2-verified fixes.
        apt_requirements = """build-essential
ca-certificates
gcc
libelf1
libelf-dev
libftdi1-2
libftdi1-dev
libncursesw5
libpcsclite-dev
libssl-dev
libudev-dev
libusb-1.0-0
lrzsz
net-tools
srecord
xsltproc
xz-utils
zlib1g-dev
netcat
autoconf
bison
brotli
bzip2
clang-format
cmake
doxygen
flex
golang
lcov
lld
lsb-core
lsb-release
make
openssl
perl
pkgconf
tree
g++
git
grep
curl
ninja-build
tar
procps
file
jq
less
lsof
tmux
tzdata
"""

        return [
            File(".", "apt-requirements.txt", apt_requirements),
        ]

    @property
    def need_copy_code(self) -> bool:
        return False

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        opentitan_commit = (
            str(self.config.global_env.get("OPENTITAN_COMMIT")).strip()
            if self.config.global_env and self.config.global_env.get("OPENTITAN_COMMIT")
            else "7ae4fe0c16bf22c421d1c8e0bfcf4a8395a507ae"
        )
        if not opentitan_commit or any(ch.isspace() for ch in opentitan_commit):
            raise ValueError(f"Invalid OPENTITAN_COMMIT: {opentitan_commit!r}")

        return f"""FROM {image_name}

{self.global_env}

USER root

# Avoid any interactive prompts during apt installs
ENV DEBIAN_FRONTEND=noninteractive

# Install micromamba under a stable prefix
ENV MAMBA_ROOT_PREFIX=/opt/micromamba

ARG OPENTITAN_COMMIT={opentitan_commit}

# Bypass any host HTTP proxy for Ubuntu archives. Some proxies can
# produce intermittent stale or corrupted apt index fetches.
RUN printf '%s\\n' \
    'Acquire::http::Proxy::archive.ubuntu.com "DIRECT";' \
    'Acquire::http::Proxy::security.ubuntu.com "DIRECT";' \
    'Acquire::https::Proxy::archive.ubuntu.com "DIRECT";' \
    'Acquire::https::Proxy::security.ubuntu.com "DIRECT";' \
    'Acquire::Retries "3";' \
    > /etc/apt/apt.conf.d/99ubuntu-direct

# Install apt dependencies
COPY apt-requirements.txt /tmp/apt-requirements.txt
RUN apt-get update && \
    apt-get install -y --no-install-recommends $(cat /tmp/apt-requirements.txt) && \
    rm -f /tmp/apt-requirements.txt && \
    rm -rf /var/lib/apt/lists/*

# Prevent nproc from returning host CPU count inside containers.
# Without this, make -j$(nproc) on a 256-core host causes OOM.
# See: terminal-bench-2-verified (zai-org) caffe-cifar-10 fix.
RUN printf '#!/bin/bash\\necho 4\\n' > /usr/local/bin/nproc && \
    chmod +x /usr/local/bin/nproc

# Install micromamba (vcs:minimal does not provide it)
RUN curl -Ls https://micro.mamba.pm/api/micromamba/linux-64/latest | \
      tar -xvj -C /usr/local/bin --strip-components=1 bin/micromamba && \
    /usr/local/bin/micromamba clean --all --yes

# Create a minimal micromamba env (project deps installed later in prepare.sh)
RUN /usr/local/bin/micromamba create -y -n opentitan python=3.10 pip && \
    /usr/local/bin/micromamba clean --all --yes

# Auto-activate micromamba env for both interactive and non-interactive bash.
# NOTE: Avoid multi-line heredocs here to keep compatibility with legacy docker builders.
RUN printf '%s\\n' \\
  'export MAMBA_ROOT_PREFIX=/opt/micromamba' \\
  'eval "$(/usr/local/bin/micromamba shell hook --shell=bash)"' \\
  'if [[ "${{CONDA_DEFAULT_ENV:-}}" != "opentitan" ]]; then' \\
  '  micromamba activate opentitan' \\
  'fi' \\
  > /etc/opentitan_bash_env
ENV BASH_ENV=/etc/opentitan_bash_env
RUN echo 'source /etc/opentitan_bash_env' >> /root/.bashrc

# Clone OpenTitan repository and checkout fixed commit
RUN git clone https://github.com/lowRISC/opentitan /home/opentitan
WORKDIR /home/opentitan
RUN git fetch --all && \
    git checkout "${{OPENTITAN_COMMIT}}" && \
    git submodule update --init --recursive

# Set bash as default shell
RUN ln -sf /bin/bash /bin/sh

{self.clear_env}

CMD ["/bin/bash"]
"""


class OpenTitanImageDefault(Image):
    """Default PR image which builds on OpenTitanImageBase and injects patches/scripts."""

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
        return OpenTitanImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _prepare_dev_script(self) -> str:
        base_sha = self.pr.base.sha
        return """#!/bin/bash
set -e

# Stage 1: checkout base_sha in a clean workspace (+submodules)
cd /home/opentitan
git reset --hard
git clean -fdx
git checkout __BASE_SHA__
git submodule update --init --recursive

# Stage 2: install repository apt dependencies (if present)
export DEBIAN_FRONTEND=noninteractive
if [[ -f apt-requirements.txt ]]; then
  echo "[INFO] Installing apt dependencies from apt-requirements.txt"
  cp apt-requirements.txt /tmp/opentitan-apt-requirements.txt
  sed -i -e '/^$/d' -e '/^#/d' -e 's/#.*//' /tmp/opentitan-apt-requirements.txt
  if [[ -s /tmp/opentitan-apt-requirements.txt ]]; then
    apt-get update
    xargs -r apt-get install -y --no-install-recommends < /tmp/opentitan-apt-requirements.txt
    rm -rf /var/lib/apt/lists/*
  else
    echo "[INFO] apt-requirements.txt is empty after filtering comments"
  fi
  rm -f /tmp/opentitan-apt-requirements.txt
else
  echo "[WARN] apt-requirements.txt not found at __BASE_SHA__; skipping repo-specific apt install"
fi

# Stage 3: install project-specific Python dependencies from the checked-out repo
if [[ -f python-requirements.txt ]]; then
  echo "[INFO] Installing Python deps from python-requirements.txt"
  python -m pip install -U pip "setuptools<66.0.0"
  cp python-requirements.txt /tmp/opentitan-python-requirements.txt
  python - <<'PY'
from pathlib import Path
import re

path = Path("/tmp/opentitan-python-requirements.txt")
lines = []
for line in path.read_text(encoding="utf-8").splitlines():
    stripped = line.strip()
    if stripped in {
        "types-dataclasses",
        "types-pkg_resources",
    }:
        continue
    line = re.sub(r'(#egg=[A-Za-z0-9_.-]+)\\s+[<>=!~].*', r'\\1', line)
    lines.append(line)
path.write_text("\\n".join(lines) + "\\n", encoding="utf-8")
PY
  python -m pip install -r /tmp/opentitan-python-requirements.txt --no-warn-script-location
  rm -f /tmp/opentitan-python-requirements.txt
else
  echo "[ERROR] python-requirements.txt not found at __BASE_SHA__"
  exit 1
fi

# Stage 4: install RISC-V toolchain + Verible, then persist their PATH
if [[ -f util/get-toolchain.py ]]; then
  toolchain_version=""
  if [[ -f util/container/Dockerfile ]]; then
    toolchain_version="$(sed -n 's/^ARG RISCV_TOOLCHAIN_TAR_VERSION=//p' util/container/Dockerfile | head -n1)"
  fi

  echo "[INFO] Installing RISC-V toolchain via util/get-toolchain.py"
  if [[ -n "$toolchain_version" ]]; then
    python util/get-toolchain.py --update -r "$toolchain_version"
  else
    echo "[WARN] RISCV_TOOLCHAIN_TAR_VERSION not found; falling back to util/get-toolchain.py defaults"
    python util/get-toolchain.py --update
  fi
else
  echo "[WARN] util/get-toolchain.py not found at __BASE_SHA__; skipping RISC-V toolchain install"
fi

verible_version=""
if [[ -f hw/tool_requirements.py ]]; then
  verible_version="$(python - <<'PY'
from pathlib import Path

ns = {}
exec(Path("hw/tool_requirements.py").read_text(encoding="utf-8"), ns)
reqs = ns.get("__TOOL_REQUIREMENTS__", {})
entry = reqs.get("verible", "")
if isinstance(entry, dict):
    print(entry.get("min_version", ""))
elif isinstance(entry, str):
    print(entry)
PY
)"
fi
if [[ -z "$verible_version" && -f util/container/Dockerfile ]]; then
  verible_version="$(sed -n 's/^ARG VERIBLE_VERSION=//p' util/container/Dockerfile | head -n1)"
fi

if [[ -n "$verible_version" ]]; then
  echo "[INFO] Installing Verible ${verible_version}"
  rm -rf /tools/verible
  mkdir -p /tools/verible
  for asset in \
    "verible-${verible_version}-linux-static-x86_64.tar.gz" \
    "verible-${verible_version}-Ubuntu-22.04-jammy-x86_64.tar.gz" \
    "verible-${verible_version}-Ubuntu-20.04-focal-x86_64.tar.gz" \
    "verible-${verible_version}-Ubuntu-18.04-bionic-x86_64.tar.gz"
  do
    url="https://github.com/chipsalliance/verible/releases/download/${verible_version}/${asset}"
    if curl -f -Ls -o /tmp/verible.tar.gz "$url"; then
      tar -C /tools/verible -xf /tmp/verible.tar.gz --strip-components=1
      rm -f /tmp/verible.tar.gz
      break
    fi
  done
else
  echo "[WARN] Unable to determine Verible version from hw/tool_requirements.py or util/container/Dockerfile"
fi

cat > /etc/opentitan_tools_path.sh <<'EOF'
for d in /tools/riscv/bin /tools/verible/bin; do
  if [ -d "$d" ]; then
    export PATH="$d:$PATH"
  fi
done
true
EOF

if ! grep -q "/etc/opentitan_tools_path.sh" /etc/opentitan_bash_env; then
  echo "source /etc/opentitan_tools_path.sh" >> /etc/opentitan_bash_env
fi
source /etc/opentitan_tools_path.sh

python -m pip show edalize fusesoc hjson >/dev/null
if command -v riscv32-unknown-elf-gcc >/dev/null 2>&1; then
  echo "[INFO] RISC-V toolchain ready: $(riscv32-unknown-elf-gcc --version | head -n1)"
else
  echo "[WARN] riscv32-unknown-elf-gcc not found after Stage 4"
fi
if command -v verible-verilog-lint >/dev/null 2>&1; then
  echo "[INFO] Verible ready: $(verible-verilog-lint --version | head -n1)"
else
  echo "[WARN] verible-verilog-lint not found after Stage 4"
fi
""".replace("__BASE_SHA__", base_sha)

    def _prepare_finalize_script(self) -> str:
        return render_finalize_script(
            "/home/opentitan",
            "/home/opentitan_base_commit.txt",
        )

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


@Instance.register("lowRISC", "opentitan")
class OpenTitan(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return OpenTitanImageDefault(self.pr, self._config)

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

cd /home/opentitan
git reset --hard
git clean -fdx

BASE_FILE="/home/opentitan_base_commit.txt"
if [[ -f "$BASE_FILE" ]]; then
  git checkout "$(cat "$BASE_FILE")"
else
  echo "[ERROR] Missing $BASE_FILE (image not prepared?)"
  exit 1
fi

git submodule update --init --recursive

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

cd /home/opentitan
git reset --hard
git clean -fdx

BASE_FILE="/home/opentitan_base_commit.txt"
if [[ -f "$BASE_FILE" ]]; then
  git checkout "$(cat "$BASE_FILE")"
else
  echo "[ERROR] Missing $BASE_FILE (image not prepared?)"
  exit 1
fi

git submodule update --init --recursive

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
