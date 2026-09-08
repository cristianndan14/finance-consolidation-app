"""Fechas: inferencia del año faltante y periodo del resumen.

# El problema del año que no esta

Casi ningun resumen de tarjeta pone el año en las lineas de consumo: dice `15/03`,
no `15/03/2026`. El año hay que inferirlo del periodo del resumen.

Eso funciona once meses al año. El que falla es el cruce de diciembre a enero: un
resumen que cierra el 5 de enero de 2026 lista consumos del 20 de diciembre. Si se
les asigna el año del cierre, esos consumos van a diciembre de **2026** — un año en
el futuro. El gasto desaparece del dashboard de diciembre y reaparece dentro de doce
meses.

`infer_year` resuelve eso eligiendo, entre el año del cierre y el anterior, el que
deja la fecha mas cerca del cierre sin pasarse.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Final

# Cuanto antes del cierre puede estar un consumo. Los resumenes suelen abarcar un
# mes, pero una operacion informada tarde (o una cuota) puede venir de mas atras.
MAX_DAYS_BEFORE_CLOSING: Final = 45
# Un consumo posterior al cierre es raro pero pasa por diferencias de huso o de
# fecha de informe del comercio.
MAX_DAYS_AFTER_CLOSING: Final = 5

_DATE_PATTERNS: Final = (
    # ISO: 2026-03-15
    (re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$"), ("y", "m", "d")),
    # es-AR con año: 15/03/2026, 15-03-2026, 15.03.26
    (re.compile(r"^(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{2,4})$"), ("d", "m", "y")),
    # es-AR sin año: 15/03, 15-03
    (re.compile(r"^(\d{1,2})[/\-.](\d{1,2})$"), ("d", "m")),
)


class DateParseError(ValueError):
    """El texto no representa una fecha reconocible."""


@dataclass(frozen=True)
class Period:
    """El mes al que se imputa un resumen."""

    year: int
    month: int

    def __post_init__(self) -> None:
        if not 2000 <= self.year <= 2100:
            raise ValueError(f"año fuera de rango: {self.year}")
        if not 1 <= self.month <= 12:
            raise ValueError(f"mes fuera de rango: {self.month}")

    @property
    def first_day(self) -> date:
        return date(self.year, self.month, 1)

    @property
    def last_day(self) -> date:
        return date(self.year, self.month, calendar.monthrange(self.year, self.month)[1])

    def shift(self, months: int) -> Period:
        """El periodo N meses adelante (o atras, con negativo)."""
        total = self.year * 12 + (self.month - 1) + months
        return Period(total // 12, total % 12 + 1)

    def __str__(self) -> str:
        return f"{self.year:04d}-{self.month:02d}"


def period_from_closing(closing: date) -> Period:
    """Periodo al que corresponde un resumen, dado su cierre.

    Se usa el mes del cierre. Un resumen que cierra el 5 de enero es el resumen de
    enero, aunque la mayoria de sus consumos sean de diciembre: es el mes en que se
    paga, que es el criterio de flujo de caja que usa todo el proyecto.
    """
    return Period(closing.year, closing.month)


def infer_year(day: int, month: int, *, closing: date) -> int:
    """Elige el año de un `DD/MM` sabiendo la fecha de cierre del resumen.

    Prueba el año del cierre y el anterior, y se queda con el candidato que caiga
    dentro de la ventana valida alrededor del cierre. Si los dos caen (no deberia),
    gana el mas cercano al cierre.

        >>> infer_year(20, 12, closing=date(2026, 1, 5))   # cruce de año
        2025
        >>> infer_year(15, 3, closing=date(2026, 3, 28))
        2026
    """
    earliest = closing - timedelta(days=MAX_DAYS_BEFORE_CLOSING)
    latest = closing + timedelta(days=MAX_DAYS_AFTER_CLOSING)

    candidates: list[tuple[int, int]] = []
    for year in (closing.year, closing.year - 1, closing.year + 1):
        try:
            candidate = date(year, month, day)
        except ValueError:
            # 29/02 en un año no bisiesto: ese año no es el correcto.
            continue
        if earliest <= candidate <= latest:
            candidates.append((abs((closing - candidate).days), year))

    if candidates:
        return min(candidates)[1]

    # Ninguno entra en la ventana. Se devuelve el mas cercano de todos modos: la
    # transaccion se persiste y el check de coherencia de fechas la marca para
    # revision. Descartarla seria peor: perderia un gasto en silencio.
    fallbacks = [
        (abs((closing - date(year, month, day)).days), year)
        for year in (closing.year, closing.year - 1)
        if _valid(year, month, day)
    ]
    if not fallbacks:
        raise DateParseError(f"{day:02d}/{month:02d} no es una fecha valida en ningun año cercano")
    return min(fallbacks)[1]


def _valid(year: int, month: int, day: int) -> bool:
    try:
        date(year, month, day)
    except ValueError:
        return False
    return True


def parse_date(raw: str, *, closing: date | None = None) -> date:
    """Parsea una fecha de resumen, inferiendo el año si falta.

    >>> parse_date("2026-03-15")
    datetime.date(2026, 3, 15)
    >>> parse_date("15/03/2026")
    datetime.date(2026, 3, 15)
    >>> parse_date("20/12", closing=date(2026, 1, 5))
    datetime.date(2025, 12, 20)
    """
    text = raw.strip()
    if not text:
        raise DateParseError("fecha vacia")

    for pattern, order in _DATE_PATTERNS:
        match = pattern.match(text)
        if not match:
            continue

        parts = dict(zip(order, (int(g) for g in match.groups()), strict=True))
        day = parts["d"]
        month = parts["m"]

        if "y" not in parts:
            if closing is None:
                raise DateParseError(
                    f"{raw!r} no trae año y no se paso la fecha de cierre para inferirlo"
                )
            year = infer_year(day, month, closing=closing)
        else:
            year = _expand_two_digit_year(parts["y"])

        try:
            return date(year, month, day)
        except ValueError as exc:
            raise DateParseError(f"{raw!r} no es una fecha valida: {exc}") from exc

    raise DateParseError(f"formato de fecha no reconocido: {raw!r}")


def _expand_two_digit_year(year: int) -> int:
    """`26` -> 2026. Un resumen de tarjeta nunca es del siglo XX."""
    if year >= 100:
        return year
    return 2000 + year


def is_plausible_for_statement(value: date, *, closing: date) -> bool:
    """True si la fecha cae en la ventana razonable alrededor del cierre.

    Es el check 5 de la validacion: atrapa años mal inferidos, que es el error mas
    frecuente en el cruce diciembre/enero.
    """
    return (
        closing - timedelta(days=MAX_DAYS_BEFORE_CLOSING)
        <= value
        <= closing + timedelta(days=MAX_DAYS_AFTER_CLOSING)
    )
