"""Ejemplos few-shot armados con las correcciones reales del usuario.

# De donde sale la señal

De `app.transaction_revisions`, la bitacora de lo que el usuario corrigio a mano.
Cuando alguien reasigna la categoria de una transaccion esta diciendo, con un
dato y no con una opinion, que el modelo se equivoco en ese comercio. Devolverle
esa correccion en el prompt del mes siguiente es la forma mas barata que hay de
mejorar la clasificacion: no requiere reentrenar nada ni cambiar el prompt base.

# Por que no se usan los alias de `source = 'user'`

Podria parecer la fuente obvia — es la misma correccion, ya normalizada. Pero un
alias del usuario **nunca llega al modelo**: el embudo lo resuelve en el filtro 2
y la clave ni siquiera entra en la lista que se pregunta. Lo que se busca aca es
otra cosa: que el criterio del usuario se generalice a las claves que todavia no
vio nadie.

# Por que la lista es corta

Un prompt con cuarenta ejemplos cuesta tokens en cada llamada y, pasado cierto
punto, el modelo copia el formato de los ejemplos en lugar de razonar sobre la
lista. Ocho alcanzan para fijar el criterio.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Final

from app.domain import categorization
from app.llm.ports import MerchantExample

DEFAULT_LIMIT: Final = 8

_EMPTY = "_(este usuario todavía no corrigió ninguna clasificación)_"


def build(
    corrections: Iterable[tuple[str, str, str]],
    *,
    category_slugs: Sequence[str],
    exclude_keys: Iterable[str] = (),
    limit: int = DEFAULT_LIMIT,
) -> list[MerchantExample]:
    """`(descripcion cruda, comercio, slug)` corregidos -> ejemplos listos para el prompt.

    `corrections` viene en orden de relevancia (lo mas reciente primero): ante dos
    correcciones de la misma clave gana la primera, que es la ultima que hizo el
    usuario.

    Se descarta lo que no se puede enseñar: un slug que ya no esta en el catalogo
    activo le enseñaria al modelo a devolver una categoria que el sistema
    descarta, y una clave que esta en la tanda que se va a preguntar le daria la
    respuesta en vez de hacerlo pensar.
    """
    allowed = set(category_slugs)
    skip = {categorization.merchant_key(key) for key in exclude_keys}

    examples: list[MerchantExample] = []
    seen: set[str] = set()

    for description, canonical_name, slug in corrections:
        if slug not in allowed or not canonical_name:
            continue

        key = categorization.merchant_key(description)
        if key in seen or key in skip:
            continue

        seen.add(key)
        examples.append(
            MerchantExample(raw_key=key, canonical_name=canonical_name, category_slug=slug)
        )
        if len(examples) >= limit:
            break

    return examples


def render(examples: Sequence[MerchantExample]) -> str:
    """El bloque que se inyecta en el prompt.

    Sin ejemplos devuelve una linea que lo dice. Dejar el hueco vacio haria que el
    prompt tenga una seccion muda, y un modelo que ve un titulo sin contenido
    tiende a inventarle contenido.
    """
    if not examples:
        return _EMPTY

    return "\n".join(
        f"- `{example.raw_key}` → {example.canonical_name} ({example.category_slug})"
        for example in examples
    )
