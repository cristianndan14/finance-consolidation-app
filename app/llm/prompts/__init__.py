"""Prompts versionados, cargados de los `.md` de este directorio.

# Por que los prompts viven en archivos y llevan version en el nombre

Cada fila de `extraction_runs` guarda el `prompt_version` que la produjo. Eso
permite responder la pregunta que aparece sola cuando algo se ve raro: "¿esta
transaccion salio del prompt viejo o del nuevo?". Si los prompts fueran strings
dentro del codigo, la respuesta seria "de la version que estaba desplegada ese
dia", que no es una respuesta.

Editar un prompt en produccion es crear `_v2`, no modificar `_v1`: los datos ya
extraidos siguen apuntando a la version con la que se generaron.

# Por que markdown y no f-strings

Los prompts de extraccion son largos y tienen ejemplos con montos y tablas. En un
string de Python quedan ilegibles, con comillas escapadas y sin resaltado. En un
`.md` se leen y se editan como lo que son: texto.

Los huecos se rellenan con `str.format`, asi que las llaves literales del texto
van dobladas (`{{`).
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Final

PROMPTS_DIR: Final = Path(__file__).resolve().parent

# Las versiones que el codigo usa hoy. Tenerlas nombradas evita que un typo en un
# string suelto termine en un `prompt_version` que no existe.
PARSE_HEADER_V1: Final = "parse_header_v1"
PARSE_TRANSACTIONS_V1: Final = "parse_transactions_v1"
REPAIR_TRANSACTIONS_V1: Final = "repair_transactions_v1"

# v2: la v1 se quedo corta contra resumenes reales de cuatro emisores. El
# header no encontraba el saldo anterior cuando venia con otra etiqueta
# ("Del mes pasado", "Saldo a favor") ni la fecha de cierre cuando estaba en
# una frase; el de transacciones duplicaba los impuestos cuya etiqueta el
# layout parte en dos renglones. Las v1 quedan porque las corridas ya hechas
# apuntan a ellas.
PARSE_HEADER_V2: Final = "parse_header_v2"
PARSE_TRANSACTIONS_V2: Final = "parse_transactions_v2"

ENRICH_MERCHANTS_V1: Final = "enrich_merchants_v1"

# Lo que se usa hoy.
PARSE_HEADER: Final = PARSE_HEADER_V2
PARSE_TRANSACTIONS: Final = PARSE_TRANSACTIONS_V2
REPAIR_TRANSACTIONS: Final = REPAIR_TRANSACTIONS_V1
ENRICH_MERCHANTS: Final = ENRICH_MERCHANTS_V1


class PromptNotFoundError(LookupError):
    """Se pidio una version de prompt que no esta en el directorio."""


@lru_cache(maxsize=32)
def load(version: str) -> str:
    """El texto de un prompt. Cacheado: es un archivo que no cambia en runtime."""
    # El nombre viene del codigo, nunca de una request, pero igual se acota a un
    # nombre simple: un `../` en un `prompt_version` leeria archivos del disco.
    if not version.replace("_", "").replace("-", "").isalnum():
        raise PromptNotFoundError(f"nombre de prompt invalido: {version!r}")

    path = PROMPTS_DIR / f"{version}.md"
    if not path.is_file():
        available = sorted(p.stem for p in PROMPTS_DIR.glob("*.md"))
        raise PromptNotFoundError(f"no existe el prompt {version!r}. Disponibles: {available}")
    return path.read_text(encoding="utf-8")


def render(version: str, **values: Any) -> str:
    """Carga un prompt y le rellena los huecos."""
    try:
        return load(version).format(**values)
    except KeyError as exc:
        raise PromptNotFoundError(
            f"al prompt {version!r} le falta el valor {exc.args[0]!r}"
        ) from exc


def available() -> list[str]:
    return sorted(p.stem for p in PROMPTS_DIR.glob("*.md"))
