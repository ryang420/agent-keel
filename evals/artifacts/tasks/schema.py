from ..model import Step, TaskSpec, csv_bytes, json_bytes

RULES = '''Read schema.json {"version":1 or 2} and records.csv. v1 columns id,date,amount_cents; v2 columns
id,date,amount_decimal, optional channel. Unknown extra columns ignored, column ordering arbitrary. Dates are
valid YYYY-MM-DD. Write normalized.csv(id,date,amount_cents,channel). Default missing/empty channel to unknown.
v1 cents are signed integers. v2 decimal uses optional minus, digits, optional decimal point with one or two
fractional digits; no exponents/rounding. Reject BOTH amount columns present (conflicting_fields), unknown
version (unsupported_schema), invalid amount (invalid_amount), or malformed required field (invalid_field).
Any error rejects the entire invocation: nonzero exit, error.json {"code":reason,"row":source_row} for a row
error or {"code":reason} for a schema/header error, and no normalized.csv. Validate schema/header before rows.
Every development and verification step is independent; clear the output directory before each step.'''


def _steps(seed):
    def step(version, columns, rows, expected=None, error=None, row=None):
        gold = {'normalized.csv': csv_bytes(['id', 'date', 'amount_cents', 'channel'], expected)} if expected else {}
        return Step({'schema.json': json_bytes({'version': version}), 'records.csv': csv_bytes(columns, rows)},
                    gold, error_code=error, error_row=row)
    one = [[f'r{seed}-{i}', '2026-01-01', (i - 4) * seed] for i in range(12)]
    expected = [[*r, 'unknown'] for r in one]
    return (step(1, ['id', 'date', 'amount_cents'], one, expected),
            step(2, ['channel', 'amount_decimal', 'date', 'id', 'extra'],
                 [['web', f'{seed}.29', '2026-01-02', f'r{seed}-z', 'ignored'],
                  ['', '-0.01', '2026-01-03', f'r{seed}-n', 'ignored']],
                 [[f'r{seed}-z', '2026-01-02', seed * 100 + 29, 'web'],
                  [f'r{seed}-n', '2026-01-03', -1, 'unknown']]),
            step(2, ['id', 'date', 'amount_decimal'], [['bad', '2026-01-01', '1.005']], error='invalid_amount', row=1),
            step(2, ['id', 'date', 'amount_decimal', 'amount_cents'], [], error='conflicting_fields'),
            step(9, ['id', 'date', 'amount_cents'], one, error='unsupported_schema'))


def build(seed=23):
    from . import worlds
    dev, verification = worlds(_steps, seed)
    return TaskSpec('schema', RULES, ('normalized.csv', 'error.json'), dev, verification)
