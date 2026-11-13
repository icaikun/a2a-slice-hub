"""Portable repository-relative ownership boundaries (not platform-dependent globs)."""
from pathlib import PurePosixPath
import re

from .errors import GateRejected

CONTROL_NAMES = {
    "conftest.py", "pytest.ini", "pyproject.toml", "setup.cfg", "setup.py", "tox.ini",
    "uv.lock", "poetry.lock", "pipfile", "pipfile.lock", ".gitattributes", ".gitmodules",
    "sitecustomize.py", "usercustomize.py", "requirements.txt", "requirements-dev.txt",
}
CONTROL_ROOTS = {".github", ".agents", ".codex", ".hub", "scripts", "slices", "migrations"}
RESERVED = {"con", "prn", "aux", "nul"} | {f"{x}{i}" for x in ("com", "lpt") for i in range(1, 10)}


def safe_path(value):
    if not isinstance(value, str) or not value or "\\" in value or ":" in value:
        raise GateRejected("non-portable repository path")
    parts = value.split("/")
    if PurePosixPath(value).is_absolute() or any(
        p in {"", ".", ".."} or p.rstrip(" .") != p or p.casefold() == ".git"
        or p.split(".")[0].casefold() in RESERVED or re.search(r'[<>"|?*\x00-\x1f]', p)
        for p in parts
    ):
        raise GateRejected("unsafe repository path")
    return value


def inside(name, prefix):
    return name == prefix or name.startswith(prefix + "/")


def protected(name):
    parts = name.casefold().split("/")
    return (parts[0] == "tests" and (len(parts) < 2 or parts[1] != "unit")) or parts[0] in CONTROL_ROOTS or any(p in CONTROL_NAMES for p in parts) or parts[-1].endswith(".pth")


def enforce(name, allowed):
    safe_path(name)
    if protected(name) or not any(inside(name, safe_path(p)) for p in allowed):
        raise GateRejected("protected or foreign-slice path: " + name)
