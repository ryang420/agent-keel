"""Small synthetic tasks, each with independent development and held-out worlds."""

from . import cleaning, joins, incremental, replay, schema, revenue


def tasks():
    return tuple(module.build() for module in (cleaning, joins, incremental, replay, schema, revenue))


def worlds(make, seed):
    from ..model import Scenario

    return (Scenario('development', make(seed)),
            (Scenario('verification-1', make(seed + 100)), Scenario('verification-2', make(seed + 200))))
