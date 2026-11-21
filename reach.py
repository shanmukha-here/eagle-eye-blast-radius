"""Find the executable code object and observed branch for an isolated body edit."""
import dis
import marshal
from functools import lru_cache
from types import CodeType


def nested_edit(old, new):
    """Descend through identical function factories, including closure creation."""
    while True:
        if (old.co_code != new.co_code or old.co_names != new.co_names
                or old.co_flags != new.co_flags or old.co_varnames != new.co_varnames
                or old.co_exceptiontable != new.co_exceptiontable
                or old.co_freevars != new.co_freevars or old.co_cellvars != new.co_cellvars
                or len(old.co_consts) != len(new.co_consts)):
            return old, new
        children = []
        for a, b in zip(old.co_consts, new.co_consts):
            if isinstance(a, CodeType) and isinstance(b, CodeType):
                if a.co_qualname != b.co_qualname:
                    return old, new
                if marshal.dumps(a) != marshal.dumps(b):
                    children.append((a, b))
            elif type(a) is not type(b) or marshal.dumps(a, 2) != marshal.dumps(b, 2):
                return old, new
        if len(children) != 1:
            return old, new
        child, replacement = children[0]
        # A docstring or signature change is visible when the function is created.
        if (child.co_argcount, child.co_posonlyargcount, child.co_kwonlyargcount,
            child.co_flags, child.co_freevars, child.co_cellvars, child.co_consts[:1]) != (
            replacement.co_argcount, replacement.co_posonlyargcount, replacement.co_kwonlyargcount,
            replacement.co_flags, replacement.co_freevars, replacement.co_cellvars, replacement.co_consts[:1]):
            return old, new
        old, new = child, replacement


@lru_cache(maxsize=128)
def predecessors(code):
    instructions = list(dis.get_instructions(code))
    edges = {i.offset: [] for i in instructions}
    lines = dict(dis.findlinestarts(code))
    jumps = getattr(dis, "hasjump", None) or set(dis.hasjabs) | set(dis.hasjrel)
    for i, following in zip(instructions, instructions[1:]):
        if i.opcode in jumps:
            if i.opname.startswith(("JUMP_FORWARD", "JUMP_BACKWARD", "JUMP_ABSOLUTE")):
                edges[i.argval].append((i.offset, None))
                continue
            edges[i.argval].append((i.offset, following.offset))
            edges[following.offset].append((i.offset, i.argval))
        elif i.opname not in ("RETURN_CONST", "RETURN_VALUE", "RAISE_VARARGS", "RERAISE"):
            edges[following.offset].append((i.offset, None))
    return edges, lines


def witnessed_bypass(code, offset, observed, line_users):
    """Require an executed alternative and an independent executable line on the skipped arm."""
    if code.co_exceptiontable:
        return None
    edges, lines = predecessors(code)
    visited = set()
    witnesses = []
    while offset not in visited:
        visited.add(offset)
        if offset in lines and lines[offset] is not None:
            witnesses.append(line_users.get(lines[offset], 0))
        incoming = edges.get(offset, ())
        if len(incoming) != 1:
            return None
        source, other = incoming[0]
        if other is not None:
            if other == offset or not observed(source) or not observed(other) or not witnesses:
                return None
            # Any test seen on the path in the second clean profiling run stays selected.
            mask = 0
            for tests in witnesses:
                mask |= tests
            return mask
        offset = source
    return None
