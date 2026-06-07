"""
AnkiGit — Git version control for Anki collections.

This module registers Anki hooks only when running inside Anki.
The engine/ and formats/ layers can be imported and tested independently.
"""

import tomllib
from pathlib import Path

version = "0.1.8"

_pyproject_path = Path(__file__).parent.parent / "pyproject.toml"
if _pyproject_path.exists():
    with open(_pyproject_path, "rb") as _f:
        _pyproject_version = tomllib.load(_f)["project"]["version"]
    if _pyproject_version != version:
        raise RuntimeError(
            f"Version mismatch: pyproject.toml says {_pyproject_version}, "
            f"anki_git/__init__.py says {version}"
        )


def init_addon():
    try:
        from .addon import init_addon as _init
        _init()
    except ImportError as e:
        if "aqt" not in str(e):
            import traceback
            traceback.print_exc()


init_addon()
