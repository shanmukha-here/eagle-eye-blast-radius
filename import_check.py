"""Cold-import a structurally verified literal edit and the built-in dialect registry."""

import importlib
import importlib.abc
import importlib.util
import sys
from pathlib import Path


class PatchedModule(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    def __init__(self, module, filename, source):
        self.module = module
        self.filename = filename
        self.source = source

    def find_spec(self, fullname, path, target=None):
        if fullname == self.module:
            options = {}
            if Path(self.filename).name == "__init__.py":
                options["submodule_search_locations"] = [str(Path(self.filename).parent)]
            return importlib.util.spec_from_file_location(fullname, self.filename, loader=self, **options)
        return None

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        exec(compile(self.source, self.filename, "exec"), module.__dict__)


if __name__ == "__main__":
    root, path = sys.argv[1:]
    module = path[:-3].replace("/", ".").removesuffix(".__init__")
    source = sys.stdin.buffer.read()
    filename = str(Path(root) / path)
    sys.meta_path.insert(0, PatchedModule(module, filename, source))
    sys.path.insert(0, root)
    from sqlglot.dialects import Dialect

    importlib.import_module(module)
    # Force lazy registration and construction of inherited dialect classes.
    list(Dialect.classes.values())
