#!/usr/bin/env python
"""Bloquea el commit de PDFs de resumenes reales.

Los resumenes de tarjeta contienen todos los consumos del titular, su nombre, su
domicilio y los ultimos digitos de sus tarjetas. No pueden entrar al historial de
git, del que no se borran nunca del todo.

Unica excepcion: tests/fixtures/pdfs/, donde viven los PDFs sinteticos generados
por reportlab (tests/fixtures/pdfs/generate_pdfs.py).
"""

from __future__ import annotations

import sys
from pathlib import PurePosixPath

ALLOWED_PREFIX = "tests/fixtures/pdfs/"


def main(argv: list[str]) -> int:
    offenders = [
        arg
        for arg in argv
        if PurePosixPath(arg.replace("\\", "/")).suffix.lower() == ".pdf"
        and not arg.replace("\\", "/").startswith(ALLOWED_PREFIX)
    ]
    if not offenders:
        return 0

    print("ERROR: se intento commitear un PDF fuera de " + ALLOWED_PREFIX)
    for path in offenders:
        print(f"  - {path}")
    print()
    print("Los resumenes reales van a tests/fixtures/private/ (gitignored).")
    print("Para usarlos en tests, anonimizalos primero:")
    print("  uv run python -m app.cli anonymize <archivo.pdf>")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
