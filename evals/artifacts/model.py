"""Frozen fixture descriptors; no submitted code runs in the evaluator process."""

from dataclasses import dataclass
import csv
import io
import json


def json_bytes(value) -> bytes:
    return (json.dumps(value, sort_keys=True, ensure_ascii=False, indent=2) + '\n').encode()


def csv_bytes(columns: list[str], rows: list[list]) -> bytes:
    output = io.StringIO(newline='')
    writer = csv.writer(output, lineterminator='\n')
    writer.writerow(columns)
    writer.writerows(rows)
    return output.getvalue().encode()


@dataclass(frozen=True)
class Step:
    inputs: dict[str, bytes]
    expected: dict[str, bytes]
    preserve_output: bool = False
    error_code: str | None = None
    error_row: int | None = None


@dataclass(frozen=True)
class Scenario:
    name: str
    steps: tuple[Step, ...]


@dataclass(frozen=True)
class TaskSpec:
    name: str
    instructions: str
    outputs: tuple[str, ...]
    development: Scenario
    verification: tuple[Scenario, Scenario]

    @property
    def prompt(self) -> str:
        return (
            'Deliver /work/output/solution.py using Python 3.12. Use only the supplied data.\n'
            'Interface: python solution.py --input-dir /input --output-dir /output\n'
            'During development inputs are at /input/0, /input/1, ... in execution order. Run every step.\n'
            'Write your development results into /work/output. For stateful steps preserve that output; '
            'for independent steps clear result files first, retaining solution.py.\n'
            'The script will be rerun with different paths and unseen data. Use only Python standard library '
            '(including sqlite3 and zoneinfo); no internet, no evaluator files.\n'
            'CSV headers and JSON field names are exact. CSV row order is irrelevant; duplicates are not. '
            'Integers must be exact base-10 values; missing nullable CSV fields are empty strings. '
            'Source row numbers are one-based data rows, excluding the header.\n\n' + self.instructions
        )
