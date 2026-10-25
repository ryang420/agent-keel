"""Opt-in artifact evaluator. prepare/validate/report never call a model."""

import argparse
import asyncio
import json
import os
from pathlib import Path
import re
import uuid

from evals.dab.runner import image_id

from .budget import Ledger
from .fixtures import prepare, validate
from .model import json_bytes
from .report import build, markdown
from .runner import execute
from .tasks import tasks

DEFAULT_ROOT = '.local/evals/artifacts'
DEFAULT_IMAGE = 'python:3.12-slim'


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest='command', required=True)
    for name in ('prepare', 'validate', 'execute', 'report'):
        sub = subs.add_parser(name)
        sub.add_argument('--root', type=Path, default=Path(DEFAULT_ROOT))
        if name in ('validate', 'execute'):
            sub.add_argument('--image', default=DEFAULT_IMAGE)
        if name == 'validate':
            sub.add_argument('--skip-image', action='store_true', help='check fixture bytes only')
        if name in ('execute', 'report'):
            sub.add_argument('--run-id', default=None)
        if name == 'execute':
            sub.add_argument('--max-tokens', type=int, required=True)
            sub.add_argument('--tasks', nargs='+', choices=[t.name for t in tasks()])
            sub.add_argument('--model', default='gpt-6-luna')
    args = parser.parse_args(argv)
    if args.command == 'prepare':
        manifest = prepare(args.root)
        print(f"Prepared six tasks; {len(manifest['files'])} frozen files.")
        return 0
    if args.command == 'validate':
        result = validate(args.root)
        if not args.skip_image:
            try:
                result['image_id'] = image_id(args.image)
            except RuntimeError:
                result['ready'] = False
                result['problems'].append('image_unavailable')
        print(json.dumps(result))
        return 0 if result['ready'] else 1
    run_id = args.run_id or uuid.uuid4().hex
    if not re.fullmatch(r'[a-zA-Z0-9_-]{1,80}', run_id):
        raise SystemExit('invalid run ID')
    run_root = args.root / 'runs' / run_id
    if args.command == 'report':
        if not args.run_id:
            raise SystemExit('report requires --run-id')
        config = json.loads((run_root / 'config.json').read_text())
        records_path = run_root / 'records.json'
        records = json.loads(records_path.read_text()) if records_path.exists() else []
        report = build(records, config['selected'])
    else:
        if args.max_tokens <= 0:
            raise SystemExit('positive --max-tokens is required')
        if run_root.exists():
            raise SystemExit('run directory exists; choose a new --run-id')
        if not validate(args.root)['ready']:
            raise SystemExit('fixtures are not ready; run prepare/validate')
        admin = os.environ.get('KEEL_ARTIFACT_ADMIN_DSN') or os.environ.get('KEEL_TEST_POSTGRES_DSN')
        if not admin:
            raise SystemExit('set KEEL_ARTIFACT_ADMIN_DSN or KEEL_TEST_POSTGRES_DSN')
        selected = [t for t in tasks() if not args.tasks or t.name in args.tasks]
        ledger = Ledger.grant(args.root / 'budgets' / (run_id + '.json'), args.max_tokens)
        records = asyncio.run(execute(selected, admin_dsn=admin, ledger=ledger, root=run_root,
                                      image=args.image, model=args.model))
        report = build(records, [t.name for t in selected])
    (run_root / 'report.json').write_bytes(json_bytes(report))
    (run_root / 'report.md').write_text(markdown(report))
    print(markdown(report))
    return 0 if report['complete'] else 1
