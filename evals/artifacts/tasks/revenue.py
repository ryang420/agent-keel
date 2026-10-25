from ..model import Step, TaskSpec, csv_bytes, json_bytes

ORDERS = ['order_id', 'paid_at', 'amount_cents', 'currency', 'is_test', 'status']
REFUNDS = ['refund_id', 'order_id', 'amount_cents', 'refunded_at']
RULES = '''Read orders.csv(order_id,paid_at,amount_cents,currency,is_test,status) and
refunds.csv(refund_id,order_id,amount_cents,refunded_at). Order IDs unique, cents nonnegative integers,
is_test=0/1, timestamps valid ISO UTC. Eligible means status=paid, currency=USD and is_test=0.
Group eligible orders by Asia/Shanghai payment date. Refunds reduce the original payment-day revenue regardless
of refund date. Identical repeated refund IDs collapse; conflicting repeated IDs reject the invocation as
conflicting_refund. Reject references to nonexistent orders as orphan_refund; reject sum refunds above order
amount as over_refund (even on ineligible orders). Priority: conflicting_refund, orphan_refund, over_refund.
Otherwise exclude ineligible orders and their refunds. Write revenue_daily.csv(date,gross_cents,refund_cents,
net_cents,order_count,refund_count) and summary.json with the same numeric fields (without date), counting unique
eligible refunds. Retain days with net zero; omit days without eligible orders. Errors: nonzero exit,
error.json {"code":reason} and no data files. Each step is independent; clear output before each step.'''


def _steps(seed):
    orders = [[f'o{seed}-1', '2026-01-01T15:59:59Z', 5 * seed, 'USD', 0, 'paid'],
              [f'o{seed}-2', '2026-01-01T16:00:00Z', 3 * seed, 'USD', 0, 'paid'],
              [f'o{seed}-3', '2026-01-02T16:00:00Z', seed, 'USD', 1, 'paid'],
              [f'o{seed}-4', '2026-01-01T00:00:00Z', seed, 'EUR', 0, 'paid'],
              [f'o{seed}-5', '2026-01-01T00:00:00Z', seed, 'USD', 0, 'pending']]
    extras = seed % 3 + 1
    orders += [[f'o{seed}-extra-{n}', '2026-01-01T12:00:00Z', seed, 'USD', 0, 'paid']
               for n in range(extras)]
    r = [[f'r{seed}-1', orders[0][0], seed, '2026-02-01T00:00:00Z'],
         [f'r{seed}-2', orders[0][0], seed, '2026-02-02T00:00:00Z'],
         [f'r{seed}-3', orders[1][0], 3 * seed, '2026-02-03T00:00:00Z'],
         [f'r{seed}-4', orders[2][0], seed, '2026-02-04T00:00:00Z']]
    columns = ['date', 'gross_cents', 'refund_cents', 'net_cents', 'order_count', 'refund_count']
    expected = {'revenue_daily.csv': csv_bytes(columns, [['2026-01-01', (5 + extras) * seed, 2 * seed,
                                                        (3 + extras) * seed, 1 + extras, 2],
                                                        ['2026-01-02', 3 * seed, 3 * seed, 0, 1, 1]]),
                'summary.json': json_bytes(dict(zip(columns[1:], [(8 + extras) * seed, 5 * seed,
                                                                 (3 + extras) * seed, 2 + extras, 3])))}
    def inputs(refunds):
        return {'orders.csv': csv_bytes(ORDERS, orders), 'refunds.csv': csv_bytes(REFUNDS, refunds)}
    return (Step(inputs([*r, r[0]]), expected),
            Step(inputs([*r, ['orphan', 'missing', 1, r[0][-1]]]), {}, error_code='orphan_refund'),
            Step(inputs([*r, ['excess', orders[0][0], 9 * seed, r[0][-1]]]), {}, error_code='over_refund'),
            Step(inputs([*r, [r[0][0], r[0][1], 1, r[0][-1]]]), {}, error_code='conflicting_refund'))


def build(seed=29):
    from . import worlds
    dev, verification = worlds(_steps, seed)
    return TaskSpec('revenue', RULES, ('revenue_daily.csv', 'summary.json', 'error.json'), dev, verification)
