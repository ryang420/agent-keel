"""Typed output checks; accepts bytes, never imports a submitted program."""

from collections import Counter
import csv
from dataclasses import dataclass
import io
import json
import re

from .model import Step

INTEGER = re.compile(r'-?(?:0|[1-9][0-9]*)\Z')
INT_COLUMNS = frozenset({'version', 'amount_cents', 'source_row', 'order_count', 'net_cents', 'gross_cents',
                         'refund_cents', 'refund_count', 'transaction_count'})


@dataclass(frozen=True)
class CheckResult:
    passed: bool
    failures: tuple[str, ...]


def _csv(data: bytes):
    reader = csv.reader(io.StringIO(data.decode('utf-8'), newline=''), strict=True)
    header = next(reader)
    if not header or len(header) != len(set(header)):
        raise ValueError('invalid header')
    rows = []
    for row in reader:
        if len(row) != len(header):
            raise ValueError('wrong column count')
        values = []
        for key, value in zip(header, row):
            if key in INT_COLUMNS:
                if value == '' and key == 'amount_cents':
                    value = None
                elif not INTEGER.fullmatch(value):
                    raise ValueError('invalid integer')
                else:
                    value = int(value)
            values.append(value)
        rows.append(tuple(values))
    return header, Counter(rows)


def _canonical(value):
    # Tag scalars: Python otherwise considers True == 1 and 1.0 == 1.
    if isinstance(value, dict):
        return ('object', tuple((k, _canonical(v)) for k, v in sorted(value.items())))
    if isinstance(value, list):
        return ('array', tuple(sorted((_canonical(v) for v in value), key=repr)))
    return type(value).__name__, value


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON key')
        result[key] = value
    return result


def _json(data):
    def invalid(value):
        raise ValueError(f'invalid JSON constant {value}')
    return json.loads(data, object_pairs_hook=_object, parse_constant=invalid)


def check_step(step: Step, files: dict[str, bytes], exit_code: int) -> CheckResult:
    failures = []
    if (exit_code != 0) != bool(step.error_code):
        failures.append('exit_code')
    expected = dict(step.expected)
    if step.error_code and not expected:
        error = {'code': step.error_code}
        if step.error_row is not None:
            error['row'] = step.error_row
        expected['error.json'] = json.dumps(error).encode()
    for name, gold in expected.items():
        if name not in files:
            failures.append(f'missing:{name}')
            continue
        try:
            parser = _csv if name.endswith('.csv') else lambda data: _canonical(_json(data))
            reference = parser(gold)
        except (ValueError, UnicodeError, csv.Error, StopIteration) as exc:
            raise ValueError(f'invalid evaluator fixture {name}') from exc
        try:
            predicted = parser(files[name])
        except (ValueError, UnicodeError, csv.Error, StopIteration):
            failures.append(f'invalid:{name}')
            continue
        if predicted != reference:
            failures.append(f'mismatch:{name}')
    failures.extend(f'unexpected:{name}' for name in files.keys() - expected.keys())
    return CheckResult(not failures, tuple(sorted(failures)))
