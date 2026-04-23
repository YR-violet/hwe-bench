# Copyright (c) 2024 Bytedance Ltd. and/or its affiliates

#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at

#      http://www.apache.org/licenses/LICENSE-2.0

#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.

"""Build per-PR Docker images or run f2p validation for benchmark cases."""

import concurrent.futures
import json
import glob
import logging
import sys
import tempfile
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Callable, Dict, Literal, Optional

from tqdm import tqdm

from hwe_bench.harness.base import (
    BUILD_DATASET_LOG_FILE,
    BUILD_IMAGE_LOG_FILE,
    BUILD_IMAGE_WORKDIR,
    FIX_PATCH_RUN_LOG_FILE,
    INSTANCE_WORKDIR,
    REPORT_FILE,
    RUN_LOG_FILE,
    TEST_PATCH_RUN_LOG_FILE,
    Config,
    Image,
    Instance,
    PullRequest,
)
from hwe_bench.harness.reporting import generate_report
from hwe_bench.utils import docker_util
from hwe_bench.utils.args_util import ArgumentParser
from hwe_bench.utils.logger import get_non_propagate_logger, setup_logger


def get_parser() -> ArgumentParser:
    parser = ArgumentParser(
        description="Build per-PR Docker images or run f2p case validation."
    )
    parser.add_argument(
        "--mode",
        type=str,
        choices=["instance", "image"],
        required=False,
        default="instance",
        help="The mode to run the script in.",
    )
    parser.add_argument(
        "--workdir",
        type=Path,
        required=False,
        help="The path to the workdir.",
    )
    parser.add_argument(
        "--raw_dataset_files",
        type=str,
        nargs="*",
        required=False,
        help="The paths to the raw dataset files. Supports glob patterns.",
    )
    parser.add_argument(
        "--force_build",
        type=parser.bool,
        required=False,
        default=False,
        help="Whether to force build the images.",
    )
    parser.add_argument(
        "--global_env",
        type=str,
        nargs="*",
        required=False,
        help="The global environment variables.",
    )
    parser.add_argument(
        "--clear_env",
        type=parser.bool,
        required=False,
        default=True,
        help="Whether to clear the environment variables.",
    )
    parser.add_argument(
        "--stop_on_error",
        type=parser.bool,
        required=False,
        default=True,
        help="Whether to stop on error.",
    )
    parser.add_argument(
        "--max_workers_build_image",
        type=int,
        required=False,
        default=8,
        help="The maximum number of workers to use for building the image.",
    )
    parser.add_argument(
        "--max_workers_run_instance",
        type=int,
        required=False,
        default=8,
        help="The maximum number of workers to use for running the instance.",
    )
    parser.add_argument(
        "--run_cmd",
        type=str,
        required=False,
        default="",
        help="The command to run the image.",
    )
    parser.add_argument(
        "--test_patch_run_cmd",
        type=str,
        required=False,
        default="",
        help="The command to run the test patch.",
    )
    parser.add_argument(
        "--fix_patch_run_cmd",
        type=str,
        required=False,
        default="",
        help="The command to run the fix patch.",
    )
    parser.add_argument(
        "--log_dir",
        type=Path,
        required=False,
        default=None,
        help="The path to the log directory.",
    )
    parser.add_argument(
        "--log_level",
        type=str,
        required=False,
        default="INFO",
        help="The log level to use.",
    )
    parser.add_argument(
        "--log_to_console",
        type=parser.bool,
        required=False,
        default=True,
        help="Whether to log to the console.",
    )
    return parser


def list_to_dict(env: Optional[list[str]]) -> Optional[dict[str, str]]:
    if env is None or len(env) == 0:
        return None

    result = {}
    for item in env:
        key_value = item.split("=")
        if len(key_value) == 2:
            key, value = key_value
            result[key] = value

    return result


def build_image(
    image: Image,
    *,
    workdir: Path,
    force_build: bool,
    log_level: str,
    logger: logging.Logger,
):
    image_workdir = workdir / image.pr.org / image.pr.repo / BUILD_IMAGE_WORKDIR
    image_dir = image_workdir / image.workdir()
    image_dir.mkdir(parents=True, exist_ok=True)

    dockerfile_path = image_dir / image.dockerfile_name()
    dockerfile_path.parent.mkdir(parents=True, exist_ok=True)
    with open(dockerfile_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(image.dockerfile())

    for file in image.files():
        file_path = image_dir / file.dir / file.name
        file_path.parent.mkdir(parents=True, exist_ok=True)
        with open(file_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(file.content)

    if not force_build and docker_util.exists(image.image_full_name()):
        logger.debug(f"Image {image.image_full_name()} already exists, skipping...")
        return

    logger.info(f"Building image {image.image_full_name()}...")
    docker_util.build(
        image_dir,
        image.dockerfile_name(),
        image.image_full_name(),
        get_non_propagate_logger(
            image_dir,
            BUILD_IMAGE_LOG_FILE,
            log_level,
            False,
        ),
    )
    logger.info(f"Image {image.image_full_name()} built successfully.")


def build_images_for_instances(
    instances: list[Instance],
    *,
    workdir: Path,
    force_build: bool,
    max_workers_build_image: int,
    stop_on_error: bool,
    log_level: str,
    logger: logging.Logger,
):
    logger.info("Building images...")

    external_images: set[str] = set()
    images: dict[str, set[Image]] = {}
    for instance in instances:
        required_image = instance.dependency()
        while isinstance(required_image, Image):
            parent_image = required_image.dependency()

            if isinstance(parent_image, Image):
                parent_image_name = parent_image.image_full_name()
            else:
                parent_image_name = parent_image
                external_images.add(parent_image_name)

            if parent_image_name not in images:
                images[parent_image_name] = set()
            images[parent_image_name].add(required_image)

            required_image = parent_image

    image_count = sum(len(child_images) for child_images in images.values())
    logger.info(f"Total images: {image_count}")

    building_images: set[Image] = set()
    for external_name in external_images:
        for image in images[external_name]:
            building_images.add(image)

    with tqdm(total=image_count, desc="Building images") as building_bar:
        while building_images:
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=max_workers_build_image
            ) as executor:
                futures = {
                    executor.submit(
                        build_image,
                        image,
                        workdir=workdir,
                        force_build=force_build,
                        log_level=log_level,
                        logger=logger,
                    ): image
                    for image in building_images
                }

                failed_images: set[Image] = set()
                for future in concurrent.futures.as_completed(futures):
                    image = futures[future]
                    try:
                        future.result()
                    except Exception as e:
                        logger.error(
                            f"Error building image {image.image_full_name()}: {e}"
                        )
                        failed_images.add(image)
                        if stop_on_error:
                            executor.shutdown(wait=False)
                            sys.exit(1)
                    finally:
                        building_bar.update(1)

            new_building_images: set[Image] = set()
            for image in building_images:
                if image in failed_images:
                    continue
                if image.image_full_name() not in images:
                    continue
                for new_image in images[image.image_full_name()]:
                    new_building_images.add(new_image)
            building_images = new_building_images

    logger.info("Images built successfully.")


def run_instances(
    instances: list[Instance],
    *,
    run_instance_fn: Callable[[Instance], None],
    max_workers_run_instance: int,
    stop_on_error: bool,
    logger: logging.Logger,
):
    logger.info("Running instances...")

    with tqdm(total=len(instances), desc="Running instances") as running_bar:
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=max_workers_run_instance
        ) as executor:
            futures = {
                executor.submit(run_instance_fn, instance): instance
                for instance in instances
            }

            for future in concurrent.futures.as_completed(futures):
                instance = futures[future]
                try:
                    future.result()
                except Exception as e:
                    logger.error(f"Error running instance {instance.pr.id}: {e}")
                    if stop_on_error:
                        executor.shutdown(wait=False)
                        sys.exit(1)
                finally:
                    running_bar.update(1)

    logger.info("Instances run successfully.")


def rebuild_images(input_path: Path, only: set[int] | None = None) -> None:
    input_path = input_path.resolve()

    records = []
    with input_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                record = json.loads(line)
                if only and record.get("number") not in only:
                    continue
                records.append(record)

    print(
        f"Loaded {len(records)} records from {input_path}"
        + (f" (filtered to PRs: {sorted(only)})" if only else "")
    )

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".jsonl", delete=False, encoding="utf-8"
    ) as tmp:
        for record in records:
            tmp.write(json.dumps(record, ensure_ascii=False) + "\n")
        tmp_path = tmp.name

    workdir = Path(tempfile.mkdtemp(prefix="rebuild_hwe_"))
    logs_dir = workdir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)

    print(f"Temp workdir: {workdir}")
    print(f"Temp JSONL: {tmp_path}")
    print("Force build: True (rebuilding all images)")

    cli = CliArgs.from_dict(
        {
            "mode": "image",
            "workdir": workdir,
            "raw_dataset_files": [tmp_path],
            "force_build": True,
            "global_env": None,
            "clear_env": True,
            "stop_on_error": False,
            "max_workers_build_image": 4,
            "max_workers_run_instance": 1,
            "run_cmd": "",
            "test_patch_run_cmd": "",
            "fix_patch_run_cmd": "",
            "log_dir": str(logs_dir),
            "log_level": "INFO",
            "log_to_console": True,
        }
    )
    cli.run_mode_image()


@dataclass
class CliArgs:
    mode: Literal["instance", "image"]
    workdir: Path
    raw_dataset_files: Optional[list[str]]
    force_build: bool
    global_env: Optional[list[str]]
    clear_env: bool
    stop_on_error: bool
    max_workers_build_image: int
    max_workers_run_instance: int
    run_cmd: str
    test_patch_run_cmd: str
    fix_patch_run_cmd: str
    log_dir: Path
    log_level: str
    log_to_console: bool

    def __post_init__(self):
        self._check_mode()
        self._check_workdir()
        self._check_raw_dataset_files()
        self._check_log_dir()
        self._check_log_level()
        self._check_log_to_console()
        self._check_max_workers()

    def _check_mode(self):
        valid_modes = ["instance", "image"]
        if self.mode not in valid_modes:
            raise ValueError(f"Invalid mode: {self.mode}, expected: {valid_modes}")

    def _check_workdir(self):
        if not self.workdir:
            raise ValueError(f"Invalid workdir: {self.workdir}")
        if isinstance(self.workdir, str):
            self.workdir = Path(self.workdir)
        if not isinstance(self.workdir, Path):
            raise ValueError(f"Invalid workdir: {self.workdir}")
        if not self.workdir.exists():
            raise ValueError(f"Workdir not found: {self.workdir}")

    def _check_raw_dataset_files(self):
        if not self.raw_dataset_files:
            raise ValueError(f"Invalid raw_dataset_files: {self.raw_dataset_files}")

        self._expanded_files: list[Path] = []
        for file_pattern in self.raw_dataset_files:
            matched_files = glob.glob(file_pattern)
            if not matched_files:
                raise ValueError(f"No files found matching pattern: {file_pattern}")
            self._expanded_files.extend([Path(f) for f in matched_files])

        if not self._expanded_files:
            raise ValueError("No raw dataset files found after expanding patterns")

        for file_path in self._expanded_files:
            if not file_path.exists():
                raise ValueError(f"Raw dataset file not found: {file_path}")

    def _check_log_dir(self):
        if not self.log_dir:
            raise ValueError(f"Invalid log_dir: {self.log_dir}")
        if isinstance(self.log_dir, str):
            self.log_dir = Path(self.log_dir)
        if not isinstance(self.log_dir, Path):
            raise ValueError(f"Invalid log_dir: {self.log_dir}")
        if not self.log_dir.exists():
            self.log_dir.mkdir(parents=True, exist_ok=True)

    def _check_log_level(self):
        self.log_level = self.log_level.upper()
        if self.log_level not in ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]:
            raise ValueError(f"Invalid log_level: {self.log_level}")

    def _check_log_to_console(self):
        if not isinstance(self.log_to_console, bool):
            raise ValueError(f"Invalid log_to_console: {self.log_to_console}")

    def _check_max_workers(self):
        if self.max_workers_build_image <= 0:
            raise ValueError(
                f"Invalid max_workers_build_image: {self.max_workers_build_image}"
            )
        if self.max_workers_run_instance <= 0:
            raise ValueError(
                f"Invalid max_workers_run_instance: {self.max_workers_run_instance}"
            )

    @property
    def logger(self) -> logging.Logger:
        if not hasattr(self, "_logger"):
            self._logger = setup_logger(
                self.log_dir,
                BUILD_DATASET_LOG_FILE,
                self.log_level,
                self.log_to_console,
            )
            self._logger.info("Initialize logger successfully.")
        return self._logger

    @property
    def raw_dataset(self) -> Dict[str, PullRequest]:
        if not self.raw_dataset_files:
            raise ValueError(f"Invalid raw_dataset_files: {self.raw_dataset_files}")

        if not hasattr(self, "_raw_dataset"):
            self.logger.info("Loading raw dataset...")
            self._raw_dataset: dict[str, PullRequest] = {}

            for file_path in self._expanded_files:
                with open(file_path, "r", encoding="utf-8") as f:
                    for line in f:
                        if line.strip() == "":
                            continue

                        pr = PullRequest.from_json(line)
                        self._raw_dataset[pr.id] = pr

            self.logger.info(
                f"Successfully loaded {len(self._raw_dataset)} valid pull requests from {self.raw_dataset_files}"
            )

        return self._raw_dataset

    @property
    def instances(self) -> list[Instance]:
        if not hasattr(self, "_instances"):
            self.logger.info("Creating instances...")
            self._instances: list[Instance] = []
            config = Config(
                global_env=list_to_dict(self.global_env),
                clear_env=self.clear_env,
            )

            for pr in self.raw_dataset.values():
                try:
                    instance = Instance.create(pr, config)
                    self._instances.append(instance)
                except Exception as e:
                    self.logger.error(f"Error creating instance for {pr.id}: {e}")

            self.logger.info(
                f"Successfully loaded {len(self._instances)} valid instances."
            )

        return self._instances

    @classmethod
    def from_dict(cls, d: dict) -> "CliArgs":
        valid_fields = {field.name for field in fields(cls)}
        return cls(**{key: value for key, value in d.items() if key in valid_fields})

    def build_image(self, image: Image):
        build_image(
            image,
            workdir=self.workdir,
            force_build=self.force_build,
            log_level=self.log_level,
            logger=self.logger,
        )

    def run_mode_image(self):
        build_images_for_instances(
            self.instances,
            workdir=self.workdir,
            force_build=self.force_build,
            max_workers_build_image=self.max_workers_build_image,
            stop_on_error=self.stop_on_error,
            log_level=self.log_level,
            logger=self.logger,
        )

    def run_instance(self, instance: Instance):
        instance_dir = (
            self.workdir
            / instance.pr.org
            / instance.pr.repo
            / INSTANCE_WORKDIR
            / instance.dependency().workdir()
        )
        instance_dir.mkdir(parents=True, exist_ok=True)

        report_path = instance_dir / REPORT_FILE
        if report_path.exists():
            self.logger.info(
                f"Report already exists for {instance.name()}, skipping..."
            )
            return

        volumes = None
        if instance.runtime_files():
            runtime_dir = instance_dir / "runtime_mount"
            runtime_dir.mkdir(parents=True, exist_ok=True)

            def write_mount_file(name: str, content: str) -> Path:
                path = runtime_dir / name
                with open(path, "w", encoding="utf-8", newline="\n") as f:
                    f.write(content or "")
                return path

            mount_files: list[tuple[Path, str]] = []
            for file in instance.runtime_files():
                host_path = write_mount_file(file.name, file.content)
                mount_files.append((host_path, f"/home/{file.name}"))

            mount_files.append(
                (write_mount_file("fix.patch", instance.pr.fix_patch), "/home/fix.patch")
            )
            mount_files.append(
                (
                    write_mount_file("test.patch", instance.pr.test_patch),
                    "/home/test.patch",
                )
            )

            volumes = {
                str(host): {"bind": container, "mode": "ro"}
                for host, container in mount_files
            }

        def run_and_save_output(
            image_full_name: str, run_command: str, output_path: Path
        ) -> str:
            self.logger.info(
                f"Running {image_full_name} with command: {run_command}..."
            )
            output = docker_util.run(
                image_full_name,
                run_command,
                output_path,
                self.global_env,
                volumes=volumes,
                timeout_seconds=1200,
            )
            self.logger.info(
                f"Running {image_full_name} with command: {run_command}... done"
            )
            return output

        output_run = run_and_save_output(
            instance.name(),
            instance.run(self.run_cmd),
            instance_dir / RUN_LOG_FILE,
        )
        output_test = run_and_save_output(
            instance.name(),
            instance.test_patch_run(self.test_patch_run_cmd),
            instance_dir / TEST_PATCH_RUN_LOG_FILE,
        )
        output_fix = run_and_save_output(
            instance.name(),
            instance.fix_patch_run(self.fix_patch_run_cmd),
            instance_dir / FIX_PATCH_RUN_LOG_FILE,
        )

        self.logger.debug(f"Generating report for {instance.name()}...")
        report = generate_report(instance, output_run, output_test, output_fix)
        self.logger.debug(f"Report for {instance.name()} generated successfully.")

        self.logger.debug(f"Saving report for {instance.name()}...")
        with open(report_path, "w", encoding="utf-8") as f:
            f.write(report.json())
        self.logger.debug(f"Report for {instance.name()} saved successfully.")

    def run_instances(self):
        run_instances(
            self.instances,
            run_instance_fn=self.run_instance,
            max_workers_run_instance=self.max_workers_run_instance,
            stop_on_error=self.stop_on_error,
            logger=self.logger,
        )

    def run_mode_instance(self):
        self.run_mode_image()
        self.run_instances()

    def run(self):
        if self.mode == "image":
            self.run_mode_image()
        elif self.mode == "instance":
            self.run_mode_instance()
        else:
            raise ValueError(f"Invalid mode: {self.mode}")


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] in {"build-images", "rebuild-images"}:
        parser = ArgumentParser(
            description="Build per-PR Docker images from s11 eval-ready JSONL."
        )
        parser.add_argument("--input", required=True, help="s11 eval-ready JSONL")
        parser.add_argument(
            "--only",
            default="",
            help="Comma-separated PR numbers to build (default: all)",
        )
        args = parser.parse_args(args=sys.argv[2:])
        only = (
            {int(x.strip()) for x in args.only.split(",") if x.strip()}
            if args.only
            else None
        )
        rebuild_images(Path(args.input), only)
        return

    parser = get_parser()
    args = parser.parse_args()
    cli = CliArgs.from_dict(vars(args))
    cli.run()


if __name__ == "__main__":
    main()
