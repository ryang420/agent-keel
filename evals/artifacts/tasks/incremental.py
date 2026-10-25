from ..model import Step, TaskSpec, csv_bytes, json_bytes

COLUMNS = ['account_id', 'event_id', 'version', 'updated_at', 'event_date', 'amount_cents', 'op']
RULES = '''Read initial.csv and changes.csv, with columns account_id,event_id,version,updated_at,event_date,
amount_cents,op. Key=(account_id,event_id). All timestamps are valid UTC ISO; versions nonnegative integers.
First invocation initializes state from initial.csv; subsequent invocations use output state.json instead.
Resolve updates by highest version then latest updated_at, independently of event_date/arrival order.
op is upsert or delete. Delete payload event_date/amount_cents are empty. Identical ties collapse; different
payloads at the same key/version/updated_at abort the whole invocation with nonzero exit and no state changes.
Keep tombstones to stop stale resurrection. state.json is {"rows":[objects with ALL input fields]}, version and
non-null amount_cents integers; delete event_date and amount_cents JSON null. current.csv has input columns
except op and contains live rows only. Development steps 0..2 preserve output after initialization; step 3 is
a conflicting-tie rejection and must leave both files unchanged. State row ordering is irrelevant.'''


def _steps(seed):
    a = f'a{seed}'
    ts = '2026-01-01T00:00:00Z'
    def row(event, version, amount, op='upsert', date='2026-01-01'):
        return [a, event, version, ts, date if op == 'upsert' else '', amount if op == 'upsert' else '', op]
    initial = [row('x', 1, seed), row('y', 1, 2 * seed)]
    def expected(rows):
        objects = [dict(zip(COLUMNS, r)) for r in rows]
        for obj in objects:
            if obj['op'] == 'delete':
                obj['event_date'] = obj['amount_cents'] = None
        return {'state.json': json_bytes({'rows': objects}),
                'current.csv': csv_bytes(COLUMNS[:-1], [r[:-1] for r in rows if r[-1] == 'upsert'])}
    states = [[row('x', 2, 3 * seed, date='2025-12-01'), row('y', 2, 0, 'delete')],
              [row('x', 2, 3 * seed, date='2025-12-01'), row('y', 2, 0, 'delete'), row('z', 1, 5 * seed)],
              [row('x', 3, 7 * seed), row('y', 2, 0, 'delete'), row('z', 1, 5 * seed)]]
    changes = [states[0], [row('y', 1, seed), states[1][-1]],
               [row('x', 1, seed), states[2][0], states[2][0]],
               [row('x', 3, 99 * seed)]]
    # Same event ID under a different account must remain independent.
    other = [a + '-other', 'x', 1, ts, '2026-01-01', 11 * seed, 'upsert']
    changes[0] = [*changes[0], other]
    for state in states:
        state.append(other)
    # Same version: the newer timestamp wins, even when it arrives before an older row.
    newer = row('z', 1, 9 * seed)
    newer[3] = '2026-01-02T00:00:00Z'
    changes[2] += [newer, row('z', 1, 5 * seed)]
    states[2] = [newer if r[1] == 'z' else r for r in states[2]]
    return tuple(Step({'initial.csv': csv_bytes(COLUMNS, initial), 'changes.csv': csv_bytes(COLUMNS, batch)},
                      expected(states[min(i, 2)]), preserve_output=i > 0,
                      error_code='conflicting_update' if i == 3 else None)
                 for i, batch in enumerate(changes))


def build(seed=17):
    from . import worlds
    dev, verification = worlds(_steps, seed)
    return TaskSpec('incremental', RULES, ('state.json', 'current.csv'), dev, verification)
