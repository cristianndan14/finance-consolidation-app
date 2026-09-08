"""Deduplicacion de transacciones.

# El problema

Hay que distinguir dos situaciones que se ven casi iguales:

1. **El mismo resumen se procesa dos veces.** Pasa al reintentar un job, al
   reprocesar con un prompt nuevo, o al subir el archivo renombrado. Las
   transacciones duplicadas NO deben insertarse: duplicarian el gasto del mes.

2. **El resumen tiene lineas legitimamente identicas.** Dos cafes de $2.500 en el
   mismo bar el mismo dia son dos gastos reales. Colapsarlos hace que el mes cierre
   $2.500 abajo y que el cuadre contra el total del resumen falle.

Una clave hash sola no alcanza: resolveria (1) rompiendo (2).

# La solucion

`dedupe_key` identifica el CONTENIDO de la transaccion. `occurrence_index` numera
las repeticiones de esa clave DENTRO del mismo lote extraido: la primera es 0, la
segunda 1, etc.

El constraint `unique (user_id, statement_id, dedupe_key, occurrence_index)` hace
entonces lo correcto en los dos casos:

- Reprocesar el mismo resumen produce el mismo lote, con los mismos indices, y
  choca contra el unique. Idempotente.
- Los dos cafes reales tienen indices 0 y 1, no chocan, y ambos se guardan.

La clave se calcula sobre la descripcion NORMALIZADA para que no dependa de
variaciones de espaciado del extractor de texto, y NO incluye la categoria ni el
comercio, que son datos del enriquecimiento: si los incluyera, recategorizar
cambiaria la identidad de la transaccion y el merge de un reparse la veria como
nueva.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections import defaultdict
from collections.abc import Iterable, Sequence
from datetime import date
from decimal import Decimal
from typing import Protocol

_WHITESPACE = re.compile(r"\s+")
_NOISE = re.compile(r"[^\w\s]", flags=re.UNICODE)


def normalize_description(raw: str) -> str:
    """Normaliza la descripcion para comparar.

    Mayusculas, sin acentos, sin puntuacion y con un solo espacio entre palabras.
    El objetivo es que el mismo consumo leido dos veces por el extractor de a la
    misma clave, incluso si una vez vino con doble espacio o con un guion distinto.

        >>> normalize_description("Café  Martínez -- Suc. 12")
        'CAFE MARTINEZ SUC 12'
    """
    text = unicodedata.normalize("NFKD", raw)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = _NOISE.sub(" ", text)
    return _WHITESPACE.sub(" ", text).strip().upper()


def dedupe_key(
    *,
    description: str,
    posted_date: date,
    amount: Decimal,
    currency: str,
    installment_number: int | None = None,
    installment_total: int | None = None,
) -> str:
    """Hash que identifica el contenido de una transaccion.

    Las cuotas entran en la clave porque distinguen consumos que de otro modo serian
    identicos: la cuota 3/12 y la 4/12 de la misma compra pueden tener el mismo
    monto, la misma descripcion y caer en el mismo resumen si el emisor informa dos
    juntas.
    """
    parts = (
        normalize_description(description),
        posted_date.isoformat(),
        f"{amount:.2f}",
        currency.upper(),
        str(installment_number or ""),
        str(installment_total or ""),
    )
    payload = "|".join(parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class HasDedupeKey(Protocol):
    dedupe_key: str


def assign_occurrence_indexes[T: HasDedupeKey](items: Sequence[T]) -> list[tuple[T, int]]:
    """Numera 0..n-1 las repeticiones de cada `dedupe_key`, en orden de aparicion.

    Se preserva el orden original del lote porque es el orden del resumen, que es
    como el usuario lo va a revisar.

    Para un lote con claves ``[a, b, a, a]`` los indices resultantes son
    ``[0, 0, 1, 2]``.
    """
    seen: defaultdict[str, int] = defaultdict(int)
    result: list[tuple[T, int]] = []
    for item in items:
        index = seen[item.dedupe_key]
        seen[item.dedupe_key] = index + 1
        result.append((item, index))
    return result


def drop_overlap_duplicates[T: HasDedupeKey](items: Iterable[T]) -> list[T]:
    """Quita las repeticiones que introduce el solapamiento entre chunks.

    La etapa 2 corta el texto en chunks con 300 caracteres de solapamiento para no
    perder transacciones partidas en el borde. El costo es que las del solapamiento
    vienen dos veces, del chunk anterior y del siguiente.

    OJO con la diferencia respecto de `assign_occurrence_indexes`: aca SI se
    colapsa por clave, porque estas repeticiones son artefactos del chunking, no
    consumos reales. Se aplica ANTES de asignar los indices; si se hiciera despues,
    los dos cafes legitimos se perderian.

    Es una decision con un costo real: si un resumen tuviera dos lineas identicas
    y ademas cayeran en la zona de solapamiento, se pierde una. Se acepta porque el
    check de cuadre lo detecta (la suma no llega al total) y la pantalla de revision
    permite agregarla, mientras que el caso contrario — duplicar transacciones en
    silencio — infla el gasto del mes sin que nada avise.
    """
    seen: set[str] = set()
    result: list[T] = []
    for item in items:
        if item.dedupe_key in seen:
            continue
        seen.add(item.dedupe_key)
        result.append(item)
    return result


def installment_group_key(
    *,
    description: str,
    installment_total: int,
    purchase_total_amount: Decimal | None,
    purchase_date: date | None,
) -> str:
    """Clave que agrupa las cuotas de una misma compra a lo largo de los meses.

    No puede depender de `posted_date` ni del numero de cuota, que cambian mes a
    mes. Usa lo que se mantiene: el comercio, el total de cuotas y, si el resumen lo
    informa, el monto total y la fecha de la compra original.

    Cuando el emisor no informa el total ni la fecha (algunos no lo hacen), la clave
    es mas debil: dos compras distintas en el mismo comercio, con la misma cantidad
    de cuotas, se agrupan juntas. Es un limite conocido; la alternativa seria no
    agrupar nada y perder la proyeccion de cuotas futuras, que es una de las vistas
    mas utiles.
    """
    parts = (
        normalize_description(description),
        str(installment_total),
        f"{purchase_total_amount:.2f}" if purchase_total_amount is not None else "",
        purchase_date.isoformat() if purchase_date else "",
    )
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:32]
