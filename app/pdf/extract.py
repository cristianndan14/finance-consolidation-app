"""Etapa 1: del PDF al texto. Deterministico, offline y gratis.

# Por que no hay OCR

Los resumenes de homebanking se generan digitalmente: tienen capa de texto. OCR
agregaria una dependencia pesada, errores de reconocimiento en los digitos (que
es justo lo que no se puede errar en montos) y minutos de CPU por documento. Si
un PDF resulta ser un escaneo, el documento se marca `failed` con
`no_text_layer` en lugar de adivinar.

# Por que se guardan tres representaciones de cada pagina

Cuesta lo mismo obtenerlas y evita reabrir el PDF cuando la etapa 2 descubra que
para tal emisor una funciona mejor que otra:

- `text_layout` — `extract_text(layout=True)`: respeta las columnas con espacios.
  Es la que sirve en las tablas de consumos, donde fecha, descripcion y monto
  estan alineados por posicion.
- `text_flow` — `extract_text()`: orden de lectura natural. Mejor para los
  bloques de texto libre (leyendas, totales en parrafos).
- `tables` — `extract_tables()`: solo da algo cuando la pagina tiene lineas de
  tabla dibujadas, pero cuando lo da es lo mas limpio.

# Que decide `has_text_layer`

Un PDF escaneado abre bien y devuelve texto vacio o casi. El umbral esta en
caracteres por pagina, no en caracteres totales: un resumen de 8 paginas con 300
caracteres en total es un escaneo con un titulo suelto, no un documento corto.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

import pdfplumber

from app.logging_config import get_logger

log = get_logger(__name__)

EXTRACTOR: Final = "pdfplumber"

# Debajo de esto, el PDF es un escaneo. Un resumen real tiene miles de caracteres
# por pagina; el umbral esta puesto donde no hay zona gris.
MIN_CHARS_PER_PAGE: Final = 200

# Las tablas se serializan a JSON: una pagina patologica podria devolver miles de
# celdas y no aportar nada.
MAX_TABLES_PER_PAGE: Final = 10


@dataclass(frozen=True)
class ExtractedText:
    """Lo que se guarda en `app.document_texts`."""

    extractor: str
    extractor_version: str
    page_texts: list[dict[str, Any]]
    full_text: str
    char_count: int
    has_text_layer: bool
    page_count: int


def _version() -> str:
    """Identifica como se extrajo, no solo con que.

    Queda en `(extractor, extractor_version)`, que es unico por documento: si
    mañana se cambia el modo de extraccion, la version nueva convive con la
    vieja y se pueden comparar en lugar de perder la anterior.
    """
    return f"{pdfplumber.__version__}/layout+flow+tables"


def _clean_tables(raw: list[list[list[str | None]]]) -> list[list[list[str]]]:
    """Normaliza las celdas a texto: `None` es ruido en el JSON."""
    return [
        [[(cell or "").strip() for cell in row] for row in table]
        for table in raw[:MAX_TABLES_PER_PAGE]
    ]


def extract(data: bytes) -> ExtractedText:
    """Extrae las tres representaciones de cada pagina.

    Sincrono y CPU-bound: quien lo llame desde codigo async debe usar
    `asyncio.to_thread`.
    """
    import io

    pages: list[dict[str, Any]] = []
    flow_chunks: list[str] = []

    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for number, page in enumerate(pdf.pages, start=1):
            layout = page.extract_text(layout=True) or ""
            flow = page.extract_text() or ""
            try:
                tables = _clean_tables(page.extract_tables())
            except Exception as exc:
                # `extract_tables` es lo mas fragil de pdfplumber. Que falle en
                # una pagina no puede tirar la extraccion entera: las otras dos
                # representaciones son las que realmente se usan.
                log.warning(
                    "no se pudieron extraer tablas de una pagina",
                    page=number,
                    error=type(exc).__name__,
                )
                tables = []

            pages.append(
                {
                    "page": number,
                    "text_layout": layout,
                    "text_flow": flow,
                    "tables": tables,
                }
            )
            flow_chunks.append(flow)

    page_count = len(pages)
    # El texto completo lleva marcas de pagina porque la etapa 2 necesita poder
    # decir "esta transaccion salio de la pagina 3" al validar la cobertura.
    full_text = "\n\n".join(
        f"--- pagina {page['page']} ---\n{page['text_layout']}" for page in pages
    )
    char_count = sum(len(chunk.strip()) for chunk in flow_chunks)
    has_text_layer = page_count > 0 and (char_count / page_count) >= MIN_CHARS_PER_PAGE

    log.info(
        "texto extraido",
        pages=page_count,
        char_count=char_count,
        has_text_layer=has_text_layer,
    )

    return ExtractedText(
        extractor=EXTRACTOR,
        extractor_version=_version(),
        page_texts=pages,
        full_text=full_text,
        char_count=char_count,
        has_text_layer=has_text_layer,
        page_count=page_count,
    )
