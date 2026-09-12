"""Comercios y categorias: lo que se puede resolver sin llamar al modelo.

# La idea central de esta fase

Enriquecer 200 transacciones por mes con el LLM seria pagar por adivinar lo que ya
se sabe. El grueso se resuelve gratis y de forma deterministica, y al modelo solo
le llega lo que de verdad necesita criterio:

1. **Por `kind`.** Un `tax` es "Impuestos y percepciones", un `payment` es "Pagos
   del resumen". Eso no es una opinion, es una correspondencia fija — y en un
   resumen argentino son cerca de un tercio de las lineas.
2. **Por la memoria de alias.** La segunda vez que aparece `MERPAGO*SPOTIFY` ya se
   sabe que es Spotify, porque la primera vez quedo guardado en
   `merchant_aliases`. Es lo que hace que el costo del enriquecimiento tienda a
   cero con el uso.
3. **Por reglas sobre la descripcion.** Un puñado de comercios argentinos que
   aparecen en todos los resumenes (COTO, YPF, UBER, RAPPI) no justifican una
   llamada paga.

Recien lo que sobra va al LLM, en un solo batch.

# Por que la clave de alias no es la descripcion cruda

El mismo comercio aparece cada mes con un numero de operacion distinto:

    MERPAGO*SPOTIFY 1234 BUENOS AIRES AR
    MERPAGO*SPOTIFY 5678 CABA

Con la descripcion cruda como clave, el cache no acierta nunca y se le pagaria al
modelo todos los meses por la misma respuesta. `merchant_key` saca el prefijo del
procesador de pagos, los numeros de operacion y los sufijos de sucursal y pais,
de modo que las dos lineas de arriba den la misma clave.

El riesgo simetrico —agrupar de mas— existe: dos comercios distintos cuyo nombre
solo se diferencia en un numero quedarian bajo la misma clave. Se acepta porque la
correccion manual del usuario (`source = 'user'`) pisa al modelo y queda para
siempre, mientras que el caso contrario (no acertar nunca) cuesta dinero cada mes.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Final

# ─────────────────────────────────────────────────────────────────────────────
# La clave con la que se recuerda un comercio
# ─────────────────────────────────────────────────────────────────────────────

# Prefijos de procesadores de pago. No dicen nada del comercio y ensucian la
# clave: lo que importa de `MERPAGO*SPOTIFY` es Spotify.
_PROCESSOR_PREFIXES: Final = (
    "MERPAGO",
    "MERCADOPAGO",
    "MP",
    "PAYU",
    "DLO",
    "DLOCAL",
    "PAGOFACIL",
    "RAPIPAGO",
    "PAYPAL",
    "PP",
    "SQ",
    "STRIPE",
)

# Sufijos que son ubicacion o pais, no comercio.
_LOCATION_SUFFIXES: Final = (
    "BUENOS AIRES",
    "CIUDAD AUTONOMA",
    "CAPITAL FEDERAL",
    "CABA",
    "ARGENTINA",
    "ARG",
    "AR",
)

_ACCENTS: Final = re.compile(r"[̀-ͯ]")
_WHITESPACE: Final = re.compile(r"\s+")
# Todo lo que no sea letra, numero o espacio se vuelve separador.
_PUNCT: Final = re.compile(r"[^\w\s]", flags=re.UNICODE)
# Tokens que son puro numero o codigos alfanumericos largos: numeros de
# operacion, de sucursal, de cupon.
_NUMERIC_TOKEN: Final = re.compile(r"^\d+$")
_CODE_TOKEN: Final = re.compile(r"^(?=.*\d)[A-Z0-9]{5,}$")

MAX_KEY_LENGTH: Final = 120


def _fold(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", text)
    return _ACCENTS.sub("", normalized).upper()


def merchant_key(description: str) -> str:
    """Clave estable con la que se recuerda un comercio entre resúmenes.

    >>> merchant_key("MERPAGO*SPOTIFY 1234 BUENOS AIRES AR")
    'SPOTIFY'
    >>> merchant_key("MERPAGO*SPOTIFY 5678 CABA")
    'SPOTIFY'
    >>> merchant_key("SUPERMERCADO COTO  SUC. 12")
    'SUPERMERCADO COTO SUC'
    """
    text = _PUNCT.sub(" ", _fold(description))
    tokens = [token for token in _WHITESPACE.split(text) if token]

    if tokens and tokens[0] in _PROCESSOR_PREFIXES:
        tokens = tokens[1:]

    tokens = [
        token
        for token in tokens
        if not _NUMERIC_TOKEN.match(token) and not _CODE_TOKEN.match(token)
    ]

    # Los sufijos de ubicacion se sacan del final, no de cualquier posicion: un
    # comercio podria llamarse "AR..." legitimamente.
    joined = " ".join(tokens)
    changed = True
    while changed:
        changed = False
        for suffix in _LOCATION_SUFFIXES:
            if joined.endswith(f" {suffix}") or joined == suffix:
                joined = joined[: -len(suffix)].strip()
                changed = True

    return joined[:MAX_KEY_LENGTH] or _fold(description).strip()[:MAX_KEY_LENGTH]


def merchant_slug(canonical_name: str) -> str:
    """Slug de `app.merchants.slug`, unico por usuario."""
    text = _PUNCT.sub(" ", _fold(canonical_name)).lower()
    return _WHITESPACE.sub("-", text.strip())[:80] or "sin-nombre"


# ─────────────────────────────────────────────────────────────────────────────
# Categoria por tipo de movimiento
# ─────────────────────────────────────────────────────────────────────────────
#
# Esto no es una heuristica: es la definicion de cada `kind`. Un impuesto va a
# impuestos, siempre. Resolverlo aca deja fuera del LLM alrededor de un tercio de
# las lineas de un resumen argentino, que ademas son las que menos aportan al
# analisis de consumo.
KIND_CATEGORY: Final[dict[str, str]] = {
    "tax": "impuestos",
    "interest": "intereses",
    "fee": "comisiones",
    "fx_charge": "comisiones",
    "payment": "pagos",
    "refund": "devoluciones",
}


# ─────────────────────────────────────────────────────────────────────────────
# Reglas por descripcion
# ─────────────────────────────────────────────────────────────────────────────
#
# Un puñado de comercios que aparecen en practicamente todos los resumenes
# argentinos. No pretende ser exhaustivo —para eso esta el LLM— sino sacarle de
# encima los casos obvios y baratos.
#
# El orden importa: gana la primera que matchea.
_RULES: Final[tuple[tuple[re.Pattern[str], str], ...]] = tuple(
    (re.compile(pattern), slug)
    for pattern, slug in (
        (r"\b(COTO|CARREFOUR|JUMBO|DISCO|VEA|DIA|CHANGOMAS|LIBERTAD|MAKRO)\b", "supermercado"),
        (r"\b(YPF|SHELL|AXION|PUMA ENERGY|REFINOR|GNC)\b", "combustible"),
        (r"\b(UBER|CABIFY|DIDI|SUBE|AUTOPISTA|AUSA|TELEPASE|BEAT)\b", "transporte"),
        (r"\b(RAPPI|PEDIDOSYA|MCDONALD|BURGER KING|STARBUCKS|HAVANNA|MOSTAZA)\b", "gastronomia"),
        (r"\b(FARMACITY|FARMACIA|DR AHORRO|SIMPLICITY|OSDE|SWISS MEDICAL|GALENO)\b", "salud"),
        (
            r"\b(NETFLIX|SPOTIFY|DISNEY|HBO|MAX|PARAMOUNT|APPLE COM|GOOGLE|YOUTUBE|"
            r"AMAZON PRIME|CLARO VIDEO|FLOW)\b",
            "suscripciones",
        ),
        (
            r"\b(EDENOR|EDESUR|METROGAS|AYSA|TELECOM|PERSONAL|MOVISTAR|CLARO|FIBERTEL)\b",
            "servicios",
        ),
        (r"\b(MERCADOLIBRE|MERCADO LIBRE|FRAVEGA|GARBARINO|MUSIMUNDO|COMPUMUNDO)\b", "tecnologia"),
        (r"\b(ZARA|H M|UNIQLO|ADIDAS|NIKE|DEXTER|SPORTLINE|GRIMOLDI)\b", "indumentaria"),
        (r"\b(EASY|SODIMAC|SIMPLICIT|FERRETERIA|BLAISTEN)\b", "hogar"),
        (r"\b(DESPEGAR|AEROLINEAS|LATAM|BOOKING|AIRBNB|AIR EUROPA)\b", "viajes"),
        (
            r"\b(CINEMARK|HOYTS|SHOWCASE|TICKETEK|PASSLINE|STEAM|PLAYSTATION|XBOX)\b",
            "entretenimiento",
        ),
        (r"\b(UDEMY|COURSERA|PLATZI|DUOLINGO|NETIDIOMAS)\b", "educacion"),
        (r"\b(VETERINARIA|PUPPIS|MASCOTA)\b", "mascotas"),
        (r"\b(SEGURO|SEGUROS|ZURICH|SANCOR|LA CAJA|PREVENCION)\b", "seguros"),
        (r"\bADELANTO\b|\bEFECTIVO\b", "adelantos"),
    )
)


def categorize(*, kind: str, description: str) -> str | None:
    """El slug de categoria que corresponde, o `None` si hay que preguntarle al LLM.

    Devolver `None` es una respuesta valida y deseable: significa "esto necesita
    criterio". Forzar una categoria acá con una regla floja llenaria el dashboard
    de clasificaciones plausibles y equivocadas, que es peor que un "Sin
    categoria" honesto.
    """
    by_kind = KIND_CATEGORY.get(kind)
    if by_kind is not None:
        return by_kind

    key = _fold(description)
    for pattern, slug in _RULES:
        if pattern.search(key):
            return slug

    return None


def needs_llm(*, kind: str, description: str) -> bool:
    """Si esta transaccion hay que mandarsela al modelo."""
    return categorize(kind=kind, description=description) is None
