"""Root conftest — ensures the installed `deile` package wins over the local `deile/` stub.

The deile-bot repo ships a `deile/` directory containing only YAML config files
(no Python). When pytest collects from this repo, that directory becomes a
namespace package on sys.path and *shadows* the real `deile` package installed
via pip. Tests that import e.g. `deile.config.settings` then fail with
`ModuleNotFoundError`.

This conftest forces resolution of `deile.*` to the installed package by
removing the local namespace path entry as soon as Python touches `deile`.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path


def _force_installed_deile() -> None:
    here = Path(__file__).resolve().parent
    local_stub = here / "deile"
    if not local_stub.is_dir():
        return
    # If `deile` is already imported as a NamespacePackage pointing at the local
    # stub, evict it so the next import resolves to the real installed package.
    mod = sys.modules.get("deile")
    if mod is not None and getattr(mod, "__file__", None) is None:
        # Try to resolve the installed package by temporarily hiding the stub.
        spec = importlib.util.find_spec("deile")
        if spec is None or (spec.submodule_search_locations and
                            str(local_stub) in list(spec.submodule_search_locations)):
            del sys.modules["deile"]
    # Try a clean import — if it succeeds and points at a real __init__.py we
    # are done; otherwise leave Python's namespace machinery alone.
    try:
        mod = importlib.import_module("deile")
        if getattr(mod, "__file__", None) is None:
            # Still a namespace package; trim the local stub from __path__.
            if hasattr(mod, "__path__"):
                mod.__path__ = [p for p in mod.__path__ if Path(p).resolve() != local_stub]
    except Exception:
        pass


_force_installed_deile()
