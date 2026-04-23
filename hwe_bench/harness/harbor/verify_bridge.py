from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def parse_instance_id(instance_id: str) -> dict[str, Any]:
    org_repo, pr_part = instance_id.split(":")
    org, repo = org_repo.split("/")
    if not pr_part.startswith("pr-"):
        raise ValueError(f"Invalid instance_id: {instance_id}")
    return {
        "org": org,
        "repo": repo,
        "number": int(pr_part[len("pr-") :]),
    }


def write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def collect_patches(harbor_job_dir: Path, output_dir: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    patches: list[dict[str, Any]] = []
    summary: list[dict[str, Any]] = []
    seen_instance_ids: set[str] = set()
    errors: list[str] = []

    for trial_dir in sorted(path for path in harbor_job_dir.iterdir() if path.is_dir()):
        result_file = trial_dir / "result.json"
        if not result_file.exists():
            continue

        result = json.loads(result_file.read_text(encoding="utf-8"))
        trial_name = str(result.get("trial_name", trial_dir.name))
        agent_result = result.get("agent_result") or {}

        detail_file = trial_dir / "verifier" / "detail.json"
        if not detail_file.exists():
            summary.append({"trial_name": trial_name, "error": "missing_detail_json"})
            errors.append(f"{trial_name}: missing detail.json")
            continue

        try:
            detail = json.loads(detail_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            summary.append({"trial_name": trial_name, "error": "invalid_detail_json"})
            errors.append(f"{trial_name}: invalid detail.json ({exc})")
            continue

        instance_id = str(detail.get("instance_id", "")).strip()
        if not instance_id:
            summary.append({"trial_name": trial_name, "error": "missing_instance_id"})
            errors.append(f"{trial_name}: missing instance_id in detail.json")
            continue

        if instance_id in seen_instance_ids:
            summary.append(
                {
                    "trial_name": trial_name,
                    "instance_id": instance_id,
                    "error": "duplicate_instance_id",
                }
            )
            errors.append(f"{trial_name}: duplicate instance_id {instance_id}")
            continue
        seen_instance_ids.add(instance_id)

        patch_file = trial_dir / "verifier" / "model_patch.diff"
        patch = patch_file.read_text(encoding="utf-8", errors="replace") if patch_file.exists() else ""
        meta = parse_instance_id(instance_id)

        summary.append(
            {
                "trial_name": trial_name,
                "instance_id": instance_id,
                "has_patch": bool(patch.strip()),
                "error": detail.get("error"),
                "patch_lines": detail.get("patch_lines"),
                "cost_usd": agent_result.get("cost_usd"),
                "input_tokens": agent_result.get("n_input_tokens"),
                "output_tokens": agent_result.get("n_output_tokens"),
            }
        )

        if patch.strip():
            patches.append(
                {
                    "org": meta["org"],
                    "repo": meta["repo"],
                    "number": meta["number"],
                    "fix_patch": patch,
                }
            )

    output_dir.mkdir(parents=True, exist_ok=True)
    write_json(output_dir / "collection_summary.json", summary)
    write_jsonl(output_dir / "patches.jsonl", patches)

    if errors:
        raise RuntimeError("verify_bridge encountered errors:\n" + "\n".join(errors))

    return patches, summary


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Collect Harbor verifier patches into HWE-bench patches.jsonl."
    )
    parser.add_argument("--harbor-job-dir", type=Path, required=True, help="Harbor job directory.")
    parser.add_argument("--output", type=Path, required=True, help="Output directory.")
    args = parser.parse_args()

    patches, summary = collect_patches(args.harbor_job_dir, args.output)
    print(f"Collected {len(patches)} non-empty patches from {len(summary)} trials")
    print(args.output / "collection_summary.json")
    print(args.output / "patches.jsonl")


if __name__ == "__main__":
    main()
