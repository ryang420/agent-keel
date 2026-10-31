"""DataAgentBench scoring harness for agent-keel.

Protocol ``dab-score-v1``: a frozen 12-dataset / 54-query slice of the pinned DAB revision, executed through the
current Keel runtime and scored by DAB's own validators. Generated runs, datasets and reports live under the
gitignored ``.local/evals/`` tree; gold and validators never enter a sandbox.

Modules
-------
``suitegen``  One-off developer tool that writes ``suite.json`` from a pinned DAB checkout.
``suite``     Loads and validates the tracked suite manifest.
``inputs``    Fetches pinned inputs, verifies every hash and reports each dataset's readiness.
``prompt``    Builds the query instructions and checks that no evaluator material leaks into them.
``policy``    Builds sandbox policies and per-profile read-only mount trees.
``budget``    Cumulative token ledger and reservation.
``runner``    Drives one fresh profile, session and run per query through the in-process API.
``scoring``   Runs the pinned ``validate.py`` outside every sandbox and aggregates Pass@1 by dataset mean.
``report``    Assembles planned/ready/executed/scored/blocked/budget_exhausted reports.
``cli``       ``prepare``, ``validate``, ``execute``, ``score`` and ``report`` commands.
"""

PROTOCOL = "dab-score-v2"

__all__ = ["PROTOCOL"]
