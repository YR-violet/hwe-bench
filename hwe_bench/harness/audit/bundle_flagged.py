#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import re
import shutil
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable

SAFE_VERDICTS = {"genuine_fix", "true_unresolved"}


def _read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _read_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)


def _read_text_if_exists(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def _write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content or "", encoding="utf-8")


def _normalize_bundle_metadata(bundle_root: Path) -> None:
    bundle_root = bundle_root.resolve()

    summary_path = bundle_root / "flagged_cases.json"
    if summary_path.exists():
        summary = _read_json(summary_path)
        summary["bundle_root"] = str(bundle_root)
        for case in summary.get("cases", []):
            try:
                number = int(case["number"])
            except (KeyError, TypeError, ValueError):
                continue
            case["bundle_dir"] = str((bundle_root / f"pr-{number}").resolve())
        _write_json(summary_path, summary)

    index_path = bundle_root / "flagged_cases.jsonl"
    if index_path.exists():
        rows: list[dict[str, Any]] = []
        for row in _read_jsonl(index_path):
            try:
                number = int(row["number"])
            except (KeyError, TypeError, ValueError):
                rows.append(row)
                continue
            row["bundle_dir"] = str((bundle_root / f"pr-{number}").resolve())
            rows.append(row)
        _write_jsonl(index_path, rows)

    for manifest_path in sorted(bundle_root.glob("pr-*/case_manifest.json")):
        manifest = _read_json(manifest_path)
        manifest["bundle_dir"] = str(manifest_path.parent.resolve())
        _write_json(manifest_path, manifest)


def _copy_if_exists(src: Path | None, dst: Path) -> bool:
    if src is None or not src.exists():
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return True


def _safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()) or "unknown"


def _parse_instance_id(instance_id: str) -> tuple[str, str, int] | None:
    if ":" not in instance_id or "/" not in instance_id:
        return None
    org_repo, pr_part = instance_id.split(":", 1)
    org, repo = org_repo.split("/", 1)
    m = re.fullmatch(r"pr-(\d+)", pr_part)
    if not m:
        return None
    return org, repo, int(m.group(1))




def _discover_run_roots(audit_root: Path) -> list[Path]:
    if (audit_root / "run_meta.json").exists():
        return [audit_root]
    return sorted(
        path
        for path in audit_root.iterdir()
        if path.is_dir() and (path / "run_meta.json").exists()
    )


def _discover_named_files(root: Path, filename: str) -> list[Path]:
    if not root.exists():
        return []
    if root.is_file():
        return [root] if root.name == filename else []
    return sorted(root.rglob(filename))


def _load_dataset_rows(dataset_path: Path, org: str, repo: str) -> dict[int, dict[str, Any]]:
    rows: dict[int, dict[str, Any]] = {}
    for row in _read_jsonl(dataset_path):
        if row.get("org") != org or row.get("repo") != repo:
            continue
        rows[int(row["number"])] = row
    return rows


def _collect_audit_entries(
    audit_root: Path,
    *,
    org: str,
    repo: str,
    dataset_name: str | None = None,
) -> tuple[dict[int, list[dict[str, Any]]], set[int]]:
    entries_by_number: dict[int, list[dict[str, Any]]] = defaultdict(list)
    flagged_numbers: set[int] = set()

    for run_root in _discover_run_roots(audit_root):
        run_meta = _read_json(run_root / "run_meta.json")
        if run_meta.get("org") != org or run_meta.get("repo") != repo:
            continue
        if dataset_name and str(run_meta.get("dataset", "")) != dataset_name:
            continue

        agent = str(run_meta.get("agent", "unknown"))
        dataset = str(run_meta.get("dataset", run_root.name))
        run_id = run_root.name

        for batch_result_path in sorted(run_root.glob("batches/*/batch_result.json")):
            result = _read_json(batch_result_path)
            batch_id = str(result.get("batch_id", batch_result_path.parent.name))
            for case in result.get("cases", []):
                number = int(case["number"])
                entry = {
                    "number": number,
                    "agent": agent,
                    "dataset": dataset,
                    "run_id": run_id,
                    "run_root": run_root,
                    "case_dir": run_root / "cases" / f"pr-{number}",
                    "batch_id": batch_id,
                    "status": str(case.get("status", "")),
                    "trajectory_audit": case.get("trajectory_audit") or {},
                    "patch_review": case.get("patch_review") or {},
                    "evidence": case.get("evidence") or [],
                }
                entries_by_number[number].append(entry)
                verdict = str((entry["patch_review"] or {}).get("verdict", ""))
                if verdict and verdict not in SAFE_VERDICTS:
                    flagged_numbers.add(number)

    return entries_by_number, flagged_numbers


def _load_patch_candidates(
    patches_dir: Path,
    *,
    org: str,
    repo: str,
) -> dict[int, dict[str, dict[str, Any]]]:
    """Return {pr_number: {source_dir_name: {path, fix_patch}}}."""
    candidates: dict[int, dict[str, dict[str, Any]]] = defaultdict(dict)
    for patches_path in _discover_named_files(patches_dir, "patches.jsonl"):
        source_key = patches_path.parent.name  # e.g. "hwe-opentitan-sonnet-expansion-97"
        for row in _read_jsonl(patches_path):
            if row.get("org") != org or row.get("repo") != repo:
                continue
            candidates[int(row["number"])][source_key] = {
                "path": patches_path,
                "fix_patch": str(row.get("fix_patch", "") or ""),
            }
    return candidates


def _load_trial_index(
    jobs_dir: Path,
    *,
    org: str,
    repo: str,
) -> dict[int, list[Path]]:
    index: dict[int, list[Path]] = defaultdict(list)
    detail_paths = _discover_named_files(jobs_dir, "detail.json")

    for detail_path in detail_paths:
        if detail_path.parent.name != "verifier":
            continue
        trial_dir = detail_path.parent.parent
        try:
            detail = _read_json(detail_path)
        except Exception:
            continue
        parsed = _parse_instance_id(str(detail.get("instance_id", "")))
        if parsed is None:
            continue
        detail_org, detail_repo, number = parsed
        if detail_org == org and detail_repo == repo:
            index[number].append(trial_dir)

    if index:
        return index

    for trajectory_path in _discover_named_files(jobs_dir, "trajectory.json"):
        if trajectory_path.parent.name != "agent":
            continue
        m = re.search(r"pr-(\d+)", str(trajectory_path))
        if not m:
            continue
        index[int(m.group(1))].append(trajectory_path.parent.parent)

    return index


def _resolve_patch(
    entry: dict[str, Any],
    patch_candidates: dict[int, dict[str, dict[str, Any]]],
) -> tuple[str, str | None]:
    number = int(entry["number"])
    source_map = patch_candidates.get(number, {})
    agent = str(entry.get("agent", ""))
    run_id = str(entry.get("run_id", ""))

    # Exact match: find the source_key that contains the agent name
    for source_key, candidate in source_map.items():
        source_lower = source_key.lower()
        # Match by agent short name in source directory name
        for keyword in _agent_keywords(agent):
            if keyword in source_lower:
                return str(candidate.get("fix_patch", "") or ""), str(Path(candidate["path"]).resolve())

    # Single candidate: use it directly
    if len(source_map) == 1:
        candidate = next(iter(source_map.values()))
        return str(candidate.get("fix_patch", "") or ""), str(Path(candidate["path"]).resolve())

    # Fallback: read from Stage 1 audit case dir
    fallback_path = Path(entry["case_dir"]) / "agent_patch.diff"
    return _read_text_if_exists(fallback_path), str(fallback_path.resolve()) if fallback_path.exists() else None


def _agent_keywords(agent: str) -> list[str]:
    """Extract short identifying keywords from agent name for exact path matching."""
    lowered = agent.strip().lower()
    keywords: list[str] = []
    for kw in ("sonnet", "opus", "kimi", "codex", "deepseek", "ds", "qwen", "gemini"):
        if kw in lowered:
            keywords.append(kw)
    # "deepseek-v3.2" should match "ds" in path names like "hwe-opentitan-ds-expansion-97"
    if "deepseek" in lowered:
        keywords.append("ds")
    if "codex" in lowered:
        keywords.append("codex")
    return keywords


def _resolve_trajectory(entry: dict[str, Any], trial_index: dict[int, list[Path]]) -> tuple[Path | None, Path | None]:
    number = int(entry["number"])
    choices = trial_index.get(number, [])
    agent = str(entry.get("agent", ""))
    keywords = _agent_keywords(agent)

    # Exact match: find trial dir whose job parent name contains agent keyword
    for trial_dir in choices:
        path_lower = str(trial_dir).lower()
        for kw in keywords:
            if kw in path_lower:
                trajectory_path = trial_dir / "agent" / "trajectory.json"
                if trajectory_path.exists():
                    return trajectory_path, trial_dir
                break

    # Fallback: Stage 1 audit case dir
    fallback = Path(entry["case_dir"]) / "trajectory.json"
    return (fallback if fallback.exists() else None), None


def _candidate_eval_roots(pattern: str, agent: str, dataset: str) -> list[Path]:
    if not pattern:
        return []
    dataset_variants = [dataset, _safe_name(dataset)]
    # Use agent keywords for eval root pattern expansion (e.g. "sonnet", "opus", "kimi", "ds", "codex")
    agent_variants = [agent, _safe_name(agent)] + _agent_keywords(agent)
    roots: list[Path] = []
    seen: set[Path] = set()
    for agent_variant in agent_variants:
        for dataset_variant in dataset_variants:
            rendered = pattern.replace("{agent}", agent_variant).replace("{dataset}", dataset_variant)
            path = Path(rendered).expanduser()
            if path not in seen:
                seen.add(path)
                roots.append(path)
    return roots


def _resolve_eval_artifact(
    entry: dict[str, Any],
    *,
    eval_root_pattern: str,
    org: str,
    repo: str,
    filename: str,
) -> tuple[Path | None, Path | None]:
    pr_dir_name = f"pr-{int(entry['number'])}"
    for eval_root in _candidate_eval_roots(str(eval_root_pattern), str(entry["agent"]), str(entry["dataset"])):
        candidates = [
            eval_root / org / repo / "evals" / pr_dir_name / filename,
            eval_root / "workdir" / org / repo / "evals" / pr_dir_name / filename,
        ]
        for candidate in candidates:
            if candidate.exists():
                return candidate, eval_root

    fallback = Path(entry["case_dir"]) / filename
    return (fallback if fallback.exists() else None), None


def _extract_modified_files(diff_text: str) -> list[str]:
    files: list[str] = []
    seen: set[str] = set()

    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            parts = line.split()
            if len(parts) >= 4:
                path = parts[3]
                if path.startswith("b/"):
                    path = path[2:]
                if path != "/dev/null" and path not in seen:
                    seen.add(path)
                    files.append(path)

    if files:
        return files

    for line in diff_text.splitlines():
        if line.startswith("+++ "):
            path = line[4:].strip()
            if path.startswith("b/"):
                path = path[2:]
            if path != "/dev/null" and path not in seen:
                seen.add(path)
                files.append(path)

    return files


def _classify_file(path_str: str) -> str:
    lowered = path_str.lower()
    suffix = Path(lowered).suffix

    dv_markers = [
        "/dv/",
        "/tb/",
        "/test/",
        "/tests/",
        "scoreboard",
        "vseq",
        "ral",
        "monitor",
        "sequencer",
        "dvsim",
        "uvm",
    ]
    if any(marker in lowered for marker in dv_markers):
        return "dv"

    rtl_suffixes = {".sv", ".svh", ".v", ".vh", ".vhd", ".vhdl"}
    if suffix in rtl_suffixes or "/rtl/" in lowered:
        return "rtl"

    build_markers = ["/ci/", "/scripts/", "/util/", "/tools/"]
    build_suffixes = {".sh", ".py", ".mk", ".bazel", ".bzl", ".cfg", ".json", ".toml", ".yml", ".yaml"}
    if suffix in build_suffixes or "makefile" in lowered or any(marker in lowered for marker in build_markers):
        return "build"

    docs_suffixes = {".md", ".rst", ".txt", ".adoc"}
    if suffix in docs_suffixes or "/doc/" in lowered or "/docs/" in lowered:
        return "docs"

    return "other"


def _patch_summary(patch_text: str) -> dict[str, Any]:
    files_modified = _extract_modified_files(patch_text)
    buckets = Counter(_classify_file(path) for path in files_modified)
    return {
        "files_modified": files_modified,
        "file_buckets": dict(buckets),
    }


def _case_record_from_dataset(row: dict[str, Any]) -> dict[str, Any]:
    base = row.get("base") or {}
    record = {
        "org": row.get("org", ""),
        "repo": row.get("repo", ""),
        "number": int(row.get("number", 0)),
        "state": row.get("state", "") or "",
        "title": row.get("title", "") or "",
        "body": row.get("body", "") or "",
        "base": {
            "label": base.get("label", "") if isinstance(base, dict) else "",
            "ref": base.get("ref", "") if isinstance(base, dict) else "",
            "sha": base.get("sha", "") if isinstance(base, dict) else "",
        },
        "resolved_issues": row.get("resolved_issues") or [],
        "fix_patch": str(row.get("fix_patch", "") or ""),
        "test_patch": str(row.get("test_patch", "") or ""),
        "prepare_script": str(row.get("prepare_script", "") or ""),
        "tb_script": str(row.get("tb_script", "") or ""),
        "problem_statement": str(row.get("problem_statement", "") or ""),
        "tag": row.get("tag", "") or "",
        "number_interval": row.get("number_interval", "") or "",
        "lang": row.get("lang", "") or "",
    }
    return record


def _assign_agent_keys(entries: list[dict[str, Any]]) -> dict[int, str]:
    counts = Counter(str(entry["agent"]) for entry in entries)
    assigned: dict[int, str] = {}
    used: set[str] = set()

    for entry in sorted(entries, key=lambda item: (str(item["agent"]), str(item["dataset"]), str(item["run_id"]))):
        if counts[str(entry["agent"])] == 1:
            base = _safe_name(str(entry["agent"]))
        else:
            base = _safe_name(str(entry["run_id"]))

        key = base
        suffix = 2
        while key in used:
            key = f"{base}-{suffix}"
            suffix += 1
        used.add(key)
        assigned[id(entry)] = key

    return assigned


def _report_valid_from_path(report_path: Path | None) -> bool | None:
    if report_path is None or not report_path.exists():
        return None
    try:
        report = _read_json(report_path)
    except Exception:
        return None
    valid = report.get("valid")
    return bool(valid) if isinstance(valid, bool) else None


def _build_bundle_for_case(
    *,
    bundle_root: Path,
    number: int,
    org: str,
    repo: str,
    dataset_rows: dict[int, dict[str, Any]],
    entries: list[dict[str, Any]],
    patch_candidates: dict[int, list[dict[str, Any]]],
    trial_index: dict[int, list[Path]],
    eval_root_pattern: str,
) -> dict[str, Any] | None:
    case_dir = bundle_root / f"pr-{number}"
    case_dir.mkdir(parents=True, exist_ok=True)
    agents_root = case_dir / "agents"
    agents_root.mkdir(parents=True, exist_ok=True)

    dataset_row = dataset_rows.get(number)
    primary_case_dir = next((Path(entry["case_dir"]) for entry in entries if Path(entry["case_dir"]).exists()), None)
    if dataset_row is None:
        if case_dir.exists():
            shutil.rmtree(case_dir)
        return None

    case_record = _case_record_from_dataset(dataset_row)

    problem_statement = case_record.get("problem_statement", "") or _read_text_if_exists((primary_case_dir or case_dir) / "problem_statement.md")
    tb_script = case_record.get("tb_script", "") or _read_text_if_exists((primary_case_dir or case_dir) / "tb_script.sh")
    golden_patch = case_record.get("fix_patch", "") or _read_text_if_exists((primary_case_dir or case_dir) / "golden_patch.diff")

    _write_text(case_dir / "problem_statement.md", str(problem_statement))
    _write_text(case_dir / "tb_script.sh", str(tb_script))
    _write_text(case_dir / "golden_patch.diff", str(golden_patch))
    _write_text(case_dir / "prepare_script.sh", str(case_record.get("prepare_script", "")))
    _write_text(case_dir / "fix.patch", str(case_record.get("fix_patch", "")))
    _write_text(case_dir / "test.patch", str(case_record.get("test_patch", "")))
    _write_json(case_dir / "case.json", case_record)

    agent_keys = _assign_agent_keys(entries)
    coarse_agents: dict[str, Any] = {}
    matrix_agents: dict[str, Any] = {}
    flagged_by_agents: list[str] = []

    for entry in entries:
        agent_key = agent_keys[id(entry)]
        agent_dir = agents_root / agent_key
        agent_dir.mkdir(parents=True, exist_ok=True)

        patch_text, patch_source = _resolve_patch(entry, patch_candidates)
        patch_summary = _patch_summary(patch_text)
        _write_text(agent_dir / "patch.diff", patch_text)

        trajectory_path, trial_dir = _resolve_trajectory(entry, trial_index)
        _copy_if_exists(trajectory_path, agent_dir / "trajectory.json")

        report_path, eval_root = _resolve_eval_artifact(
            entry,
            eval_root_pattern=eval_root_pattern,
            org=org,
            repo=repo,
            filename="report.json",
        )
        _copy_if_exists(report_path, agent_dir / "report.json")

        fix_log_path, _ = _resolve_eval_artifact(
            entry,
            eval_root_pattern=eval_root_pattern,
            org=org,
            repo=repo,
            filename="fix-patch-run.log",
        )
        _copy_if_exists(fix_log_path, agent_dir / "fix-patch-run.log")

        verdict = str((entry["patch_review"] or {}).get("verdict", ""))
        if verdict and verdict not in SAFE_VERDICTS:
            flagged_by_agents.append(agent_key)

        artifacts = {
            "agent_dir": f"agents/{agent_key}",
            "patch": f"agents/{agent_key}/patch.diff",
            "trajectory": f"agents/{agent_key}/trajectory.json" if (agent_dir / "trajectory.json").exists() else None,
            "report": f"agents/{agent_key}/report.json" if (agent_dir / "report.json").exists() else None,
            "fix_log": f"agents/{agent_key}/fix-patch-run.log" if (agent_dir / "fix-patch-run.log").exists() else None,
        }

        coarse_agents[agent_key] = {
            "display_name": entry["agent"],
            "dataset": entry["dataset"],
            "run_id": entry["run_id"],
            "batch_id": entry["batch_id"],
            "status": entry["status"],
            "trajectory_audit": entry["trajectory_audit"],
            "patch_review": entry["patch_review"],
            "evidence": entry["evidence"],
            "artifacts": artifacts,
            "source_paths": {
                "audit_case_dir": str(Path(entry["case_dir"]).resolve()),
                "patch_source": patch_source,
                "trial_dir": str(trial_dir.resolve()) if trial_dir is not None else None,
                "trajectory_source": str(trajectory_path.resolve()) if trajectory_path is not None else None,
                "eval_root": str(eval_root.resolve()) if eval_root is not None else None,
                "report_source": str(report_path.resolve()) if report_path is not None else None,
                "fix_log_source": str(fix_log_path.resolve()) if fix_log_path is not None else None,
            },
        }

        matrix_agents[agent_key] = {
            "display_name": entry["agent"],
            "dataset": entry["dataset"],
            "run_id": entry["run_id"],
            "status": entry["status"],
            "patch_review_verdict": verdict,
            "patch_review_confidence": (entry["patch_review"] or {}).get("confidence"),
            "trajectory_verdict": (entry["trajectory_audit"] or {}).get("verdict"),
            "report_valid": _report_valid_from_path(report_path),
            "has_patch": bool(patch_text.strip()),
            "has_trajectory": trajectory_path is not None and trajectory_path.exists(),
            "has_report": report_path is not None and report_path.exists(),
            "has_fix_log": fix_log_path is not None and fix_log_path.exists(),
            **patch_summary,
        }

    verdict_counts = Counter(
        str((entry["patch_review"] or {}).get("verdict", "missing"))
        for entry in entries
    )
    status_counts = Counter(str(entry.get("status", "missing")) for entry in entries)
    trajectory_counts = Counter(
        str((entry.get("trajectory_audit") or {}).get("verdict", "missing"))
        for entry in entries
    )

    passed_agents = [
        agent_key
        for agent_key, summary in matrix_agents.items()
        if summary.get("status") == "resolved"
    ]
    failed_agents = [
        agent_key
        for agent_key, summary in matrix_agents.items()
        if summary.get("status") != "resolved"
    ]

    coarse_audit = {
        "schema_version": "audit.bundle.coarse.v1",
        "org": org,
        "repo": repo,
        "number": number,
        "summary": {
            "agent_count": len(entries),
            "flagged_by_agents": flagged_by_agents,
            "verdict_counts": dict(verdict_counts),
            "status_counts": dict(status_counts),
            "trajectory_verdict_counts": dict(trajectory_counts),
        },
        "agents": coarse_agents,
    }

    multi_agent_matrix = {
        "schema_version": "audit.multi_agent_matrix.v1",
        "org": org,
        "repo": repo,
        "number": number,
        "summary": {
            "agent_count": len(entries),
            "flagged_by_agents": flagged_by_agents,
            "passed_agents": passed_agents,
            "failed_agents": failed_agents,
            "verdict_counts": dict(verdict_counts),
            "status_counts": dict(status_counts),
        },
        "agents": matrix_agents,
    }

    docker_image = f"hwebench/{org.lower()}_m_{repo.lower()}:pr-{number}"
    case_manifest = {
        "schema_version": "audit.case_bundle.v1",
        "org": org,
        "repo": repo,
        "number": number,
        "bundle_dir": str(case_dir.resolve()),
        "docker_image": docker_image,
        "flagged": True,
        "flagged_by_agents": flagged_by_agents,
        "case_record": case_record,
        "paths": {
            "problem_statement": "problem_statement.md",
            "tb_script": "tb_script.sh",
            "golden_patch": "golden_patch.diff",
            "prepare_script": "prepare_script.sh",
            "fix_patch": "fix.patch",
            "test_patch": "test.patch",
            "case_json": "case.json",
            "coarse_audit": "coarse_audit.json",
            "multi_agent_matrix": "multi_agent_matrix.json",
        },
        "agents": {
            agent_key: {
                "display_name": coarse_agents[agent_key]["display_name"],
                "dataset": coarse_agents[agent_key]["dataset"],
                "run_id": coarse_agents[agent_key]["run_id"],
                "status": coarse_agents[agent_key]["status"],
                "patch_review_verdict": (coarse_agents[agent_key]["patch_review"] or {}).get("verdict"),
                "paths": coarse_agents[agent_key]["artifacts"],
            }
            for agent_key in sorted(coarse_agents)
        },
    }

    _write_json(case_dir / "coarse_audit.json", coarse_audit)
    _write_json(case_dir / "multi_agent_matrix.json", multi_agent_matrix)
    _write_json(case_dir / "case_manifest.json", case_manifest)

    return {
        "number": number,
        "bundle_dir": str(case_dir.resolve()),
        "flagged_by_agents": flagged_by_agents,
        "verdict_counts": dict(verdict_counts),
        "agent_count": len(entries),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Bundle flagged multi-agent audit cases for detailed investigation.")
    parser.add_argument("--audit-root", required=True, help="Stage 1 audit root, e.g. .../artifacts/audit/org__repo")
    parser.add_argument("--dataset", required=True, help="Dataset JSONL with problem_statement/tb_script/fix_patch")
    parser.add_argument("--patches-dir", required=True, help="Directory containing per-agent patches.jsonl files")
    parser.add_argument("--jobs-dir", required=True, help="Directory containing Harbor jobs/trials")
    parser.add_argument("--eval-root-pattern", required=True, help="Eval root pattern, e.g. /tmp/hwe_eval_ot_{agent}_{dataset}/")
    parser.add_argument("--org", required=True, help="GitHub org")
    parser.add_argument("--repo", required=True, help="GitHub repo")
    parser.add_argument("--dataset-name", default="", help="Filter audit runs by dataset name (from run_meta.json), e.g. expansion-97")
    parser.add_argument("--output", required=True, help="Output root directory; org__repo is appended automatically")
    args = parser.parse_args()

    audit_root = Path(args.audit_root).resolve()
    dataset_path = Path(args.dataset).resolve()
    patches_dir = Path(args.patches_dir).resolve()
    jobs_dir = Path(args.jobs_dir).resolve()
    bundle_root = Path(args.output).resolve() / f"{args.org}__{args.repo}"
    bundle_root.mkdir(parents=True, exist_ok=True)

    dataset_name = args.dataset_name.strip() or None
    dataset_rows = _load_dataset_rows(dataset_path, args.org, args.repo)
    entries_by_number, flagged_numbers = _collect_audit_entries(
        audit_root,
        org=args.org,
        repo=args.repo,
        dataset_name=dataset_name,
    )
    patch_candidates = _load_patch_candidates(
        patches_dir,
        org=args.org,
        repo=args.repo,
    )
    trial_index = _load_trial_index(
        jobs_dir,
        org=args.org,
        repo=args.repo,
    )

    bundle_rows: list[dict[str, Any]] = []
    missing_dataset_numbers: list[int] = []
    for number in sorted(flagged_numbers):
        entries = entries_by_number.get(number, [])
        if not entries:
            continue
        bundle_row = _build_bundle_for_case(
                bundle_root=bundle_root,
                number=number,
                org=args.org,
                repo=args.repo,
                dataset_rows=dataset_rows,
                entries=entries,
                patch_candidates=patch_candidates,
                trial_index=trial_index,
                eval_root_pattern=args.eval_root_pattern,
            )
        if bundle_row is None:
            missing_dataset_numbers.append(number)
            print(f"[WARN] Missing dataset row for PR #{number}; skipping bundle")
            continue
        bundle_rows.append(bundle_row)

    summary = {
        "schema_version": "audit.bundle.index.v1",
        "org": args.org,
        "repo": args.repo,
        "audit_root": str(audit_root),
        "bundle_root": str(bundle_root),
        "flagged_case_count": len(bundle_rows),
        "missing_dataset_numbers": missing_dataset_numbers,
        "cases": bundle_rows,
    }
    _write_json(bundle_root / "flagged_cases.json", summary)
    _write_jsonl(bundle_root / "flagged_cases.jsonl", bundle_rows)
    _normalize_bundle_metadata(bundle_root)

    print(
        "\n".join(
            [
                "[SUMMARY]",
                f"audit_root={audit_root}",
                f"bundle_root={bundle_root}",
                f"flagged_case_count={len(bundle_rows)}",
                f"missing_dataset_count={len(missing_dataset_numbers)}",
                f"flagged_index={bundle_root / 'flagged_cases.json'}",
            ]
        )
    )


if __name__ == "__main__":
    main()
