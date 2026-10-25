from ..model import Step, TaskSpec, csv_bytes, json_bytes

COLUMNS = ['transaction_id', 'event_timestamp', 'region', 'amount_cents']
RULES = '''Read transactions.csv(transaction_id,event_timestamp,region,amount_cents) and batch.json(batch_id).
Persist state.json as {"transactions":[objects with all input fields, integer amount_cents]}. Transaction ID is
globally unique. Ignore repeated identical transactions even across different batches. Conflicting payload for
an existing ID aborts with nonzero exit, leaving state.json and daily.csv unchanged. daily.csv columns are
date,region,net_cents,transaction_count, using UTC date and distinct transaction IDs. Include net-zero groups.
Steps 0..3 are A,A,B,B, preserving outputs; step 4 is an expected conflicting payload rejection; step 5 is a
fresh independent combined batch and must be run with an empty output directory. Logical state must match
the prior successful A+B state, independent of batch identity and row order.'''


def _steps(seed):
    a = [[f't{seed}-1', '2026-01-01T00:00:00Z', 'east', seed],
         [f't{seed}-2', '2026-01-01T23:59:59Z', 'east', -seed]]
    extra = [f't{seed}-3', '2026-01-02T00:00:00Z', 'west', 3 * seed]
    b = [a[1], extra]
    wrong = [[a[0][0], a[0][1], 'east', 99 * seed]]
    batches = [a, a, b, b, wrong, [extra, *reversed(a)]]
    results = []
    for i, batch in enumerate(batches):
        union = a if i < 2 else [*a, extra]
        daily = [['2026-01-01', 'east', 0, 2]]
        if i >= 2:
            daily.append(['2026-01-02', 'west', 3 * seed, 1])
        results.append(Step({'transactions.csv': csv_bytes(COLUMNS, batch),
                             'batch.json': json_bytes({'batch_id': f'{seed}-{i // 2}'})},
                            {'state.json': json_bytes({'transactions': [dict(zip(COLUMNS, r)) for r in union]}),
                             'daily.csv': csv_bytes(['date', 'region', 'net_cents', 'transaction_count'], daily)},
                            preserve_output=i in (1, 2, 3, 4),
                            error_code='conflicting_transaction' if i == 4 else None))
    return tuple(results)


def build(seed=19):
    from . import worlds
    dev, verification = worlds(_steps, seed)
    return TaskSpec('replay', RULES, ('state.json', 'daily.csv'), dev, verification)
