"""Use DAB's per-call ledger logic with this suite's own persisted protocol."""

from dataclasses import replace
import json

from evals.dab.budget import BudgetError, Ledger as DabLedger
from evals.dab.accounting import BudgetProvider

from . import PROTOCOL
from .model import json_bytes


class Ledger(DabLedger):
    @classmethod
    def open(cls, path):
        if not path.is_file():
            raise BudgetError('no artifact budget grant')
        data = json.loads(path.read_text())
        if data.get('protocol') != PROTOCOL:
            raise BudgetError('budget belongs to another protocol')
        return cls(path=path, max_tokens=int(data['max_tokens']), spent_tokens=int(data['spent_tokens']),
                   entries=list(data.get('entries', [])))

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {'protocol': PROTOCOL, 'max_tokens': self.max_tokens, 'spent_tokens': self.spent_tokens,
                   'entries': self.entries}
        temporary = self.path.with_suffix('.part')
        temporary.write_bytes(json_bytes(payload))
        temporary.replace(self.path)


class LimitedProvider(BudgetProvider):
    async def stream(self, request):
        # The output reservation is also an actual provider output limit for this suite.
        request = replace(request, max_output_tokens=min(request.max_output_tokens or 8192, 8192))
        async for event in super().stream(request):
            yield event
