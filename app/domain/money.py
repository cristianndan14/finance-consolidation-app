"""Montos: parseo del formato es-AR y aritmetica con Decimal.

# Por que Decimal y no float, sin excepciones

`0.1 + 0.2 != 0.3` en punto flotante. Sobre cientos de transacciones por mes, ese
error se acumula y rompe justo la validacion mas importante del proyecto: el cuadre
de la suma de transacciones contra el total del resumen. Un cuadre que falla por
error de redondeo es indistinguible de un cuadre que falla porque el LLM se comio
una linea, y el usuario termina revisando a mano resumenes que estaban bien.

Por eso el camino completo es Decimal: el LLM devuelve los montos como STRING
(ver app/schemas/llm.py), Pydantic los convierte a Decimal en el borde, y Postgres
los guarda en `numeric(14,2)`.

# El formato es-AR

En Argentina el separador de miles es el punto y el decimal es la coma:
`1.234,56` son mil doscientos treinta y cuatro con cincuenta y seis. Es exactamente
al reves que en en-US, asi que interpretar `1.234` como un float da 1.234 en lugar
de 1234. Un error de tres ordenes de magnitud, silencioso.

Peor: los emisores mezclan formatos. Algunas fintechs emiten en formato ingles.
Por eso `parse_amount` decide por la POSICION del ultimo separador, no por una
suposicion de locale.
"""

from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Final

CENTS: Final = Decimal("0.01")

# Marcas de credito que usan los resumenes argentinos al final de un monto.
# 'CR' = credito, 'H' = haber, '-' = negativo. No hay consenso entre emisores.
_CREDIT_MARKERS: Final = ("CR", "C/R", "H", "HABER")

_CLEAN_RE: Final = re.compile(r"[^\d.,\-]")


class AmountParseError(ValueError):
    """El texto no representa un monto reconocible."""


def parse_amount(raw: str | int | float | Decimal) -> Decimal:
    """Convierte el texto de un monto a Decimal, con dos decimales.

    Acepta el formato es-AR (`1.234,56`), el ingles (`1,234.56`), sin separadores
    (`1234.56`) y con simbolos de moneda o espacios alrededor.

    La decision de cual separador es el decimal se toma por posicion: el separador
    que aparece ULTIMO, si le siguen 1 o 2 digitos, es el decimal. Es lo unico que
    funciona sin saber de antemano el locale del emisor.

        >>> parse_amount("1.234,56")
        Decimal('1234.56')
        >>> parse_amount("1,234.56")
        Decimal('1234.56')
        >>> parse_amount("$ 1.234.567,89")
        Decimal('1234567.89')
        >>> parse_amount("1.234")        # sin decimales: el punto es de miles
        Decimal('1234.00')
    """
    if isinstance(raw, Decimal):
        return quantize(raw)
    if isinstance(raw, int):
        return quantize(Decimal(raw))
    if isinstance(raw, float):
        # Aceptado por conveniencia en tests, pero es el tipo que este modulo
        # existe para evitar: se pasa por str para no arrastrar el error binario.
        return quantize(Decimal(str(raw)))

    text = raw.strip()
    if not text:
        raise AmountParseError("monto vacio")

    upper = text.upper()
    is_credit = any(upper.endswith(marker) for marker in _CREDIT_MARKERS)

    cleaned = _CLEAN_RE.sub("", text)
    if not cleaned:
        raise AmountParseError(f"no hay digitos en {raw!r}")

    negative = is_credit or cleaned.startswith("-") or text.startswith("(")
    cleaned = cleaned.lstrip("-")

    normalized = _normalize_separators(cleaned, original=raw)

    try:
        value = Decimal(normalized)
    except InvalidOperation as exc:
        raise AmountParseError(f"no se pudo interpretar {raw!r} como monto") from exc

    return quantize(-value if negative else value)


def _normalize_separators(cleaned: str, *, original: object) -> str:
    """Deja el numero con punto decimal y sin separadores de miles."""
    last_dot = cleaned.rfind(".")
    last_comma = cleaned.rfind(",")

    if last_dot == -1 and last_comma == -1:
        return cleaned

    # El separador que va ultimo es el candidato a decimal.
    sep_pos, sep = (last_dot, ".") if last_dot > last_comma else (last_comma, ",")
    decimals = len(cleaned) - sep_pos - 1

    # Con 1 o 2 digitos detras es un decimal; con 3 es un separador de miles
    # ("1.234"). Con cualquier otra cantidad, el texto no es un monto.
    if decimals in (1, 2):
        integer_part = cleaned[:sep_pos].replace(".", "").replace(",", "")
        fraction = cleaned[sep_pos + 1 :]
        if not integer_part:
            integer_part = "0"
        return f"{integer_part}.{fraction}"

    if decimals == 3:
        return cleaned.replace(".", "").replace(",", "")

    raise AmountParseError(
        f"{original!r}: {decimals} digitos despues del ultimo separador {sep!r}; no parece un monto"
    )


def quantize(value: Decimal) -> Decimal:
    """Redondea a centavos con ROUND_HALF_UP.

    ROUND_HALF_UP y no el default de Python (ROUND_HALF_EVEN, "redondeo del
    banquero") porque es lo que hace un resumen de tarjeta: 0.125 -> 0.13. Con
    HALF_EVEN daria 0.12 y el cuadre fallaria por un centavo.
    """
    return value.quantize(CENTS, rounding=ROUND_HALF_UP)


def signed(amount: Decimal, direction: int) -> Decimal:
    """Monto con signo. `direction` 1 aumenta la deuda, -1 la baja."""
    if direction not in (1, -1):
        raise ValueError(f"direction debe ser 1 o -1, no {direction!r}")
    return quantize(amount * direction)


def convert(amount: Decimal, rate: Decimal) -> Decimal:
    """Convierte un monto a otra moneda con la cotizacion dada."""
    if rate <= 0:
        raise ValueError(f"la cotizacion debe ser positiva, no {rate!r}")
    return quantize(amount * rate)


def within_tolerance(expected: Decimal, actual: Decimal, *, pct: Decimal, floor: Decimal) -> bool:
    """True si `actual` cuadra con `expected` dentro de la tolerancia.

    La tolerancia es el mayor entre un porcentaje del esperado y un piso absoluto:
    el piso absorbe redondeos de centavos en resumenes chicos, el porcentaje evita
    que un resumen de millones falle por una diferencia irrelevante.
    """
    allowed = max(abs(expected) * pct, floor)
    return abs(expected - actual) <= allowed


def format_ars(amount: Decimal) -> str:
    """Formatea en es-AR para mostrar: `1.234,56`."""
    value = quantize(amount)
    negative = value < 0
    integer, _, fraction = f"{abs(value):.2f}".partition(".")

    groups: list[str] = []
    while len(integer) > 3:
        groups.insert(0, integer[-3:])
        integer = integer[:-3]
    groups.insert(0, integer)

    formatted = f"{'.'.join(groups)},{fraction}"
    return f"-{formatted}" if negative else formatted
