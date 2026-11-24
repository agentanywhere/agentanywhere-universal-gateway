#!/usr/bin/env python3
"""Check that the documented environment variables match the ones the code reads.

The gateway reads environment variables in two ways:

1. Every field of ``Settings`` in ``app/config.py`` (pydantic-settings, no
   prefix, case-insensitive), so the variable name is the field name in
   upper case.
2. Direct reads in ``app/`` and ``admin/``: ``os.environ.get(...)``,
   ``os.environ[...]``, ``os.getenv(...)``, ``env.get(...)`` and the CLI's
   ``_first_env(...)`` helper.

The documented list is every variable named in the first column of a table
row between the ``<!-- env-vars:start -->`` and ``<!-- env-vars:end -->``
markers in ``docs/reference/configuration.md``.

The script fails (exit 1) if a variable is read but not documented, or
documented but not read. It also fails if ``env.example`` sets a variable
twice, or puts a comment after a value: ``docker run --env-file`` (unlike
python-dotenv) keeps such a comment as part of the value. It also prints, for information only, the names in
``env.example`` that the gateway does not read.

Standard library only. Usage::

    python scripts/check_env_vars.py
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "app" / "config.py"
DOC = ROOT / "docs" / "reference" / "configuration.md"
ENV_EXAMPLE = ROOT / "env.example"
SCAN_DIRS = [ROOT / "app", ROOT / "admin"]

NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
ROW_RE = re.compile(r"^\|\s*`([A-Z][A-Z0-9_]*)`")


def settings_fields() -> set[str]:
    tree = ast.parse(CONFIG.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "Settings":
            names = set()
            for item in node.body:
                if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                    if item.target.id == "model_config":
                        continue
                    names.add(item.target.id.upper())
            return names
    raise SystemExit("Settings class not found in app/config.py")


def _module_constants(tree: ast.Module) -> dict[str, str]:
    consts = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            if isinstance(node.value.value, str):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        consts[target.id] = node.value.value
    return consts


def _is_env_get(func: ast.AST) -> bool:
    # os.environ.get / env.get / environ.get
    if isinstance(func, ast.Attribute) and func.attr == "get":
        value = func.value
        if isinstance(value, ast.Attribute) and value.attr == "environ":
            return True
        if isinstance(value, ast.Name) and value.id in {"env", "environ"}:
            return True
    # os.getenv
    if isinstance(func, ast.Attribute) and func.attr == "getenv":
        return True
    # CLI helper
    if isinstance(func, ast.Name) and func.id == "_first_env":
        return True
    return False


def direct_reads() -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for base in SCAN_DIRS:
        for path in sorted(base.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            consts = _module_constants(tree)
            rel = str(path.relative_to(ROOT))
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Call) and _is_env_get(node.func):
                    args = node.args if (isinstance(node.func, ast.Name)) else node.args[:1]
                    for arg in args:
                        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                            names.append(arg.value)
                        elif isinstance(arg, ast.Name) and arg.id in consts:
                            names.append(consts[arg.id])
                elif isinstance(node, ast.Subscript):
                    v = node.value
                    if isinstance(v, ast.Attribute) and v.attr == "environ":
                        s = node.slice
                        if isinstance(s, ast.Constant) and isinstance(s.value, str):
                            names.append(s.value)
                for name in names:
                    if NAME_RE.match(name):
                        found.setdefault(name, set()).add(rel)
    return found


def documented() -> set[str]:
    text = DOC.read_text(encoding="utf-8")
    try:
        section = text.split("<!-- env-vars:start -->", 1)[1].split("<!-- env-vars:end -->", 1)[0]
    except IndexError:
        raise SystemExit(f"{DOC.relative_to(ROOT)}: env-vars markers not found")
    names = set()
    for line in section.splitlines():
        m = ROW_RE.match(line.strip())
        if m:
            names.add(m.group(1))
    return names


def env_example_names() -> set[str]:
    names = set()
    for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("#"):
            line = line.lstrip("#").strip()
        m = re.match(r"^([A-Z][A-Z0-9_]*)=", line)
        if m:
            names.add(m.group(1))
    return names


def env_example_problems() -> list[str]:
    """Lines of env.example that a plain env-file reader would get wrong."""
    problems, seen = [], {}
    for number, line in enumerate(ENV_EXAMPLE.read_text(encoding="utf-8").splitlines(), 1):
        m = re.match(r"^([A-Z][A-Z0-9_]*)=(.*)$", line)
        if not m:
            continue
        name, value = m.groups()
        if re.search(r"\s#", value):
            problems.append(f"line {number}: {name} has a comment after its value; put it on the line above")
        if name in seen:
            problems.append(f"line {number}: {name} is already set on line {seen[name]}")
        seen.setdefault(name, number)
    return problems


def main() -> int:
    fields = settings_fields()
    reads = direct_reads()
    in_code = fields | set(reads)
    in_docs = documented()

    missing = sorted(in_code - in_docs)
    extra = sorted(in_docs - in_code)

    print(f"Settings fields: {len(fields)}; direct reads: {len(reads)}; "
          f"total read by code: {len(in_code)}; documented: {len(in_docs)}")
    for name in sorted(reads):
        tag = " (also a Settings field)" if name in fields else ""
        print(f"  direct read: {name} in {', '.join(sorted(reads[name]))}{tag}")

    not_read = sorted(env_example_names() - in_code)
    if not_read:
        print("Info: in env.example but not read by the gateway: " + ", ".join(not_read))

    ok = True
    if missing:
        ok = False
        print("FAIL: read by the code but not documented in docs/reference/configuration.md:")
        for name in missing:
            print(f"  {name}")
    if extra:
        ok = False
        print("FAIL: documented in docs/reference/configuration.md but not read by the code:")
        for name in extra:
            print(f"  {name}")
    problems = env_example_problems()
    if problems:
        ok = False
        print("FAIL: env.example lines that `docker run --env-file` would read differently:")
        for problem in problems:
            print(f"  {problem}")
    if ok:
        print("OK: documented environment variables match the code.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
