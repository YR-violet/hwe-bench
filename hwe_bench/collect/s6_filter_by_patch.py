# s6_filter_by_patch.py - Step 6: Filter PRs by patch size and Verilog content
import argparse
import json
import os
import sys
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from tqdm import tqdm
from unidiff import PatchSet


def get_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Filters PRs based on patch characteristics (size, Verilog content)."
    )
    parser.add_argument(
        "--raw_dataset_file",
        type=Path,
        required=True,
        help="Path to the raw dataset file (e.g., ..._raw_dataset.jsonl) containing fix_patch and test_patch.",
    )
    parser.add_argument(
        "--out_dir", type=Path, required=True, help="Output directory path."
    )
    parser.add_argument(
        "--max_files_changed",
        type=int,
        default=50,
        help="Maximum number of files allowed to be changed in a PR.",
    )
    parser.add_argument(
        "--max_rows_changed",
        type=int,
        default=2000,
        help="Maximum number of source code line changes (added + removed) in a PR.",
    )
    parser.add_argument(
        "--num_workers",
        type=int,
        default=min(32, os.cpu_count() + 4 if os.cpu_count() else 10),
        help="Number of worker threads for filtering.",
    )
    return parser

# --- Helper functions provided by user (with slight completion) ---
def get_modified_files(patch: str) -> list[str]:
    files = PatchSet(patch)
    file_list = []
    
    for file_obj in files:
        if file_obj.source_file != "/dev/null":
            path = file_obj.source_file
        else:
            path = file_obj.target_file
        
        if path.startswith("a/") or path.startswith("b/"):
            file_list.append(path[2:])
        else:
            file_list.append(path)

    return list(set(file_list))

def get_modified_row_count(patch: str) -> int:
    """
    Get the number of rows modified in a patch
    """
    files = PatchSet(patch)
    return sum(file_obj.added + file_obj.removed for file_obj in files)

def is_too_large_patch(patch: str, max_files: int = 20, max_rows: int = 1000) -> bool:
    """
    Check if a pull request is too large based on number of modified files and rows
    """
    try:
        modified_files = get_modified_files(patch)
        if len(modified_files) > max_files:
            return True
        
        modified_rows = get_modified_row_count(patch)
        if modified_rows > max_rows:
            return True
    except Exception as e:
        # print(f"Warning: Could not parse patch for size check: {e}")
        return True
    
    return False

# --- End of helper functions ---

def process_pr_for_patch_filters(
    pr_data: dict,
    max_files: int,
    max_rows: int
) -> dict | None:
    """
    Applies patch-based filters to a single PR.
    Returns the pr_data if it passes all filters, otherwise None.

    Note: language-level filtering (Verilog / SystemVerilog / Chisel) is
    intentionally deferred to Step 7's LLM classifier, so SW bug fixes
    without HDL file changes still flow through.
    """
    fix_patch = pr_data.get("fix_patch", "")
    test_patch = pr_data.get("test_patch", "")

    full_patch_str = fix_patch
    if fix_patch and test_patch:
        full_patch_str += "\n" + test_patch
    elif test_patch:
        full_patch_str = test_patch

    if not full_patch_str:
        print(f"PR #{pr_data.get('number', 'unknown')} has no patch content. Skipping.")
        return None

    if is_too_large_patch(full_patch_str, max_files, max_rows):
        return None

    return pr_data


def main(
    raw_dataset_file_path: Path,
    output_dir_path: Path,
    max_files_val: int,
    max_rows_val: int,
    num_workers_val: int,
):
    print("Starting patch-based filtering of PRs (Verilog/SV check is now default)...")
    print(f"Input raw dataset file: {raw_dataset_file_path}")
    print(f"Output directory: {output_dir_path}")
    print(f"Max files changed: {max_files_val}")
    print(f"Max rows changed: {max_rows_val}")
    print(f"Number of workers: {num_workers_val}")

    if not raw_dataset_file_path.exists():
        print(f"Error: Input file not found: {raw_dataset_file_path}")
        sys.exit(1)

    base_name = raw_dataset_file_path.name
    if "_s05_raw_dataset.jsonl" in base_name:
        output_base_name = base_name.replace("_s05_raw_dataset.jsonl", "_s06_filtered_patches.jsonl")
    elif "_raw_dataset.jsonl" in base_name:
        output_base_name = base_name.replace("_raw_dataset.jsonl", "_s06_filtered_patches.jsonl")
    else:
        output_base_name = f"{base_name.split('.')[0]}_s06_filtered_patches.jsonl"
    
    output_file_path = output_dir_path / output_base_name
    output_dir_path.mkdir(parents=True, exist_ok=True)


    print(f"Loading PRs from {raw_dataset_file_path}...")
    all_prs_from_input = []
    with open(raw_dataset_file_path, "r", encoding="utf-8") as infile:
        for line in infile:
            try:
                all_prs_from_input.append(json.loads(line))
            except json.JSONDecodeError:
                print(f"Warning: Skipping malformed JSON line in {raw_dataset_file_path}")
    print(f"Loaded {len(all_prs_from_input)} PRs.")

    if not all_prs_from_input:
        print("No PRs loaded from input file. Exiting.")
        with open(output_file_path, "w", encoding="utf-8") as outfile:
            pass
        return

    filtered_prs_output = []
    with ThreadPoolExecutor(max_workers=num_workers_val, thread_name_prefix='Patch-Filter') as executor:
        future_to_pr_num = {}

        for pr_data_item in tqdm(all_prs_from_input, desc="Submitting PRs for patch filtering"):
            future = executor.submit(
                process_pr_for_patch_filters,
                pr_data_item,
                max_files_val,
                max_rows_val,
            )
            future_to_pr_num[future] = pr_data_item.get("number", "unknown")

        for future in tqdm(as_completed(future_to_pr_num), total=len(all_prs_from_input), desc="Filtering PRs by patch"):
            pr_num_logging = future_to_pr_num[future]
            try:
                result_pr_data = future.result()
                if result_pr_data:
                    filtered_prs_output.append(result_pr_data)
            except Exception as exc:
                print(f'PR #{pr_num_logging} generated an unexpected exception during patch filtering: {exc}')
    
    print(f"Finished filtering. {len(filtered_prs_output)} PRs passed patch filters.")

    filtered_prs_output.sort(key=lambda x: x.get('number', 0), reverse=True)

    print(f"Writing {len(filtered_prs_output)} filtered PRs to {output_file_path}...")
    with open(output_file_path, "w", encoding="utf-8") as outfile:
        for pr_data in tqdm(filtered_prs_output, desc="Writing final dataset"):
            outfile.write(json.dumps(pr_data, ensure_ascii=False) + "\n")
            
    print(f"Successfully wrote final dataset to {output_file_path}.")


if __name__ == "__main__":
    parser = get_parser()
    args = parser.parse_args()
    main(
        args.raw_dataset_file,
        args.out_dir,
        args.max_files_changed,
        args.max_rows_changed,
        args.num_workers,
    )
