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

import concurrent.futures
import glob
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, Literal, Optional, Tuple, Union

from dataclasses_json import config, dataclass_json
from tqdm import tqdm

from hwe_bench.harness.base import (
    EVALUATION_WORKDIR,
    FINAL_REPORT_FILE,
    GENERATE_REPORT_LOG_FILE,
    INSTANCE_WORKDIR,
    FIX_PATCH_RUN_LOG_FILE,
    REPORT_FILE,
    RUN_LOG_FILE,
    TEST_PATCH_RUN_LOG_FILE,
    Base,
    Config,
    Instance,
    PullRequest,
    PullRequestBase,
    Test,
    TestResult,
    TestStatus,
)
from hwe_bench.utils.args_util import ArgumentParser
from hwe_bench.utils.logger import setup_logger


@dataclass_json
@dataclass
class Report(PullRequestBase):
    valid: Optional[bool] = None
    error_msg: Optional[str] = None
    fixed_tests: dict[str, Test] = field(default_factory=dict)
    p2p_tests: dict[str, Test] = field(default_factory=dict)
    f2p_tests: dict[str, Test] = field(default_factory=dict)
    s2p_tests: dict[str, Test] = field(default_factory=dict)
    n2p_tests: dict[str, Test] = field(default_factory=dict)
    run_result: TestResult = None
    test_patch_result: TestResult = None
    fix_patch_result: TestResult = None
    _tests: dict[str, Test] = field(
        default_factory=dict, metadata=config(exclude=lambda _: True)
    )

    def __post_init__(self):
        if not self.run_result:
            raise ValueError("Invalid run_result: None")
        if not self.test_patch_result:
            raise ValueError("Invalid test_patch_result: None")
        if not self.fix_patch_result:
            raise ValueError("Invalid fix_patch_result: None")

        all_tests = (
            self.run_result._tests.keys()
            | self.test_patch_result._tests.keys()
            | self.fix_patch_result._tests.keys()
        )

        for test_name in all_tests:
            run = self.run_result._tests.get(test_name, TestStatus.NONE)
            test = self.test_patch_result._tests.get(test_name, TestStatus.NONE)
            fix = self.fix_patch_result._tests.get(test_name, TestStatus.NONE)
            self._tests[test_name] = Test(run, test, fix)

        self.valid, self.error_msg = self.check()

    @classmethod
    def from_dict(cls, d: dict) -> "Report":
        data = cls(**d)
        data.__post_init__()
        return data

    @classmethod
    def from_json(cls, json_str: str) -> "Report":
        data = cls.from_dict(cls.schema().loads(json_str))
        data.__post_init__()
        return data

    def dict(self) -> dict:
        return asdict(self)

    def json(self) -> str:
        return self.to_json(ensure_ascii=False)

    def check(self, force: bool = False) -> Tuple[bool, str]:
        """Validate this report and populate tracking dicts.

        Semantics (all-tests-must-PASS, since 2026-04-20):
          valid=True ⇔
            (1) the fix phase produced at least one test result,
            (2) every declared test is PASS in the fix phase, and
            (3) at least one test was non-PASS in the test phase (the base
                was genuinely broken; a no-op patch does not count as valid).

        When valid=True, the tracking dicts (p2p_tests / f2p_tests /
        s2p_tests / n2p_tests / fixed_tests) are populated per standard
        semantics. When valid=False, tracking dicts remain empty — the
        early-return skips the populate loop. Raw per-test state is always
        preserved in `test_patch_result` and `fix_patch_result`; consult
        those fields (not the tracking dicts) for per-test audit data on
        invalid reports.
        """
        if not force and self.valid is not None:
            return (self.valid, self.error_msg)

        # Clear tracking dicts so repeated invocations are idempotent.
        self.fixed_tests.clear()
        self.p2p_tests.clear()
        self.f2p_tests.clear()
        self.s2p_tests.clear()
        self.n2p_tests.clear()

        # Rule 1: fix phase must report at least one test.
        if self.fix_patch_result.all_count == 0:
            self.valid = False
            self.error_msg = f"After applying the fix patch, no test results were captured when executing the test command. A brief summary is as follows: {self.short_report()}"
            return (self.valid, self.error_msg)

        # Rule 2: every reported test must PASS in the fix phase.
        # Subsumes the legacy "no regression" and "anomalous pattern" rules:
        # any fix != PASS (FAIL / SKIP / NONE) disqualifies the patch.
        # Collect every failing test (not just the first) so error_msg gives
        # the complete failure surface in one pass.
        failing = [
            (name, test)
            for name, test in self._tests.items()
            if test.fix != TestStatus.PASS
        ]
        if failing:
            self.valid = False
            details = "; ".join(f"`{n}`: {t}" for n, t in failing)
            self.error_msg = (
                f"{len(failing)} test(s) did not pass in the fix phase: "
                f"{details}. A brief summary is as follows: {self.short_report()}"
            )
            return (self.valid, self.error_msg)

        # Rule 3: at least one test must have been non-PASS in the test phase,
        # proving the base was genuinely broken. Otherwise an empty or no-op
        # patch on an already-passing base would be counted as valid.
        if not any(t.test != TestStatus.PASS for t in self._tests.values()):
            self.valid = False
            self.error_msg = f"No test was failing in the test phase; the base already passed without the fix patch. A brief summary is as follows: {self.short_report()}"
            return (self.valid, self.error_msg)

        # Populate tracking dicts for downstream consumers.
        for name, test in self._tests.items():
            if test.test == TestStatus.PASS:
                self.p2p_tests[name] = test
            elif test.test == TestStatus.FAIL:
                self.f2p_tests[name] = test
                self.fixed_tests[name] = test
            elif test.test == TestStatus.SKIP:
                self.s2p_tests[name] = test
                self.fixed_tests[name] = test
            elif test.test == TestStatus.NONE:
                self.n2p_tests[name] = test
                self.fixed_tests[name] = test

        self.valid = True
        self.error_msg = ""
        return (self.valid, self.error_msg)

    def short_report(self) -> str:
        return (
            "Test Result Summary:\n"
            "Stage Descriptions:\n"
            "  run  : Execute the test command without any patches applied.\n"
            "  test : Execute the test command after applying the test patch.\n"
            "  fix  : Execute the test command after applying both the test patch and the fix patch.\n"
            "Each stage is reported as (pass, fail, skip), representing the number of tests that passed, failed, or were skipped, respectively.\n\n"
            f"Results:\n"
            f"  run  = ({self.run_result.passed_count}, {self.run_result.failed_count}, {self.run_result.skipped_count})\n"
            f"  test = ({self.test_patch_result.passed_count}, {self.test_patch_result.failed_count}, {self.test_patch_result.skipped_count})\n"
            f"  fix  = ({self.fix_patch_result.passed_count}, {self.fix_patch_result.failed_count}, {self.fix_patch_result.skipped_count})"
        )


def generate_report(
    instance: Instance,
    run_result: Union[str, TestResult],
    test_patch_result: Union[str, TestResult],
    fix_patch_result: Union[str, TestResult],
) -> Report:
    if isinstance(run_result, str):
        run_result = instance.parse_log(run_result)
    if isinstance(test_patch_result, str):
        test_patch_result = instance.parse_log(test_patch_result)
    if isinstance(fix_patch_result, str):
        fix_patch_result = instance.parse_log(fix_patch_result)

    report = Report(
        org=instance.pr.org,
        repo=instance.pr.repo,
        number=instance.pr.number,
        run_result=run_result,
        test_patch_result=test_patch_result,
        fix_patch_result=fix_patch_result,
    )

    return report


@dataclass_json
@dataclass
class ReportTask(PullRequestBase):
    instance_dir: Path

    @property
    def instance(self) -> Instance:
        pr = PullRequest(
            org=self.org,
            repo=self.repo,
            number=self.number,
            state="",
            title="",
            body="",
            base=Base(label="", ref="", sha=""),
            resolved_issues=[],
            fix_patch="",
            test_patch="",
        )

        config = Config(
            global_env=None,
            clear_env=False,
        )

        return Instance.create(pr, config)

    @property
    def run_log(self) -> str:
        run_log_path = self.instance_dir / RUN_LOG_FILE
        if not run_log_path.exists():
            raise FileNotFoundError(f"Run log file not found: {run_log_path}")
        with open(run_log_path, "r", encoding="utf-8") as f:
            run_log = f.read()
        return run_log

    @property
    def test_patch_run_log(self) -> str:
        test_patch_run_log_path = self.instance_dir / TEST_PATCH_RUN_LOG_FILE
        if not test_patch_run_log_path.exists():
            raise FileNotFoundError(
                f"Test patch run log file not found: {test_patch_run_log_path}"
            )
        with open(test_patch_run_log_path, "r", encoding="utf-8") as f:
            test_patch_run_log = f.read()
        return test_patch_run_log

    @property
    def fix_patch_run_log(self) -> str:
        fix_patch_run_log_path = self.instance_dir / FIX_PATCH_RUN_LOG_FILE
        if not fix_patch_run_log_path.exists():
            raise FileNotFoundError(
                f"Fix patch run log file not found: {fix_patch_run_log_path}"
            )
        with open(fix_patch_run_log_path, "r", encoding="utf-8") as f:
            fix_patch_run_log = f.read()
        return fix_patch_run_log

    def generate_report(
        self,
        run_log: Optional[Union[str, TestResult]] = None,
        test_patch_run_log: Optional[Union[str, TestResult]] = None,
        fix_patch_run_log: Optional[Union[str, TestResult]] = None,
        regen: bool = True,
    ) -> Report:
        if not regen:
            report_path = self.instance_dir / REPORT_FILE
            if report_path.exists():
                with open(report_path, "r", encoding="utf-8") as f:
                    report = Report.from_json(f.read())
                return report

        report = generate_report(
            self.instance,
            run_log or self.run_log,
            test_patch_run_log or self.test_patch_run_log,
            fix_patch_run_log or self.fix_patch_run_log,
        )

        with open(self.instance_dir / REPORT_FILE, "w", encoding="utf-8") as f:
            f.write(report.json())

        return report


@dataclass_json
@dataclass
class FinalReport:
    total_instances: int
    submitted_instances: int
    completed_instances: int
    incomplete_instances: int
    resolved_instances: int
    unresolved_instances: int
    empty_patch_instances: int
    error_instances: int

    submitted_ids: list[str]
    completed_ids: list[str]
    incomplete_ids: list[str]
    resolved_ids: list[str]
    unresolved_ids: list[str]
    empty_patch_ids: list[str]
    error_ids: list[str]

    @classmethod
    def from_dict(cls, d: dict) -> "FinalReport":
        data = cls(**d)
        data.__post_init__()
        return data

    @classmethod
    def from_json(cls, json_str: str) -> "FinalReport":
        data = cls.from_dict(cls.schema().loads(json_str))
        data.__post_init__()
        return data

    def dict(self) -> dict:
        return asdict(self)

    def json(self) -> str:
        return self.to_json(ensure_ascii=False)

    @classmethod
    def from_reports(
        cls,
        reports: list[Report],
        invalid_reports: list[Report],
        failed_tasks: list[ReportTask] = [],
    ) -> "FinalReport":
        submitted_ids = (
            [report.id for report in reports]
            + [report.id for report in invalid_reports]
            + [task.id for task in failed_tasks]
        )
        completed_ids = [report.id for report in reports] + [
            report.id for report in invalid_reports
        ]
        incomplete_ids = [task.id for task in failed_tasks]
        resolved_ids = [report.id for report in reports]
        unresolved_ids = [report.id for report in invalid_reports]
        empty_patch_ids = []
        error_ids = [task.id for task in failed_tasks]

        final_report = FinalReport(
            total_instances=len(reports) + len(invalid_reports) + len(failed_tasks),
            submitted_instances=len(submitted_ids),
            completed_instances=len(completed_ids),
            incomplete_instances=len(incomplete_ids),
            resolved_instances=len(resolved_ids),
            unresolved_instances=len(unresolved_ids),
            empty_patch_instances=len(empty_patch_ids),
            error_instances=len(error_ids),
            submitted_ids=submitted_ids,
            completed_ids=completed_ids,
            incomplete_ids=incomplete_ids,
            resolved_ids=resolved_ids,
            unresolved_ids=unresolved_ids,
            empty_patch_ids=empty_patch_ids,
            error_ids=error_ids,
        )

        return final_report


@dataclass_json
@dataclass
class EvaluationRecord(PullRequest):
    fixed_tests: dict[str, Test] = field(default_factory=dict)
    p2p_tests: dict[str, Test] = field(default_factory=dict)
    f2p_tests: dict[str, Test] = field(default_factory=dict)
    s2p_tests: dict[str, Test] = field(default_factory=dict)
    n2p_tests: dict[str, Test] = field(default_factory=dict)
    run_result: TestResult = None
    test_patch_result: TestResult = None
    fix_patch_result: TestResult = None

    def __post_init__(self):
        super().__post_init__()

    @classmethod
    def from_dict(cls, d: dict) -> "EvaluationRecord":
        data = cls(**d)
        data.__post_init__()
        return data

    @classmethod
    def from_json(cls, json_str: str) -> "EvaluationRecord":
        data = cls.from_dict(cls.schema().loads(json_str))
        data.__post_init__()
        return data

    @classmethod
    def build(cls, pr: PullRequest, report: Report) -> "EvaluationRecord":
        return cls(
            org=pr.org,
            repo=pr.repo,
            number=pr.number,
            tag=pr.tag,
            number_interval=pr.number_interval,
            lang=pr.lang,
            state=pr.state,
            title=pr.title,
            body=pr.body,
            base=pr.base,
            resolved_issues=pr.resolved_issues,
            fix_patch=pr.fix_patch,
            test_patch=pr.test_patch,
            prepare_script=pr.prepare_script,
            tb_script=pr.tb_script,
            fixed_tests=report.fixed_tests,
            p2p_tests=report.p2p_tests,
            f2p_tests=report.f2p_tests,
            s2p_tests=report.s2p_tests,
            n2p_tests=report.n2p_tests,
            run_result=report.run_result,
            test_patch_result=report.test_patch_result,
            fix_patch_result=report.fix_patch_result,
        )


def get_parser() -> ArgumentParser:
    parser = ArgumentParser(
        description="Generate dataset and evaluation reports for HWE-bench."
    )
    parser.add_argument(
        "--mode",
        type=str,
        choices=["dataset", "evaluation", "summary", "regen"],
        required=False,
        default="dataset",
        help="The mode to run the script in.",
    )
    parser.add_argument(
        "--workdir",
        type=Path,
        required=False,
        help="The path to the workdir.",
    )
    parser.add_argument(
        "--output_dir",
        type=Path,
        required=False,
        help="The path to the output directory.",
    )
    parser.add_argument(
        "--specifics",
        type=str,
        nargs="*",
        required=False,
        help="The specific orgs to run the script on (can specify multiple).",
    )
    parser.add_argument(
        "--skips",
        type=str,
        nargs="*",
        required=False,
        help="The orgs to skip (can specify multiple).",
    )
    parser.add_argument(
        "--raw_dataset_files",
        type=str,
        nargs="*",
        required=False,
        help="The paths to the raw dataset files. Supports glob patterns.",
    )
    parser.add_argument(
        "--dataset_files",
        type=str,
        nargs="*",
        required=False,
        help="The paths to the dataset files. Supports glob patterns.",
    )
    parser.add_argument(
        "--max_workers",
        type=int,
        required=False,
        default=8,
        help="The maximum number of workers to use.",
    )
    parser.add_argument(
        "--log_dir",
        type=Path,
        required=False,
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
    parser.add_argument(
        "--regen",
        type=parser.bool,
        required=False,
        default=True,
        help="Whether to regenerate the reports.",
    )
    return parser


@dataclass_json
@dataclass
class CliArgs:
    mode: Literal["dataset", "evaluation", "summary", "regen"]
    workdir: Path
    output_dir: Optional[Path]
    specifics: Optional[set[str]]
    skips: Optional[set[str]]
    raw_dataset_files: Optional[list[str]]
    dataset_files: Optional[list[str]]
    max_workers: int
    log_dir: Path
    log_level: str
    log_to_console: bool
    regen: bool = True

    def __post_init__(self):
        self._check_mode()
        self._check_workdir()
        self._check_log_dir()
        self._check_log_level()
        self._check_log_to_console()

        if self.mode == "dataset":
            self._check_output_dir()
            self._check_raw_dataset_files()
        elif self.mode == "evaluation":
            self._check_output_dir()
            self._check_dataset_files()
        elif self.mode == "summary":
            self._check_output_dir()

    def _check_mode(self):
        valid_modes = {"dataset", "evaluation", "summary", "regen"}
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

    def _check_output_dir(self):
        if not self.output_dir:
            raise ValueError(f"Invalid output_dir: {self.output_dir}")
        if isinstance(self.output_dir, str):
            self.output_dir = Path(self.output_dir)
        if not isinstance(self.output_dir, Path):
            raise ValueError(f"Invalid output_dir: {self.output_dir}")
        if not self.output_dir.exists():
            self.output_dir.mkdir(parents=True, exist_ok=True)

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

    def _check_dataset_files(self):
        if not self.dataset_files:
            raise ValueError(f"Invalid dataset_files: {self.dataset_files}")

        self._expanded_files: list[Path] = []
        for file_pattern in self.dataset_files:
            matched_files = glob.glob(file_pattern)
            if not matched_files:
                raise ValueError(f"No files found matching pattern: {file_pattern}")
            self._expanded_files.extend([Path(f) for f in matched_files])

        if not self._expanded_files:
            raise ValueError("No dataset files found after expanding patterns")

        for file_path in self._expanded_files:
            if not file_path.exists():
                raise ValueError(f"Dataset file not found: {file_path}")

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

    @property
    def logger(self) -> logging.Logger:
        if not hasattr(self, "_logger"):
            self._logger = setup_logger(
                self.log_dir,
                GENERATE_REPORT_LOG_FILE,
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
                        if not self.check_specific(pr.id):
                            continue
                        if self.check_skip(pr.id):
                            continue
                        self._raw_dataset[pr.id] = pr

            self.logger.info(
                f"Successfully loaded {len(self._raw_dataset)} valid pull requests from {self.raw_dataset_files}"
            )

        return self._raw_dataset

    @property
    def dataset(self) -> Dict[str, EvaluationRecord]:
        if not self.dataset_files:
            raise ValueError(f"Invalid dataset_files: {self.dataset_files}")

        if not hasattr(self, "_dataset"):
            self.logger.info("Loading dataset...")
            self._dataset: dict[str, EvaluationRecord] = {}

            for file_path in self._expanded_files:
                with open(file_path, "r", encoding="utf-8") as f:
                    for line in f:
                        if line.strip() == "":
                            continue

                        dataset = EvaluationRecord.from_json(line)

                        if not self.check_specific(dataset.id):
                            continue
                        if self.check_skip(dataset.id):
                            continue
                        self._dataset[dataset.id] = dataset

            self.logger.info(
                f"Successfully loaded {len(self._dataset)} valid datasets from {self.dataset_files}"
            )

        return self._dataset

    @classmethod
    def from_dict(cls, d: dict) -> "CliArgs":
        data = cls(**d)
        data.__post_init__()
        return data

    @classmethod
    def from_json(cls, json_str: str) -> "CliArgs":
        data = cls.from_dict(cls.schema().loads(json_str))
        data.__post_init__()
        return data

    def dict(self) -> dict:
        return asdict(self)

    def json(self) -> str:
        return self.to_json(ensure_ascii=False)

    def check_specific(self, name: str) -> bool:
        if self.specifics and not any(
            name in specific or specific in name for specific in self.specifics
        ):
            return False
        return True

    def check_skip(self, name: str) -> bool:
        if self.skips and any(name in skip or skip in name for skip in self.skips):
            return True
        return False

    def collect_report_tasks(self, subdir: str = INSTANCE_WORKDIR) -> list[ReportTask]:
        self.logger.info("Collecting report tasks...")
        tasks: list[ReportTask] = []
        for org_dir in self.workdir.iterdir():
            if not org_dir.is_dir():
                continue

            org = org_dir.name
            for repo_dir in org_dir.iterdir():
                if not repo_dir.is_dir():
                    continue

                repo = repo_dir.name
                instances_dir = repo_dir / subdir
                if not instances_dir.exists():
                    continue

                for instance_dir in instances_dir.iterdir():
                    if instance_dir.is_dir() and instance_dir.name.startswith("pr-"):
                        try:
                            number = int(instance_dir.name[3:])
                            task = ReportTask(org, repo, number, instance_dir)
                            if not self.check_specific(task.id):
                                continue
                            if self.check_skip(task.id):
                                continue
                            tasks.append(task)
                        except ValueError:
                            continue
        tasks.sort(reverse=True)

        self.logger.info(f"Successfully collected {len(tasks)} tasks.")

        return tasks

    def gen_reports(
        self, tasks: list[ReportTask]
    ) -> tuple[list[Report], list[Report], list[ReportTask]]:
        reports: list[Report] = []
        invalid_reports: list[Report] = []
        failed_tasks: list[tuple[ReportTask, str]] = []
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=self.max_workers
        ) as executor:

            def safe_generate_report(task: ReportTask) -> Tuple[Report, bool] | None:
                try:
                    report = task.generate_report(regen=self.regen)
                    if not report.valid:
                        self.logger.error(
                            f"Invalid report for {task.id}, {report.short_report()}, {report.error_msg}"
                        )
                        return (report, False)

                    return (report, True)
                except Exception as e:
                    logging.error(f"Error generating report for {task.id}: {str(e)}")
                    failed_tasks.append((task, str(e)))
                    return None

            futures = [
                executor.submit(safe_generate_report, task)
                for task in tasks
                if task.id in self.raw_dataset
            ]

            for future in tqdm(
                concurrent.futures.as_completed(futures),
                total=len(futures),
                desc="Generating reports",
            ):
                result = future.result()
                if result is None:
                    continue
                report, valid = result
                if valid:
                    reports.append(report)
                else:
                    invalid_reports.append(report)

        self.logger.info(f"Successfully generated {len(reports)} reports.")
        if failed_tasks:
            self.logger.error(f"Failed to generate {len(failed_tasks)} reports.")
            self.logger.error("Failed task list:")
            for task, error in failed_tasks:
                self.logger.error(f"  - {task.id}: {error}")
        else:
            self.logger.info("All reports generated successfully.")

        return (reports, invalid_reports, [task for task, _ in failed_tasks])

    def gen_eval_reports(
        self, tasks: list[ReportTask]
    ) -> tuple[list[Report], list[Report], list[ReportTask]]:
        reports: list[Report] = []
        invalid_reports: list[Report] = []
        failed_tasks: list[tuple[ReportTask, str]] = []
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=self.max_workers
        ) as executor:

            def safe_generate_report(
                dataset: EvaluationRecord,
                task: ReportTask,
                run_log: str,
                test_patch_run_log: str,
            ) -> Union[Tuple[Report, bool], None]:
                try:
                    report = task.generate_report(
                        run_log, test_patch_run_log, regen=self.regen
                    )
                    if not report.valid:
                        self.logger.error(
                            f"Invalid report for {task.id}, {report.short_report()}, {report.error_msg}"
                        )
                        return (report, False)

                    for p2p in dataset.p2p_tests:
                        if p2p not in report.p2p_tests:
                            self.logger.error(
                                f"Invalid p2p_tests for {task.id}: missing {p2p}"
                            )
                            return (report, False)

                    for f2p in dataset.f2p_tests:
                        if f2p not in report.f2p_tests:
                            self.logger.error(
                                f"Invalid f2p_tests for {task.id}: missing {f2p}"
                            )
                            return (report, False)

                    for s2p in dataset.s2p_tests:
                        if s2p not in report.s2p_tests:
                            self.logger.error(
                                f"Invalid s2p_tests for {task.id}: missing {s2p}"
                            )
                            return (report, False)

                    for n2p in dataset.n2p_tests:
                        if n2p not in report.n2p_tests:
                            self.logger.error(
                                f"Invalid n2p_tests for {task.id}: missing {n2p}"
                            )
                            return (report, False)

                    return (report, True)
                except Exception as e:
                    logging.error(f"Error generating report for {task.id}: {str(e)}")
                    failed_tasks.append((task, str(e)))
                    return None

            futures = [
                executor.submit(
                    safe_generate_report,
                    self.dataset[task.id],
                    task,
                    run_log=self.dataset[task.id].run_result,
                    test_patch_run_log=self.dataset[task.id].test_patch_result,
                )
                for task in tasks
                if task.id in self.dataset
            ]

            reports = []
            for future in tqdm(
                concurrent.futures.as_completed(futures),
                total=len(futures),
                desc="Generating eval reports",
            ):
                result = future.result()
                if result is None:
                    continue
                report, valid = result
                if valid:
                    reports.append(report)
                else:
                    invalid_reports.append(report)

        self.logger.info(f"Successfully generated {len(reports)} reports.")
        if failed_tasks:
            self.logger.error(f"Failed to generate {len(failed_tasks)} reports.")
            self.logger.error("Failed task list:")
            for task, error in failed_tasks:
                self.logger.error(f"  - {task.id}: {error}")

        return (reports, invalid_reports, [task for task, _ in failed_tasks])

    def run_regen(self):
        tasks = self.collect_report_tasks()
        self.gen_reports(tasks)

    def run_summary(self):
        tasks = self.collect_report_tasks()
        reports, invalid_reports, failed_tasks = self.gen_reports(tasks)
        final_report = FinalReport.from_reports(reports, invalid_reports, failed_tasks)
        with open(self.output_dir / FINAL_REPORT_FILE, "w", encoding="utf-8") as f:
            f.write(final_report.json())

    def run_evaluation(self):
        tasks = self.collect_report_tasks(EVALUATION_WORKDIR)
        reports, invalid_reports, failed_tasks = self.gen_eval_reports(tasks)
        final_report = FinalReport.from_reports(reports, invalid_reports, failed_tasks)
        with open(self.output_dir / FINAL_REPORT_FILE, "w", encoding="utf-8") as f:
            f.write(final_report.to_json(indent=4, ensure_ascii=False))

    def run_dataset(self):
        tasks = self.collect_report_tasks()
        reports, invalid_reports, failed_tasks = self.gen_reports(tasks)
        final_report = FinalReport.from_reports(reports, invalid_reports, failed_tasks)
        with open(self.output_dir / FINAL_REPORT_FILE, "w", encoding="utf-8") as f:
            f.write(final_report.json())

        dataset: dict[str, list[EvaluationRecord]] = {}
        for report in reports:
            if report.id not in self.raw_dataset:
                continue
            if report.repo_file_name not in dataset:
                dataset[report.repo_file_name] = []
            dataset[report.repo_file_name].append(
                EvaluationRecord.build(self.raw_dataset[report.id], report)
            )

        for repo_file_name in dataset:
            dataset[repo_file_name].sort(reverse=True)
            with open(
                self.output_dir / f"{repo_file_name}_dataset.jsonl",
                "w",
                encoding="utf-8",
            ) as f:
                for data in dataset[repo_file_name]:
                    f.write(data.json())
                    f.write("\n")

    def run(self):
        if self.mode == "regen":
            self.run_regen()
        elif self.mode == "summary":
            self.run_summary()
        elif self.mode == "evaluation":
            self.run_evaluation()
        elif self.mode == "dataset":
            self.run_dataset()
        else:
            raise ValueError(f"Invalid mode: {self.mode}")


if __name__ == "__main__":
    parser = get_parser()
    args = parser.parse_args()
    cli = CliArgs.from_dict(vars(args))
    cli.run()
