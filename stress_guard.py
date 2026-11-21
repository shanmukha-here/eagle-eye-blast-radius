"""Bound parser replay to edits whose changed operation cannot inspect input size."""
import ast
import copy

MATCHERS = {"_match", "_match_set", "_match_pair", "_match_next"}


def token_pattern(node):
    """Erase only token operands of a parser match or direct token-type comparison."""
    changes = []
    class Tokens(ast.NodeTransformer):
        def visit_Attribute(self, attr):
            if isinstance(attr.value, ast.Name) and attr.value.id == "TokenType":
                changes.append(attr.attr)
                return ast.Name(id="_token_operand", ctx=ast.Load())
            return attr
    def bare(node):
        return isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "TokenType"
    def local(n):
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and isinstance(n.func.value, ast.Name) and n.func.value.id == "self"
                and n.func.attr in MATCHERS and not n.keywords and all(bare(arg) for arg in n.args)):
            return True
        return isinstance(n, ast.Compare) and isinstance(n.left, ast.Attribute) and n.left.attr == "token_type" and all(bare(arg) for arg in n.comparators)
    class Pattern(ast.NodeTransformer):
        def generic_visit(self, n):
            if local(n):
                return Tokens().visit(n)
            return super().generic_visit(n)
    # The impact map caches its ASTs. Never mutate those shared objects.
    return ast.dump(Pattern().visit(copy.deepcopy(node))), changes


def eligible(path, old, new):
    if path != "sqlglot/parser.py":
        return False
    sensitive = {"_node_count", "max_nodes", "_max_nodes", "raise_error", "recursion_limit"}
    if any(isinstance(n, ast.Attribute) and n.attr in sensitive or
           isinstance(n, ast.Name) and n.id in sensitive for n in ast.walk(old)):
        return False
    a, before = token_pattern(old)
    b, after = token_pattern(new)
    pairs = [(x, y) for x, y in zip(before, after) if x != y]
    return a == b and len(before) == len(after) and len(pairs) == 1


def same_full_predicates(old, new, profile):
    """Require the edit to preserve every token match in the original stress run."""
    def calls(tree):
        return [n for n in ast.walk(tree) if isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute) and isinstance(n.func.value, ast.Name)
                and n.func.value.id == "self" and n.func.attr in ("_match", "_match_pair")
                and not n.keywords and all(isinstance(a, ast.Attribute) and isinstance(a.value, ast.Name)
                                           and a.value.id == "TokenType" for a in n.args)]
    changes = [(a, b) for a, b in zip(calls(old), calls(new)) if ast.dump(a) != ast.dump(b)]
    if len(changes) != 1:
        return False
    a, b = changes[0]
    before, after = [n.attr for n in a.args], [n.attr for n in b.args]
    if len(before) != (1 if a.func.attr == "_match" else 2):
        return False
    if any(name not in profile["members"] for name in before + after):
        return False
    key = f"{a.lineno}:{a.func.attr}"
    # Require a positive call-site witness. An absent record proves nothing here.
    observed = profile["calls"].get(key)
    return bool(observed) and all((tokens == before) == (tokens == after) for tokens in observed)
