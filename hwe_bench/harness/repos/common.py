from __future__ import annotations

from hwe_bench.harness.base import TestResult


_RESULTS_START = "HWE_BENCH_RESULTS_START"
_RESULTS_END = "HWE_BENCH_RESULTS_END"


def render_finalize_script(
    repo_dir: str,
    base_commit_path: str = "/home/base_commit.txt",
) -> str:
    return f"""
# Stage 5: truncate git history to prevent leakage
cd {repo_dir}
echo "[INFO] Truncating git history (remove remotes/extra refs/reflog/unreachable objects)"

cur="$(git symbolic-ref --quiet --short HEAD || true)"
if [[ -z "$cur" ]]; then
  git checkout -B hwe-base
  cur="hwe-base"
fi

git remote | xargs -r -n1 git remote remove
git for-each-ref --format='%(refname)' refs \
  | awk -v cur="refs/heads/$cur" '$0 != cur' \
  | xargs -r -n1 -I{{}} git update-ref -d {{}}

git branch --unset-upstream || true
git reflog expire --expire=now --all
git gc --prune=now
git fsck --full --no-reflogs --unreachable --dangling

# Clean submodule git metadata (flatten into plain directories)
git submodule absorbgitdirs --recursive || true
rm -rf .git/modules
find . -path '*/.git' \\( -type f -o -type l \\) -delete

# Commit all working tree changes (prepare_script may have left untracked/modified files).
# This ensures: (1) git reset --hard returns to a clean baseline with all env prep intact,
# (2) git diff $BASE_SHA --cached only captures agent changes, not prepare_script residue.
git add -A
git diff --cached --quiet || git -c user.name=hwe-bench -c user.email=hwe-bench@localhost commit -m "baseline"

# Record baseline commit for runtime scripts (test-run.sh / fix-run.sh)
git rev-parse HEAD > {base_commit_path}
echo "[INFO] Baseline commit: $(cat {base_commit_path})"

echo "[INFO] Environment preparation complete"
""".strip()


def parse_test_markers(test_log: str) -> TestResult:
    if _RESULTS_START in test_log and _RESULTS_END in test_log:
        test_log = test_log.split(_RESULTS_START, 1)[1].rsplit(_RESULTS_END, 1)[0]

    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    for raw_line in test_log.splitlines():
        line = raw_line.strip()
        if not line or not line.startswith("TEST:"):
            continue

        parts = line.split("...")
        if len(parts) != 2:
            continue

        test_name = parts[0].replace("TEST:", "").strip()
        status = parts[1].strip()
        if status == "PASS":
            passed_tests.add(test_name)
        elif status == "FAIL":
            failed_tests.add(test_name)
        elif status == "SKIP":
            skipped_tests.add(test_name)

    return TestResult(
        passed_count=len(passed_tests),
        failed_count=len(failed_tests),
        skipped_count=len(skipped_tests),
        passed_tests=passed_tests,
        failed_tests=failed_tests,
        skipped_tests=skipped_tests,
    )
