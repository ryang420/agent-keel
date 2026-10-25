from ..model import Step, TaskSpec, csv_bytes

COLUMNS = ['event_id', 'version', 'updated_at', 'amount_cents', 'status']
RULES = '''Read events.csv (event_id,version,updated_at,amount_cents,status). Valid status: paid or void.
event_id must be nonempty; version is a nonnegative integer; updated_at must be valid ISO UTC ending Z;
amount_cents is empty (null) or a signed integer. Reject invalid rows using the first applicable rule in
this order: invalid_key, invalid_version, invalid_timestamp, invalid_amount, invalid_status.
For each valid event choose highest version then latest timestamp. Exact duplicates collapse. If the maximum
version/timestamp group has different payloads, reject all rows in that group as conflicting_update and retry
selection among lower valid rows. Repeat as necessary. Superseded valid rows and exact duplicates are not rejects.
Write clean.csv with the input columns and rejects.csv with source_row,reason. Preserve null, zero and negative
amounts. The one development step is independent; all verification inputs are independent.'''


def _steps(seed):
    key = lambda n: f'e{seed}-{n}'
    ts = '2026-01-01T00:00:00Z'
    rows = [[key(0), 1, ts, '', 'paid'], [key(0), 2, ts, 0, 'paid'],
            [key(1), 1, ts, -seed, 'paid'], [key(1), 1, ts, -seed, 'paid'],
            [key(2), 1, ts, '', 'void'], [key(2), 2, ts, 80, 'paid'],
            [key(2), 2, ts, 90, 'paid'], ['', 1, ts, 1, 'paid'],
            [key(3), 'x', ts, 1, 'paid'], [key(4), 1, 'bad', 1, 'paid'],
            [key(5), 1, ts, '1.5', 'paid'], [key(6), 1, ts, 1, 'bad']]
    stable = [[key(n), 1, ts, n * seed, 'paid'] for n in range(10, 22)]
    later = [key(7), 1, '2026-01-02T00:00:00Z', -2 * seed, 'paid']
    rows += [later, [key(7), 1, ts, seed, 'paid'], *stable]
    expected = [rows[1], rows[2], rows[4], later, *stable]
    rejects = [[6, 'conflicting_update'], [7, 'conflicting_update'], [8, 'invalid_key'],
               [9, 'invalid_version'], [10, 'invalid_timestamp'], [11, 'invalid_amount'], [12, 'invalid_status']]
    # Rotate both source rows and source-number gold in held-out worlds.
    shift = seed % len(rows)
    rotated = rows[shift:] + rows[:shift]
    rejects = [[(n - 1 - shift) % len(rows) + 1, why] for n, why in rejects]
    return (Step({'events.csv': csv_bytes(COLUMNS, rotated)},
                 {'clean.csv': csv_bytes(COLUMNS, expected),
                  'rejects.csv': csv_bytes(['source_row', 'reason'], rejects)}),)


def build(seed=11):
    from . import worlds
    dev, verification = worlds(_steps, seed)
    return TaskSpec('cleaning', RULES, ('clean.csv', 'rejects.csv'), dev, verification)
