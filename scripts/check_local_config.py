#!/usr/bin/env python
"""Exige que la identidad del usuario se propague a Postgres con scope de transaccion.

El backend le dice a Postgres quien es el usuario actual seteando
`request.jwt.claims`, que es lo que lee `auth.uid()` en las politicas de RLS.

Ese seteo TIENE que ser local a la transaccion, porque las conexiones se reusan
desde un pool. Si sobrevive al COMMIT, la proxima request que tome esa conexion
hereda la identidad del usuario anterior y ve sus datos. Es la peor falla posible
en este proyecto, es silenciosa, y no aparece en desarrollo con un solo usuario.

Este guarda prohibe, en codigo ejecutable:
  - SET SESSION / SET ROLE            (persisten en la conexion)
  - set_config con is_local falso     (idem)
  - set_config con 2 argumentos       (el default de is_local es false)

Los docstrings se excluyen del analisis: este archivo mismo nombra los patrones
prohibidos para explicarlos, y hablar de un patron no es usarlo.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "app"

PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"\bSET\s+SESSION\b", re.IGNORECASE),
        "SET SESSION persiste en la conexion del pool",
    ),
    (
        re.compile(r"\bSET\s+ROLE\b", re.IGNORECASE),
        "SET ROLE persiste; usar set_config('role', ..., true)",
    ),
    (
        re.compile(r"set_config\s*\([^;]*,\s*(?:false|FALSE|'f')\s*\)"),
        "set_config con is_local falso persiste en la conexion",
    ),
    # set_config('k','v') sin el tercer argumento: is_local default = false.
    (
        re.compile(r"set_config\s*\(\s*[^,()]+,\s*[^,()]+\s*\)"),
        "set_config sin el tercer argumento: is_local default es false",
    ),
)


def _docstring_lines(tree: ast.Module) -> set[int]:
    """Numeros de linea (1-indexed) ocupados por docstrings.

    Un docstring es un string suelto como primera sentencia de un modulo, clase o
    funcion. Se excluye del analisis para que documentar un antipatron no cuente
    como cometerlo.
    """
    lines: set[int] = set()
    holders = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)

    for node in ast.walk(tree):
        if not isinstance(node, holders):
            continue
        body = getattr(node, "body", [])
        if not body:
            continue
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
            and first.end_lineno is not None
        ):
            lines.update(range(first.lineno, first.end_lineno + 1))
    return lines


def main() -> int:
    errors: list[str] = []

    for path in sorted(APP.rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        source = path.read_text(encoding="utf-8")

        try:
            skip = _docstring_lines(ast.parse(source, filename=rel))
        except SyntaxError as exc:
            errors.append(f"{rel}: no se pudo parsear ({exc})")
            continue

        for lineno, line in enumerate(source.splitlines(), 1):
            if lineno in skip or line.lstrip().startswith("#"):
                continue
            for pattern, why in PATTERNS:
                if pattern.search(line):
                    errors.append(f"{rel}:{lineno}: {why}\n      {line.strip()}")

    if not errors:
        return 0

    print("ERROR: la identidad del usuario debe setearse con scope de transaccion\n")
    for err in errors:
        print(f"  - {err}")
    print()
    print("Forma correcta (ver app/infra/db.py):")
    print("    select set_config('role', :role, true),")
    print("           set_config('request.jwt.claims', :claims, true)")
    return 1


if __name__ == "__main__":
    sys.exit(main())
