"""Observe primitive predicate inputs, literal defaults and actual caller keywords."""

import ast
import builtins
import dis
import json
import os
import sys
import zlib
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from functools import lru_cache
from importlib.machinery import SourceFileLoader
from multiprocessing.util import Finalize
from pathlib import Path
from threading import local
from types import FunctionType

import pytest

from value_observations import COMPARE_BITS, COMPARISONS, EXPRESSION_FIELDS, SCALARS, inputs, key

ROOT = str(Path.cwd()) + "/"
QUERY_INPUTS = os.getenv("TRACE_QUERY_INPUTS") == "1"
TYPE_EQUALITY = os.getenv("TRACE_TYPE_EQUALITY") == "1"
TRACKED = ROOT + ("sqlglot/executor/" if QUERY_INPUTS else "sqlglot/")
original_load = SourceFileLoader.get_code
original_exec = SourceFileLoader.exec_module
original_submit = ProcessPoolExecutor.submit
monitor = sys.monitoring
tool = 5
current = "*"
configured = False
child_exit_registered = False
catalog = {}
records = defaultdict(lambda: defaultdict(set))
defaults = defaultdict(set)
test_names = []
max_samples = 64
call_context = local()


def literal_default(node):
    return isinstance(node, ast.Constant) and type(node.value) in SCALARS


class Trace(ast.NodeTransformer):
    def __init__(self, filename):
        self.filename = filename
        self.owner = None

    def visit_ClassDef(self, node):
        old, self.owner = self.owner, node
        self.generic_visit(node)
        self.owner = old
        return node

    def visit_FunctionDef(self, node):
        if self.owner is None and not node.decorator_list:
            args = node.args
            opt = (args.posonlyargs + args.args)[-len(args.defaults):] if args.defaults else []
            candidates = [(n.arg, v.value) for n, v in (
                [*zip(opt, args.defaults), *zip(args.kwonlyargs, args.kw_defaults)])
                          if literal_default(v)]
            if candidates:
                catalog[self.filename, node.lineno, node.name] = candidates
        old, self.owner = self.owner, node
        self.generic_visit(node)
        self.owner = old
        return node

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Compare(self, node):
        if not TYPE_EQUALITY:
            return self.generic_visit(node)
        self.generic_visit(node)
        if len(node.ops) != 1 or type(node.ops[0]) not in COMPARISONS:
            return node
        operands = (node.left, node.comparators[0])
        if not any(isinstance(x, (ast.Constant, ast.Tuple)) or
                   isinstance(x, ast.Attribute) and x.attr[:1].isupper() for x in operands):
            return node
        func = ast.Name(id="_observe_clean_compare", ctx=ast.Load())
        args = [ast.Constant(key(self.filename, node) + ":comparison"),
                ast.Constant(type(node.ops[0]).__name__), *operands]
        return ast.copy_location(ast.Call(func=func, args=args, keywords=[]), node)

    def generic_visit(self, node):
        if TYPE_EQUALITY:
            return super().generic_visit(node)
        field = EXPRESSION_FIELDS.get(type(node))
        if not field or not isinstance(self.owner, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return super().generic_visit(node)
        expr = getattr(node, field)
        names = inputs(expr)
        super().generic_visit(node)
        if names is not None:
            func = ast.Name(id="_observe_clean_scalar_inputs", ctx=ast.Load())
            args = [ast.Constant(key(self.filename, expr)), ast.Constant(names)]
            call = ast.Call(func=func, args=args, keywords=[])
            new = ast.BoolOp(op=ast.Or(), values=[ast.copy_location(call, expr), getattr(node, field)])
            setattr(node, field, ast.copy_location(new, expr))
        return node


EVALUATE_COMPARE = {kind.__name__: callback for kind, callback in COMPARISONS.items()}


def exact_operand(value):
    return type(value) in SCALARS or (
        type(value) is tuple and len(value) <= 16 and all(type(x) in SCALARS for x in value)
    )


def observe_compare(site, op, left, right):
    known = true = 0
    if exact_operand(left) and exact_operand(right):
        candidates = COMPARISONS
    elif type(left) is type(right) is type:
        candidates = {kind: COMPARISONS[kind] for kind in (ast.Is, ast.IsNot, ast.Eq, ast.NotEq)}
    else:
        candidates = {}
    for kind, compare in candidates.items():
        try:
            if compare(left, right):
                true |= COMPARE_BITS[kind]
            known |= COMPARE_BITS[kind]
        except TypeError:
            pass
    records[site][current].add((("int", known), ("int", true)))
    return EVALUATE_COMPARE[op](left, right)


def observe(site, names):
    row = records[site][current]
    if None in row:
        return
    frame = sys._getframe(1)
    local = frame.f_locals
    code = frame.f_code
    bound_names = (*code.co_varnames, *code.co_cellvars, *code.co_freevars)
    values = tuple(local.get(name, ...) if name in bound_names else ... for name in names)
    if (len(row) >= max_samples or any(type(value) not in SCALARS or
        isinstance(value, str) and len(value) > 160 or
        isinstance(value, int) and value.bit_length() > 100 for value in values)):
        row.clear()
        row.add(None)
    else:
        row.add(tuple((type(value).__name__, value) for value in values))


@lru_cache(maxsize=4096)
def keyword_calls(code):
    result = {}
    previous = None
    for op in dis.get_instructions(code):
        if op.opname == "CALL_KW":
            if previous and previous.opname == "LOAD_CONST" and type(previous.argval) is tuple:
                result[op.offset] = previous.argval
        previous = op
    return result


def called(code, offset, function, arg0):
    if code.co_code[offset] != dis.opmap["CALL_KW"]:
        return monitor.DISABLE
    keywords = keyword_calls(code).get(offset)
    if not keywords:
        return monitor.DISABLE
    target = function.__code__ if type(function) is FunctionType else None
    frame = sys._getframe(1)
    call_context.previous = (id(frame), offset, target, keywords)


def default_audit(event, args):
    if event not in ("object.__getattr__", "object.__setattr__", "object.__delattr__"):
        return
    function, name = args[:2]
    if type(function) is not FunctionType or name not in ("__defaults__", "__kwdefaults__"):
        return
    code = function.__code__
    if code.co_filename.startswith(ROOT + "sqlglot/"):
        where = (code.co_filename[len(ROOT):], code.co_firstlineno, code.co_qualname)
        for argument, _ in catalog.get(where, ()):
            defaults[":".join(map(str, (*where, argument)))].add(current)


def entered(code, offset):
    if not code.co_filename.startswith(ROOT + "sqlglot/"):
        return monitor.DISABLE
    where = (code.co_filename[len(ROOT):], code.co_firstlineno, code.co_qualname)
    arguments = catalog.get(where)
    if not arguments:
        return monitor.DISABLE
    frame = sys._getframe(1)
    caller = frame.f_back
    call = getattr(call_context, "previous", None)
    keywords = call[3] if call and caller and call[:3] == (id(caller), caller.f_lasti, code) else ()
    bound = frame.f_locals
    for name, default in arguments:
        value = bound.get(name, ...)
        if name not in keywords and type(value) is type(default) and value == default:
            defaults[":".join(map(str, (*where, name)))].add(current)


def get_code(loader, fullname):
    filename = loader.get_filename(fullname)
    if not filename.startswith(TRACKED):
        return original_load(loader, fullname)
    tree = ast.parse(Path(filename).read_bytes(), filename)
    tree = Trace(filename[len(ROOT):]).visit(tree)
    return compile(ast.fix_missing_locations(tree), filename, "exec", dont_inherit=True)


def exec_module(loader, module):
    global current
    if not loader.path.startswith(ROOT + "sqlglot/"):
        return original_exec(loader, module)
    previous, current = current, "*"
    try:
        return original_exec(loader, module)
    finally:
        current = previous


def install():
    global configured
    if configured:
        return
    configured = True
    SourceFileLoader.get_code = get_code
    SourceFileLoader.exec_module = exec_module
    if TYPE_EQUALITY:
        builtins._observe_clean_compare = observe_compare
        return
    builtins._observe_clean_scalar_inputs = observe
    sys.addaudithook(default_audit)
    monitor.use_tool_id(tool, "default-and-scalar-observations")
    monitor.register_callback(tool, monitor.events.PY_START, entered)
    monitor.register_callback(tool, monitor.events.CALL, called)
    monitor.set_events(tool, monitor.events.PY_START | monitor.events.CALL)


def save():
    if not TYPE_EQUALITY:
        monitor.set_events(tool, 0)
    reported = {site: {owner: list(samples) for owner, samples in owners.items()}
                for site, owners in records.items()}
    payload = {"pid": os.getpid(), "items": test_names, "values": reported,
               "defaults": {site: sorted(owners) for site, owners in defaults.items()},
               "catalog": [":".join(map(str, (*where, name))) for where, args in catalog.items()
                           for name, _ in args]}
    out = Path(os.environ["TRACE_OUTPUT"])
    (out / f"value-trace-{os.getpid()}.z").write_bytes(zlib.compress(json.dumps(payload).encode()))


def work(owner, fn, args, kwargs):
    global current, child_exit_registered
    install()
    if not child_exit_registered:
        child_exit_registered = True
        Finalize(None, save, exitpriority=1)
    current = owner
    if (QUERY_INPUTS and fn.__module__ == "tests.test_executor"
            and fn.__name__ == "mp_execute" and args[1].get("execute")):
        from checkpoint import digest

        current = "case:" + digest(args[0])
    return fn(*args, **kwargs)


def submit(pool, fn, /, *args, **kwargs):
    return original_submit(pool, work, current, fn, args, kwargs)


# Register before loading test modules, including plugins that import SQLGlot.
def pytest_configure(config):
    install()
    ProcessPoolExecutor.submit = submit


def pytest_collection_finish(session):
    test_names.extend(item.nodeid for item in session.items)


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item):
    global current
    current = item.nodeid.split("::", 1)[0]


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_call(item):
    global current
    current = item.nodeid


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_teardown(item, nextitem):
    global current
    current = item.nodeid.split("::", 1)[0]


def pytest_sessionfinish(session, exitstatus):
    if exitstatus == 0:
        save()
