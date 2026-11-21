"""Identify edits whose effect must be observed through a traced configuration read."""
import ast
from functools import lru_cache

from dependencies import scalar_declarations


@lru_cache(maxsize=64)
def plain_enums(source):
    classes = {}
    for cls in ast.parse(source).body:
        if (not isinstance(cls, ast.ClassDef) or cls.decorator_list or cls.keywords
                or len(cls.bases) != 1 or not isinstance(cls.bases[0], ast.Name)
                or cls.bases[0].id != "AutoName"):
            continue
        members = {}
        for stmt in cls.body:
            if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant):
                continue
            if isinstance(stmt, ast.FunctionDef) and not stmt.name.startswith("_") and not stmt.decorator_list:
                continue
            if not (isinstance(stmt, ast.Assign) and len(stmt.targets) == 1
                    and isinstance(stmt.targets[0], ast.Name) and stmt.targets[0].id.isupper()):
                break
            name, expr = stmt.targets[0].id, stmt.value
            if isinstance(expr, ast.Constant) and type(expr.value) is str:
                members[name] = expr.value
            elif (isinstance(expr, ast.Call) and isinstance(expr.func, ast.Name)
                  and expr.func.id == "auto" and not expr.args and not expr.keywords):
                members[name] = name
            else:
                break
        else:
            if members and len(set(members.values())) == len(members):
                classes[cls.name] = members
    return classes


def enum_edit(source, modified):
    """Allow auto() to a string only for a single verified member replacement."""
    before, after = plain_enums(source), plain_enums(modified)
    if before.keys() != after.keys():
        return []
    changes = [(owner, field) for owner, members in before.items()
               for field in members if after[owner].get(field) != members[field]]
    if len(changes) != 1:
        return []
    owner, field = changes[0]

    def erase(text):
        tree = ast.parse(text)
        for cls in tree.body:
            if isinstance(cls, ast.ClassDef) and cls.name == owner:
                for stmt in cls.body:
                    if (isinstance(stmt, ast.Assign) and len(stmt.targets) == 1
                            and isinstance(stmt.targets[0], ast.Name) and stmt.targets[0].id == field):
                        stmt.value = ast.Constant(None)
        return ast.dump(tree)
    return changes if erase(source) == erase(modified) else []


class Observers:
    def __init__(self, data):
        self.events = {k: int(v, 16) for k, v in data.items()}
        self.none_safe = {k[6:] for k in data if k.startswith("guard:")}

    def users(self, path, owner, field, source, modified):
        prefix = f"component:{path}:{owner}:"
        if (prefix + "defined:" + field in self.events
                and (owner, field) in scalar_declarations(source)):
            return self.events.get(prefix + field, 0)

        prefix = f"enum:{path}:{owner}:"
        if prefix + "defined" not in self.events:
            return None
        before = plain_enums(source).get(owner, {})
        after = plain_enums(modified).get(owner, {})
        if field not in before or before.keys() != after.keys():
            return None
        if any(before[k] != after[k] for k in before if k != field):
            return None
        return (self.events.get(prefix + "unknown", 0)
                | self.events.get(prefix + "value:" + field, 0)
                | self.events.get(prefix + "lookup:" + before[field], 0)
                | self.events.get(prefix + "lookup:" + after[field], 0))
