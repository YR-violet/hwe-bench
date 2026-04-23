#!/usr/bin/env python3
"""
RTL PR data collection pipeline (8 steps).

Steps 1-6 are invoked as direct Python function calls.
Step 7 (LLM filter) uses Codex CLI to classify PRs into
RTL_BUG_FIX, SW_BUG_FIX, or OTHER.
Step 8 (feasibility filter) uses Codex CLI to score benchmark feasibility.
"""

import argparse
import os
from pathlib import Path


def run_step(description: str, func, *args, **kwargs):
    """Run a pipeline step with logging."""
    print(f"\n{'='*60}")
    print(f"Running: {description}")
    print(f"{'='*60}")
    func(*args, **kwargs)
    print(f"Done: {description}")


def main():
    from hwe_bench.collect.s8_feasibility_filter import DEFAULT_MODEL as S8_DEFAULT_MODEL

    parser = argparse.ArgumentParser(description="RTL PR data collection pipeline")
    parser.add_argument("--org", type=str, required=True, help="GitHub organization")
    parser.add_argument("--repo", type=str, required=True, help="GitHub repository")
    parser.add_argument(
        "--out-dir",
        type=str,
        default="datasets/pipeline",
        help="Output directory relative to project root (default: datasets/pipeline)",
    )
    parser.add_argument("--tokens", type=str, nargs="*", default=None, help="GitHub API token(s) or path to token file")
    parser.add_argument("--start-from", type=int, default=1, choices=range(1, 9), help="Start from step N (1-8)")
    parser.add_argument("--skip-existing", action="store_true", help="Skip steps whose output files already exist")
    parser.add_argument("--skip-filter-patches", action="store_true", help="Skip step 6: patch size/Verilog filter")
    parser.add_argument("--skip-rtl-filter", action="store_true", help="Skip step 7: LLM RTL relevance filter")
    parser.add_argument("--skip-feasibility-filter", action="store_true", help="Skip step 8: benchmark feasibility filter")
    parser.add_argument("--use-llm", action="store_true", default=True, help="Use LLM for issue extraction in step 2 (default: True)")
    parser.add_argument("--no-llm", action="store_true", help="Disable LLM issue extraction, use regex only")
    parser.add_argument("--s7-workers", type=int, default=3, help="Step 7: concurrent Codex CLI processes (default: 3)")
    parser.add_argument("--s7-model", type=str, default="gpt-5.4", help="Step 7: Codex model (default: gpt-5.4)")
    parser.add_argument("--s8-workers", type=int, default=3, help="Step 8: concurrent Codex CLI processes (default: 3)")
    parser.add_argument("--s8-model", type=str, default=S8_DEFAULT_MODEL, help=f"Step 8: Codex model (default: {S8_DEFAULT_MODEL})")
    parser.add_argument("--max-files-changed", type=int, default=50, help="Step 6: max files changed (default: 50)")
    parser.add_argument("--max-rows-changed", type=int, default=2000, help="Step 6: max rows changed (default: 2000)")
    parser.add_argument(
        "--num-workers",
        type=int,
        default=min(32, os.cpu_count() + 4 if os.cpu_count() else 10),
        help="Number of concurrent workers",
    )

    args = parser.parse_args()
    from hwe_bench.collect.util import get_tokens

    tokens = get_tokens(args.tokens)

    out_dir = Path(args.out_dir) / args.org
    out_dir.mkdir(parents=True, exist_ok=True)

    base_name = f"{args.org}__{args.repo}"
    use_llm = args.use_llm and not args.no_llm

    print(f"\nCollecting data for {args.org}/{args.repo}")
    print(f"Output: {out_dir}")
    if args.start_from > 1:
        print(f"Starting from step {args.start_from}")

    # ── Step 1: Fetch all PRs ──────────────────────────────────
    prs_file = out_dir / f"{base_name}_s01_prs.jsonl"
    if args.start_from <= 1:
        if args.skip_existing and prs_file.exists():
            print(f"\nSkip step 1: {prs_file} exists")
        else:
            from hwe_bench.collect.s1_fetch_prs import main as s1_main
            run_step(
                "Step 1/8: Fetch all PRs",
                s1_main, tokens, out_dir, args.org, args.repo, args.num_workers,
            )

    # ── Step 2: Filter by resolved issues ──────────────────────
    filtered_prs_file = out_dir / f"{base_name}_s02_filtered_prs.jsonl"
    if args.start_from <= 2:
        if args.skip_existing and filtered_prs_file.exists():
            print(f"\nSkip step 2: {filtered_prs_file} exists")
        else:
            from hwe_bench.collect.s2_filter_by_issues import main as s2_main
            run_step(
                "Step 2/8: Filter PRs by resolved issues",
                s2_main, out_dir, prs_file, args.num_workers,
                use_llm=use_llm,
            )

    # ── Step 3: Fetch related issue details ────────────────────
    issues_file = out_dir / f"{base_name}_s03_issues.jsonl"
    if args.start_from <= 3:
        if args.skip_existing and issues_file.exists():
            print(f"\nSkip step 3: {issues_file} exists")
        else:
            from hwe_bench.collect.s3_fetch_issues import main as s3_main
            run_step(
                "Step 3/8: Fetch related issue details",
                s3_main, tokens, out_dir, filtered_prs_file, args.num_workers,
            )

    # ── Step 4: Merge PRs with issues ──────────────────────────
    merged_file = out_dir / f"{base_name}_s04_merged.jsonl"
    if args.start_from <= 4:
        if args.skip_existing and merged_file.exists():
            print(f"\nSkip step 4: {merged_file} exists")
        else:
            from hwe_bench.collect.s4_merge import main as s4_main
            run_step(
                "Step 4/8: Merge PRs with issue data",
                s4_main, out_dir, args.org, args.repo,
            )

    # ── Step 5: Extract and split patches ──────────────────────
    raw_dataset = out_dir / f"{base_name}_s05_raw_dataset.jsonl"
    if args.start_from <= 5:
        if args.skip_existing and raw_dataset.exists():
            print(f"\nSkip step 5: {raw_dataset} exists")
        else:
            from hwe_bench.collect.s5_extract_patches import main as s5_main
            run_step(
                "Step 5/8: Extract and split patches",
                s5_main, tokens, out_dir, merged_file, 300, 3, args.num_workers,
            )

    print(f"\n{'='*60}")
    print("Steps 1-5 complete.")

    # ── Step 6: Filter by patch size & Verilog content ─────────
    filtered_dataset = out_dir / f"{base_name}_s06_filtered_patches.jsonl"
    if args.start_from <= 6 and not args.skip_filter_patches:
        if not raw_dataset.exists():
            print(f"\nSkip step 6: {raw_dataset} not found")
        elif args.skip_existing and filtered_dataset.exists():
            print(f"\nSkip step 6: {filtered_dataset} exists")
        else:
            from hwe_bench.collect.s6_filter_by_patch import main as s6_main
            run_step(
                "Step 6/8: Filter by patch size & Verilog content",
                s6_main, raw_dataset, out_dir,
                args.max_files_changed, args.max_rows_changed, args.num_workers,
            )

    # ── Step 7: LLM RTL relevance filter (Codex CLI) ─────────
    rtl_filter_output = out_dir / f"{base_name}_s07_classified.jsonl"
    if args.start_from <= 7 and not args.skip_rtl_filter:
        # Prefer step 6 output if available, otherwise fall back to raw dataset
        rtl_input_file = filtered_dataset if filtered_dataset.exists() else raw_dataset

        if args.skip_existing and rtl_filter_output.exists():
            print(f"\nSkip step 7: {rtl_filter_output} exists")
        elif not rtl_input_file.exists():
            print(f"\nSkip step 7: no input file found")
        else:
            from hwe_bench.collect.s7_llm_filter import main as s7_main
            run_step(
                "Step 7/8: LLM RTL relevance filter (Codex CLI)",
                s7_main, rtl_input_file, rtl_filter_output,
                args.s7_workers, args.s7_model,
            )

    # ── Step 8: Benchmark feasibility filter (Codex CLI) ──────
    feasibility_output = out_dir / f"{base_name}_s08_scored.jsonl"
    if args.start_from <= 8 and not args.skip_feasibility_filter:
        if args.skip_existing and feasibility_output.exists():
            print(f"\nSkip step 8: {feasibility_output} exists")
        elif not raw_dataset.exists():
            print(f"\nSkip step 8: {raw_dataset} not found")
        elif not rtl_filter_output.exists():
            print(f"\nSkip step 8: {rtl_filter_output} not found")
        else:
            from hwe_bench.collect.s8_feasibility_filter import main as s8_main
            run_step(
                "Step 8/8: Benchmark feasibility filter (Codex CLI)",
                s8_main,
                raw_dataset, rtl_filter_output, feasibility_output,
                args.s8_workers, args.s8_model,
            )

    print(f"\n{'='*60}")
    print("All steps complete.")
    print(f"\nOutput files in: {out_dir}")
    print(f"  - s01 Raw PRs:          {base_name}_s01_prs.jsonl")
    print(f"  - s02 Filtered PRs:     {base_name}_s02_filtered_prs.jsonl")
    print(f"  - s03 Related issues:   {base_name}_s03_issues.jsonl")
    print(f"  - s04 Merged:           {base_name}_s04_merged.jsonl")
    print(f"  - s05 Raw dataset:      {base_name}_s05_raw_dataset.jsonl")
    if not args.skip_filter_patches:
        print(f"  - s06 Filtered patches: {base_name}_s06_filtered_patches.jsonl")
    if not args.skip_rtl_filter:
        print(f"  - s07 Classified:       {base_name}_s07_classified.jsonl")
    if not args.skip_feasibility_filter:
        print(f"  - s08 Scored:           {base_name}_s08_scored.jsonl")


if __name__ == "__main__":
    main()
