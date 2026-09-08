#!/usr/bin/env python
"""Acota el uso de la service_role key de Supabase a una lista blanca de modulos.

La service_role key se conecta con un rol que tiene BYPASSRLS: las politicas de
Row Level Security no se evaluan. Si el codigo que lee datos financieros la usa,
el aislamiento entre usuarios deja de estar garantizado por Postgres y pasa a
depender de que cada query recuerde filtrar por user_id.

Inventario cerrado de usos legitimos (ver plan, seccion 1):
  1. Invitaciones y alta de usuarios  -> solo toca auth.users
  2. Seed de categorias del sistema   -> corre en CLI, no en un request
  3. Reaper de jobs huerfanos         -> solo processing_jobs.status

Prohibido en cualquier camino que toque documents, document_texts, statements,
transactions o merchants.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "app"

# El unico modulo que puede construir un cliente con service_role.
PROVIDER = "app.infra.supabase_admin"
PROVIDER_PATH = "app/infra/supabase_admin.py"

# Modulos autorizados a importar el provider.
ALLOWED_IMPORTERS = {
    "app/api/admin.py",
    "app/cli/seed.py",
    "app/cli/__main__.py",
    "app/jobs/reaper.py",
}

# Nadie mas puede nombrar la variable de entorno.
SECRET_NAMES = ("SUPABASE_SERVICE_ROLE_KEY", "service_role_key")

# settings.py declara el campo (es el unico lugar que lee el entorno), pero no
# construye clientes con el. El provider es el unico que lo consume.
SECRET_NAME_ALLOWED = {PROVIDER_PATH, "app/settings.py"}


def _rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def _imports_provider(tree: ast.Module) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module == PROVIDER or node.module.endswith("infra.supabase_admin"):
                return True
        elif isinstance(node, ast.Import) and any(alias.name == PROVIDER for alias in node.names):
            return True
    return False


def main() -> int:
    errors: list[str] = []

    for path in sorted(APP.rglob("*.py")):
        rel = _rel(path)
        source = path.read_text(encoding="utf-8")

        try:
            tree = ast.parse(source, filename=rel)
        except SyntaxError as exc:  # el lint de sintaxis es de ruff, no de aca
            errors.append(f"{rel}: no se pudo parsear ({exc})")
            continue

        if rel != PROVIDER_PATH and _imports_provider(tree) and rel not in ALLOWED_IMPORTERS:
            errors.append(
                f"{rel}: importa {PROVIDER}, que bypassea RLS.\n"
                f"    Si el uso es legitimo, agregalo a ALLOWED_IMPORTERS en este script\n"
                f"    y al inventario del README. Si toca datos financieros, no lo es:\n"
                f"    usa la conexion con el JWT del usuario (app.infra.db.user_tx)."
            )

        if rel not in SECRET_NAME_ALLOWED:
            for name in SECRET_NAMES:
                if name in source:
                    errors.append(
                        f"{rel}: menciona {name}. La key solo se consume en {PROVIDER_PATH}."
                    )

    if not errors:
        return 0

    print("ERROR: uso indebido de la service_role key de Supabase\n")
    for err in errors:
        print(f"  - {err}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
