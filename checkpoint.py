"""Fingerprint the observable expression graph at an executor optimizer boundary."""

import hashlib
import json
from enum import Enum


def digest(*roots):
    from sqlglot import exp

    nodes = []
    indices = {}

    def value(v):
        if isinstance(v, exp.Expr):
            index = indices.get(id(v))
            if index is None:
                index = len(nodes)
                indices[id(v)] = index
                nodes.append(v)
            return ["node", index]
        if isinstance(v, Enum):
            return ["enum", type(v).__module__, type(v).__qualname__, v.name]
        if v is None or type(v) in (bool, str, int, float):
            return v
        if type(v) in (tuple, list):
            return [type(v).__name__, [value(x) for x in v]]
        if type(v) is dict:
            return ["dict", [[value(k), value(x)] for k, x in v.items()]]
        raise TypeError("unsupported optimizer result")

    entries = []
    heads = [value(root) for root in roots]
    for node in nodes:
        if getattr(node, "__dict__", None):
            raise ValueError("additional instance state")
        entries.append([type(node).__module__, type(node).__qualname__, value(node.args),
                        value(node.parent), node.arg_key, node.index, value(node.comments),
                        value(node._type), value(node._meta)])
    # Hash values may incorporate process-specific type hashes. A populated cache
    # must still agree with the graph. Restore the exact state after verification.
    cached = [node._hash for node in nodes]
    try:
        for node in nodes:
            node._hash = None
        for node, previous in zip(nodes, cached):
            if previous is not None and hash(node) != previous:
                raise ValueError("stale expression hash")
    finally:
        for node, previous in zip(nodes, cached):
            node._hash = previous
    return hashlib.sha256(json.dumps([heads, entries], separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def table_digest(table):
    """Keep column order, row order and exact scalar types."""
    import datetime
    from decimal import Decimal

    def atom(v):
        if v is None or type(v) in (int, bool, str):
            return v
        if type(v) is float:
            return ["float", v.hex()]
        if type(v) in (datetime.date, datetime.time, datetime.datetime):
            return [type(v).__name__, v.isoformat()]
        if type(v) is Decimal:
            return ["decimal", str(v)]
        if type(v) is bytes:
            return ["bytes", v.hex()]
        raise TypeError("unsupported result cell")
    rows = [[atom(v) for v in row] for row in table.rows]
    raw = json.dumps([list(table.columns), rows], separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(raw.encode()).hexdigest()
