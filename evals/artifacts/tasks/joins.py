from ..model import Step, TaskSpec, csv_bytes, json_bytes

RULES = '''Read orders.csv(order_id,customer_id,net_cents) and customer_tags.csv(customer_id,tag).
Order IDs are unique. customer_tags can have duplicate rows and multiple tags per customer.
overall.json must contain order_count and net_cents, counting every order exactly once.
by_tag.csv must contain tag,order_count,net_cents: contribute each order once per distinct customer tag.
Missing customer tags use reserved UNASSIGNED (this string never appears in input tags). Overlapping tags
mean tag totals need not sum to overall. Ignore tags for customers without orders. Each step is independent.'''


def _steps(seed):
    orders = [[f'o{seed}-{i}', f'c{seed}-{i % 3}', (i + 1) * seed] for i in range(12)]
    tags = [[f'c{seed}-0', 'A'], [f'c{seed}-0', 'A'], [f'c{seed}-0', 'B'],
            [f'c{seed}-1', 'B'], ['unused', 'Z']]
    # 1+4+7+10=22; 2+5+8+11=26; 3+6+9+12=30.
    groups = [['A', 4, 22 * seed], ['B', 8, 48 * seed], ['UNASSIGNED', 4, 30 * seed]]
    return (Step({'orders.csv': csv_bytes(['order_id', 'customer_id', 'net_cents'], orders),
                  'customer_tags.csv': csv_bytes(['customer_id', 'tag'], tags)},
                 {'overall.json': json_bytes({'order_count': 12, 'net_cents': 78 * seed}),
                  'by_tag.csv': csv_bytes(['tag', 'order_count', 'net_cents'], groups)}),)


def build(seed=13):
    from . import worlds
    dev, verification = worlds(_steps, seed)
    return TaskSpec('joins', RULES, ('overall.json', 'by_tag.csv'), dev, verification)
