"""Corte del texto en fragmentos para mandarle al modelo.

# Por que se corta si el modelo tiene ventana de sobra

Gemini 2.5 Flash entra un resumen entero sin transpirar, asi que el limite no es
tecnico. Es de accuracy: en una tabla de 300 lineas, un modelo al que se le pide
"extraeme todas" empieza a saltear filas hacia el final. Con fragmentos de una o
dos paginas la tarea es corta, la atencion no se diluye, y ademas las llamadas
salen en paralelo.

# Por que el corte es por pagina y no cada N caracteres

Una pagina de resumen es una unidad visual coherente: el encabezado de la tabla,
las columnas y el subtotal estan en la misma. Cortar por caracteres parte una
linea al medio y produce una transaccion con la fecha de una y el monto de otra,
que es peor que no extraerla.

# El solapamiento

Aun cortando por pagina, un consumo puede continuar en la siguiente (descripcion
larga, o una cuota cuyo detalle sigue abajo). Por eso cada fragmento arranca
repitiendo el final del anterior, cortado en un salto de linea para no partir
nada.

El costo es que las transacciones del solapamiento vienen dos veces, del
fragmento anterior y del siguiente. Eso lo limpia `dedupe.drop_overlap_duplicates`
despues del merge — es una repeticion prevista, no un bug.

# Y por que `text_layout` y no `text_flow`

`layout=True` preserva las columnas con espacios. En una tabla de consumos, donde
fecha, descripcion y monto estan alineados por posicion, es la diferencia entre
que el modelo vea una tabla y que vea palabras sueltas.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

# Con el layout preservado, una pagina de resumen ronda los 3-5k caracteres: dos
# paginas por fragmento sin pasarse.
DEFAULT_MAX_CHARS: Final = 12_000

# Alcanza para cubrir una transaccion partida (dos o tres lineas de resumen) sin
# duplicar medio fragmento.
DEFAULT_OVERLAP_CHARS: Final = 300

# La cabecera se pide sobre la primera y la ultima pagina, que es donde estan los
# totales. Mandar el resumen entero para leer seis numeros es tirar tokens.
DEFAULT_HEADER_CHARS: Final = 16_000

LAYOUT_FIELD: Final = "text_layout"
FLOW_FIELD: Final = "text_flow"


@dataclass(frozen=True)
class TextChunk:
    """Un pedazo de texto listo para una llamada al modelo."""

    index: int
    text: str
    first_page: int
    last_page: int

    @property
    def char_count(self) -> int:
        return len(self.text)


def page_text(page: Mapping[str, Any], *, field: str = LAYOUT_FIELD) -> str:
    """El texto de una pagina, con el fallback a la otra representacion.

    Hay paginas donde `layout=True` devuelve vacio (tipicamente las que son un
    solo bloque grafico). Antes que mandar una pagina en blanco, se usa el texto
    en orden de lectura: peor para las tablas, pero algo.
    """
    text = str(page.get(field) or "").rstrip()
    if text:
        return text
    other = FLOW_FIELD if field == LAYOUT_FIELD else LAYOUT_FIELD
    return str(page.get(other) or "").rstrip()


def _marked(page: Mapping[str, Any], text: str) -> str:
    """Cada bloque lleva su numero de pagina.

    No es decorativo: el modelo lo copia en `source_page`, y el check 8 de la
    validacion (cobertura de paginas) necesita saber de que pagina salio cada
    transaccion para detectar una pagina que se proceso entera y no aporto nada.
    """
    return f"--- pagina {page.get('page', '?')} ---\n{text}"


def chunk_pages(
    pages: Sequence[Mapping[str, Any]],
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
    overlap: int = DEFAULT_OVERLAP_CHARS,
    field: str = LAYOUT_FIELD,
) -> list[TextChunk]:
    """Agrupa paginas en fragmentos de a lo sumo `max_chars`, con solapamiento."""
    if max_chars <= 0:
        raise ValueError("max_chars tiene que ser positivo")
    if overlap < 0 or overlap >= max_chars:
        raise ValueError("overlap tiene que ser no negativo y menor que max_chars")

    blocks = _blocks(pages, max_chars=max_chars, field=field)
    if not blocks:
        return []

    chunks: list[TextChunk] = []
    current: list[tuple[int, str]] = []
    current_len = 0

    def flush() -> None:
        nonlocal current, current_len
        if not current:
            return
        body = "\n\n".join(text for _page, text in current)
        tail = _tail(chunks[-1].text, overlap) if chunks and overlap else ""
        chunks.append(
            TextChunk(
                index=len(chunks),
                text=f"{tail}{body}" if tail else body,
                first_page=current[0][0],
                last_page=current[-1][0],
            )
        )
        current = []
        current_len = 0

    for number, text in blocks:
        # +2 por el separador. Un bloque solo siempre entra: `_blocks` ya partio
        # las paginas que no entraban.
        if current and current_len + len(text) + 2 > max_chars:
            flush()
        current.append((number, text))
        current_len += len(text) + 2

    flush()
    return chunks


def _blocks(
    pages: Sequence[Mapping[str, Any]], *, max_chars: int, field: str
) -> list[tuple[int, str]]:
    """Un bloque por pagina, partiendo las que no entran en un fragmento."""
    blocks: list[tuple[int, str]] = []
    for page in pages:
        text = page_text(page, field=field)
        if not text.strip():
            continue

        number = int(page.get("page", len(blocks) + 1))
        marked = _marked(page, text)
        if len(marked) <= max_chars:
            blocks.append((number, marked))
            continue

        # Una pagina que sola supera el limite (resumenes con cientos de lineas
        # en una pagina existen). Se parte por lineas: partirla es feo, pero
        # mandarla entera y que el modelo saltee la mitad es peor.
        for piece in _split_lines(text, max_chars - 64):
            blocks.append((number, _marked(page, piece)))
    return blocks


def _split_lines(text: str, limit: int) -> list[str]:
    """Corta en limites de linea, nunca al medio de una."""
    pieces: list[str] = []
    buffer: list[str] = []
    size = 0

    for line in text.splitlines():
        if buffer and size + len(line) + 1 > limit:
            pieces.append("\n".join(buffer))
            buffer, size = [], 0
        buffer.append(line)
        size += len(line) + 1

    if buffer:
        pieces.append("\n".join(buffer))
    return pieces


def _tail(text: str, overlap: int) -> str:
    """Los ultimos `overlap` caracteres, redondeados a un salto de linea."""
    if len(text) <= overlap:
        snippet = text
    else:
        snippet = text[-overlap:]
        newline = snippet.find("\n")
        if newline != -1:
            snippet = snippet[newline + 1 :]

    snippet = snippet.strip()
    if not snippet:
        return ""
    # La marca explicita evita que el modelo cuente dos veces lo que ya extrajo
    # el fragmento anterior; el dedupe posterior lo cubre igual, pero es gratis
    # avisarle.
    return f"[continuacion del fragmento anterior]\n{snippet}\n\n"


def header_text(
    pages: Sequence[Mapping[str, Any]],
    *,
    max_chars: int = DEFAULT_HEADER_CHARS,
    field: str = LAYOUT_FIELD,
) -> str:
    """El texto con el que se le piden los totales al modelo.

    # Por que no es simplemente "la primera y la ultima pagina"

    Esa era la version original, y fallo contra un resumen real de Naranja X: el
    saldo anterior estaba en la **pagina 2 de 4**, asi que el modelo nunca lo vio
    y devolvio null. El sintoma era un documento "sin totales con los que
    verificar", que parecia un problema de prompt y era de recorte.

    # La regla

    Si el documento entero entra en el presupuesto, se manda entero: para un
    resumen tipico son 5-12k caracteres, muy por debajo del limite, y no hay
    ninguna razon para adivinar en que pagina puso el emisor sus totales.

    Recien cuando no entra se recorta, y ahi se eligen las paginas por donde
    suelen estar: las dos primeras (cabecera y, con frecuencia, el bloque del
    saldo anterior) y la ultima (los totales por moneda).
    """
    if not pages:
        return ""

    def render(selected: Sequence[Mapping[str, Any]]) -> str:
        parts = [_marked(page, page_text(page, field=field)) for page in selected]
        return "\n\n".join(part for part in parts if part.strip())

    # De mas informacion a menos. El primero que entre, gana.
    candidates: list[Sequence[Mapping[str, Any]]] = [pages]
    if len(pages) > 3:
        candidates.append([pages[0], pages[1], pages[-1]])
    if len(pages) > 2:
        candidates.append([pages[0], pages[-1]])

    for selected in candidates:
        text = render(selected)
        if len(text) <= max_chars:
            return text

    text = render(candidates[-1])

    if len(text) <= max_chars:
        return text
    # Se recorta del medio: el principio y el final son justamente lo que
    # interesa.
    half = max_chars // 2
    return f"{text[:half]}\n\n[... texto omitido ...]\n\n{text[-half:]}"
