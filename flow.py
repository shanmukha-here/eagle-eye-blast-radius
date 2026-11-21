"""Compare control flow against test coverage of the unchanged Python bytecode."""

import ast
import dis
import hashlib
import inspect
import marshal
from functools import lru_cache
from types import CodeType

JUMPS = getattr(dis, "hasjump", None) or (set(dis.hasjrel) | set(dis.hasjabs))


def signature(code):
    """Stable across source roots. Offsets are only valid for this code layout."""
    h = hashlib.sha256(code.co_code)
    h.update(code.co_exceptiontable)
    h.update(code.co_linetable)
    h.update(repr((code.co_names, code.co_varnames, code.co_flags, code.co_freevars, code.co_cellvars)).encode())
    return h.hexdigest()[:32]


@lru_cache(maxsize=64)
def functions(code):
    lookup = {}
    def walk(current):
        for child in current.co_consts:
            if isinstance(child, CodeType):
                lookup.setdefault((child.co_firstlineno, child.co_name), []).append(child)
                walk(child)
    walk(code)
    return lookup


def operand(instruction):
    if instruction.opcode in JUMPS:
        return None
    if instruction.opcode in dis.hasconst:
        return marshal.dumps(instruction.argval, 2)
    if instruction.opcode in dis.hasname:
        # argrepr carries the NULL/method-call flag on LOAD_GLOBAL and LOAD_ATTR.
        return instruction.argval, instruction.argrepr
    return instruction.arg, instruction.argval


@lru_cache(maxsize=256)
def changed_offsets(old, new):
    """Follow both possible branches while opcodes and their effects agree."""
    if (old.co_exceptiontable or new.co_exceptiontable or old.co_flags != new.co_flags
            or old.co_flags & (inspect.CO_GENERATOR | inspect.CO_COROUTINE | inspect.CO_ASYNC_GENERATOR)
            or old.co_varnames != new.co_varnames or old.co_freevars != new.co_freevars
            or old.co_cellvars != new.co_cellvars
            or any(isinstance(v, CodeType) for c in (old, new) for v in c.co_consts)):
        return None
    a, b = list(dis.get_instructions(old)), list(dis.get_instructions(new))
    if any(i.opname == "EXTENDED_ARG" for seq in (a, b) for i in seq):
        return None
    indices = [{inst.offset: i for i, inst in enumerate(seq)} for seq in (a, b)]
    frontier = [(0, 0)]
    visited, changed = set(), set()
    while frontier:
        pair = frontier.pop()
        if pair in visited:
            continue
        visited.add(pair)
        x, y = pair
        if len(visited) > 8192 or x >= len(a) or y >= len(b):
            return None
        left, right = a[x], b[y]
        if left.opname != right.opname or operand(left) != operand(right):
            changed.add(left.offset)
            continue
        if left.opcode in JUMPS:
            frontier.append((indices[0][left.argval], indices[1][right.argval]))
            if left.opname.startswith(("JUMP_FORWARD", "JUMP_BACKWARD")):
                continue
        if left.opname in ("RETURN_CONST", "RETURN_VALUE", "RAISE_VARARGS", "RERAISE"):
            continue
        frontier.append((x + 1, y + 1))
    return changed


def code_pair(old_module, new_module, key, old_scope, new_scope):
    old = functions(old_module).get((old_scope[0],key[-2]), ())
    new = functions(new_module).get((new_scope[0],key[-2]), ())
    if len(old) != 1 or len(new) != 1 or old[0].co_qualname != new[0].co_qualname:
        return None
    return old[0], new[0]


class Flow:
    def __init__(self, data):
        self.code = data["code"]
        self.pool = [int(x, 16) for x in data["pool"]]
        self.opaque = int(data["opaque"], 16) | int(data.get("reflect", "0"), 16)
        self.types = data.get("types", {})
        self.files = set(data["files"])

    def equality(self, path, old, new):
        before = [n for n in ast.walk(old) if isinstance(n, ast.Compare)]
        after = [n for n in ast.walk(new) if isinstance(n, ast.Compare)]
        if len(before) != len(after):
            return False
        changed = [(a, b) for a, b in zip(before, after) if [type(x) for x in a.ops] != [type(x) for x in b.ops]]
        if len(changed) != 1:
            return False
        a, b = changed[0]
        if len(a.ops) != 1 or len(b.ops) != 1 or {type(a.ops[0]), type(b.ops[0])} not in (
                {ast.Is, ast.Eq}, {ast.IsNot, ast.NotEq}):
            return False
        ops = b.ops
        b.ops = a.ops
        try:
            if ast.dump(old) != ast.dump(new):
                return False
        finally:
            b.ops = ops
        types = [self.types.get(f"{path}:{a.lineno}:{a.col_offset}:{i}", []) for i in (0, 1)]
        for i, expr in enumerate((a.left, a.comparators[0])):
            if isinstance(expr, ast.Constant) and expr.value is None:
                other = types[1-i]
                return bool(other) and all(t == "identity" or t.startswith(("enum:", "builtin:")) for t in other)
        return len(types[0]) == 1 and types[0] == types[1] and types[0][0].startswith("enum:")

    def selection(self, path, old_module, new_module, key, old_scope, new_scope, uncalled=False):
        pair = code_pair(old_module, new_module, key, old_scope, new_scope)
        if pair is None:
            return None
        old, new = pair
        name = f"{old.co_firstlineno}:{old.co_qualname}:{signature(old)}"
        ranges = self.code.get(path, {}).get(name)
        if not ranges:
            # Cross-check an absent call against the independent function-entry
            # trace. A different code layout under this name is not proof.
            prefix = f"{old.co_firstlineno}:{old.co_qualname}:"
            if (uncalled and path in self.files and not any(k.startswith(prefix) for k in self.code.get(path, {}))
                    and old.co_flags == new.co_flags and old.co_varnames == new.co_varnames
                    and old.co_freevars == new.co_freevars and old.co_cellvars == new.co_cellvars):
                return self.opaque
            return None
        offsets = changed_offsets(old, new)
        if offsets is None:
            return None
        mask = self.opaque
        for offset in offsets:
            for start, end, i in ranges:
                if start <= offset <= end:
                    mask |= self.pool[i]
                    break
            else:
                # An offset with no trusted observation cannot certify a test.
                return None
        return mask
