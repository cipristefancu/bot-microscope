
from __future__ import annotations

import ast
import os
import re
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Modules that can talk to the internet. Importing any of them, or any part
# of them (for example "requests.adapters"), fails the test.
BANNED_MODULES: frozenset[str] = frozenset(
    {
        "requests",
        "selenium",
        "playwright",
        "instaloader",
        "urllib3",
        "httpx",
        "socket",
        "aiohttp",
        "http.client",
        "urllib.request",
        "ftplib",
    }
)

# Package names that must not appear in requirements.txt. This is the first
# part of each banned module name ("http.client" -> "http").
BANNED_PACKAGES: frozenset[str] = frozenset(m.split(".")[0] for m in BANNED_MODULES)

# Folders that are not our code: installed packages, caches, git history.
SKIPPED_DIRS: frozenset[str] = frozenset(
    {".venv", "venv", "env", ".git", "__pycache__", ".pytest_cache", ".ruff_cache"}
)


# ---------------------------------------------------------------- helpers


def is_banned(module: str) -> bool:
    """True if `module` is a banned module or a part of one."""
    return any(module == b or module.startswith(b + ".") for b in BANNED_MODULES)


def find_banned_imports(source: str, filename: str = "<code>") -> list[str]:
    """Return one message per banned import found in `source`.

    Catches `import x`, `from x import y`, `from http import client`,
    and `__import__("x")` / `importlib.import_module("x")` with a fixed name.
    Code that cannot be read is reported too, because it cannot be checked.
    """
    try:
        tree = ast.parse(source, filename=filename)
    except SyntaxError as err:
        return [f"{filename}: not readable as Python ({err.msg}), so it can't be checked"]
    found: list[str] = []
    for node in ast.walk(tree):
        for module in _imported_modules(node):
            if is_banned(module):
                found.append(f"{filename}:{node.lineno}: imports banned module '{module}'")
    return found


def _imported_modules(node: ast.AST) -> list[str]:
    """List the full module names a single code element imports."""
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
        # "from http import client" imports both "http" and "http.client".
        return [node.module] + [f"{node.module}.{a.name}" for a in node.names]
    if isinstance(node, ast.Call) and _is_dynamic_import(node.func):
        first = node.args[0] if node.args else None
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            return [first.value]
    return []


def _is_dynamic_import(func: ast.expr) -> bool:
    """True for `__import__(...)` and `importlib.import_module(...)` calls."""
    if isinstance(func, ast.Name):
        return func.id in {"__import__", "import_module"}
    return isinstance(func, ast.Attribute) and func.attr == "import_module"


def find_python_files(root: Path) -> list[Path]:
    """Every .py file under `root`, skipping installed packages and caches."""
    files: list[Path] = []
    root_resolved = root.resolve()
    for folder, subfolders, filenames in os.walk(root):
        folder_path = Path(folder).resolve()
        is_root = folder_path == root_resolved
        subfolders[:] = [
            d
            for d in subfolders
            if d not in {".git", "__pycache__", ".pytest_cache", ".ruff_cache"}
            and not (is_root and (d in SKIPPED_DIRS or (folder_path / d / "pyvenv.cfg").exists()))
        ]
        files += [Path(folder) / name for name in filenames if name.endswith(".py")]
    return sorted(files)


def find_banned_requirements(text: str) -> list[str]:
    """Return one message per banned package listed in requirements text."""
    found: list[str] = []
    for number, line in enumerate(text.splitlines(), start=1):
        match = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)", line.split("#")[0])
        if not match:
            continue
        name = match.group(1).lower().replace("_", "-")
        if name in BANNED_PACKAGES:
            found.append(f"requirements.txt:{number}: lists banned package '{name}'")
    return found


def assert_project_is_clean(root: Path) -> list[Path]:
    """Fail if `root` has no .py files or any banned import / package.

    Returns the list of scanned files so callers can inspect it.
    """
    files = find_python_files(root)
    assert files, f"No .py files found under {root}; the guardrail scanned nothing."
    problems: list[str] = []
    for path in files:
        try:
            # "utf-8-sig" also reads files that Windows saved with a BOM marker.
            source = path.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError:
            problems.append(f"{path}: not UTF-8 text, so it can't be checked")
            continue
        problems += find_banned_imports(source, str(path.relative_to(root)))
    requirements = root / "requirements.txt"
    if requirements.is_file():
        problems += find_banned_requirements(requirements.read_text(encoding="utf-8-sig"))
    assert not problems, "Network/scraping code is not allowed:\n" + "\n".join(problems)
    return files


# ------------------------------------------------- the real project checks


def test_project_has_no_network_code() -> None:
    """The actual project: real files scanned, nothing banned found."""
    scanned = assert_project_is_clean(PROJECT_ROOT)
    scanned_names = {p.relative_to(PROJECT_ROOT).as_posix() for p in scanned}
    # Proof the scan reaches both our package code and our tests.
    assert "src/botaudit/config.py" in scanned_names
    assert "tests/test_guardrails.py" in scanned_names


def test_requirements_file_exists_and_is_clean() -> None:
    """requirements.txt must exist and list no banned package."""
    requirements = PROJECT_ROOT / "requirements.txt"
    assert requirements.is_file(), "requirements.txt is missing"
    assert find_banned_requirements(requirements.read_text(encoding="utf-8-sig")) == []


# ----------------------------- proof the guardrail itself is not asleep


@pytest.mark.parametrize(
    "code",
    [
        "import requests",
        "import requests.adapters",
        "import selenium.webdriver as wd",
        "from playwright.sync_api import sync_playwright",
        "import instaloader",
        "import urllib3",
        "import httpx",
        "import socket",
        "from socket import create_connection",
        "import aiohttp",
        "import http.client",
        "from http import client",
        "from http.client import HTTPSConnection",
        "import urllib.request",
        "from urllib import request",
        "from urllib.request import urlopen",
        "import ftplib",
        "import os, requests",
        "def f():\n    import requests",
        "try:\n    import httpx\nexcept ImportError:\n    pass",
        "__import__('requests')",
        "import importlib\nimportlib.import_module('socket')",
    ],
)
def test_detector_catches_banned_import(code: str) -> None:
    """Every way of importing a banned module is caught."""
    assert find_banned_imports(code), f"Not caught: {code!r}"


@pytest.mark.parametrize(
    "code",
    [
        "import pandas",
        "import matplotlib.pyplot as plt",
        "import yaml",
        "import os, re, ast",
        "from pathlib import Path",
        "import requests_like_name_but_different",
        "import urllib.parse",
        "from http import HTTPStatus",
        "# import requests  (only a comment)",
        "text = 'import requests'  # only a string",
        "from . import socket",  # our own local file, not the network module
    ],
)
def test_detector_allows_safe_code(code: str) -> None:
    """Safe imports, comments and text strings are NOT flagged."""
    assert find_banned_imports(code) == [], f"Wrongly flagged: {code!r}"


def test_unreadable_python_file_is_reported() -> None:
    """A file that isn't valid Python can't be checked, so it fails."""
    assert find_banned_imports("def broken(:\n", "bad.py")


def test_empty_folder_does_not_pass(tmp_path: Path) -> None:
    """With no .py files to scan, the guardrail fails instead of passing."""
    with pytest.raises(AssertionError, match="scanned nothing"):
        assert_project_is_clean(tmp_path)


def test_banned_import_in_a_project_folder_fails(tmp_path: Path) -> None:
    """A banned import hidden in a sub-folder makes the scan fail."""
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "ok.py").write_text("import pandas\n", encoding="utf-8")
    (tmp_path / "pkg" / "sneaky.py").write_text("x = 1\nimport requests\n", encoding="utf-8")
    with pytest.raises(AssertionError, match=r"sneaky\.py:2"):
        assert_project_is_clean(tmp_path)


def test_banned_import_in_windows_bom_file_is_caught(tmp_path: Path) -> None:
    """A file saved by Windows with a BOM marker is still read and checked."""
    (tmp_path / "bom.py").write_text("import requests\n", encoding="utf-8-sig")
    with pytest.raises(AssertionError, match=r"bom\.py:1: imports banned module 'requests'"):
        assert_project_is_clean(tmp_path)


def test_non_utf8_file_is_reported(tmp_path: Path) -> None:
    """A file that isn't readable text can't be checked, so it fails."""
    (tmp_path / "odd.py").write_bytes(b"\xff\xfe\x00import requests")
    with pytest.raises(AssertionError, match="not UTF-8 text"):
        assert_project_is_clean(tmp_path)


def test_banned_package_in_requirements_fails(tmp_path: Path) -> None:
    """A banned package in requirements.txt makes the scan fail."""
    (tmp_path / "main.py").write_text("import pandas\n", encoding="utf-8")
    (tmp_path / "requirements.txt").write_text("pandas==3.1.0\nrequests==2.0\n", encoding="utf-8")
    with pytest.raises(AssertionError, match="requirements.txt:2"):
        assert_project_is_clean(tmp_path)


def test_installed_packages_folder_is_skipped(tmp_path: Path) -> None:
    """Files inside a virtual environment are third-party, not our code."""
    (tmp_path / "main.py").write_text("import pandas\n", encoding="utf-8")
    venv = tmp_path / "myenv"
    venv.mkdir()
    (venv / "pyvenv.cfg").write_text("home = x\n", encoding="utf-8")
    (venv / "lib.py").write_text("import requests\n", encoding="utf-8")
    assert assert_project_is_clean(tmp_path) == [tmp_path / "main.py"]


def test_subfolder_named_env_is_not_skipped(tmp_path: Path) -> None:
    """A subfolder inside our package (e.g. src/pkg/env/) is not skipped."""
    (tmp_path / "main.py").write_text("import pandas\n", encoding="utf-8")
    sub = tmp_path / "pkg" / "env"
    sub.mkdir(parents=True)
    (sub / "bad.py").write_text("import requests\n", encoding="utf-8")
    with pytest.raises(AssertionError, match="bad.py:1"):
        assert_project_is_clean(tmp_path)


@pytest.mark.parametrize(
    "line",
    [
        "requests==2.32.3",
        "Requests>=2",
        "selenium",
        "urllib3 ; python_version >= '3.9'",
        "HTTPX==0.27.0",
        "aiohttp[speedups]==3.9.0",
        "instaloader==4.10",
        "playwright==1.45.0",
    ],
)
def test_requirements_detector_catches_banned_package(line: str) -> None:
    """Banned packages are caught in requirements.txt in any spelling."""
    assert find_banned_requirements(line), f"Not caught: {line!r}"


def test_requirements_detector_allows_safe_packages() -> None:
    """Allowed packages and comments are not flagged."""
    text = "# requests is banned\npandas==3.1.0\nJinja2==3.1.6\n\nPyYAML==6.0.3\n"
    assert find_banned_requirements(text) == []
