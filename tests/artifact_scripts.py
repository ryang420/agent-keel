"""Known code for no-model sandbox acceptance; never exposed as an agent task input."""

JOINS = b'''import argparse,csv,json,pathlib,collections
p=argparse.ArgumentParser();p.add_argument('--input-dir');p.add_argument('--output-dir');a=p.parse_args()
i=pathlib.Path(a.input_dir);o=pathlib.Path(a.output_dir);o.mkdir(exist_ok=True,parents=True)
orders=list(csv.DictReader((i/'orders.csv').open()));tags=collections.defaultdict(set)
for r in csv.DictReader((i/'customer_tags.csv').open()):tags[r['customer_id']].add(r['tag'])
groups=collections.defaultdict(lambda:[0,0])
for r in orders:
 for tag in tags[r['customer_id']] or {'UNASSIGNED'}:
  groups[tag][0]+=1;groups[tag][1]+=int(r['net_cents'])
(o/'overall.json').write_text(json.dumps(dict(order_count=len(orders),net_cents=sum(int(r['net_cents']) for r in orders))))
with (o/'by_tag.csv').open('w') as f:
 w=csv.writer(f);w.writerow(['tag','order_count','net_cents']);w.writerows([k,*v] for k,v in groups.items())
'''

REPLAY = b'''import argparse,csv,json,pathlib,sys,collections
p=argparse.ArgumentParser();p.add_argument('--input-dir');p.add_argument('--output-dir');a=p.parse_args()
i=pathlib.Path(a.input_dir);o=pathlib.Path(a.output_dir);o.mkdir(exist_ok=True,parents=True)
f=o/'state.json';old=json.loads(f.read_text())['transactions'] if f.exists() else []
rows={r['transaction_id']:r for r in old}
for r in csv.DictReader((i/'transactions.csv').open()):
 r['amount_cents']=int(r['amount_cents']);key=r['transaction_id']
 if key in rows and rows[key]!=r:sys.exit(2)
 rows[key]=r
groups=collections.defaultdict(lambda:[0,0])
for r in rows.values():
 key=(r['event_timestamp'][:10],r['region']);groups[key][0]+=r['amount_cents'];groups[key][1]+=1
f.write_text(json.dumps({'transactions':list(rows.values())}))
with (o/'daily.csv').open('w') as f:
 w=csv.writer(f);w.writerow(['date','region','net_cents','transaction_count']);w.writerows([*k,*v] for k,v in groups.items())
'''
