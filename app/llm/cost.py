"""Costo de una llamada, en dolares.

Existe porque el tope de gasto (`LLM_MONTHLY_BUDGET_USD`) tiene que poder
cortar **antes** de encolar, y para eso hay que saber cuanto se lleva gastado. Un
bucle de reprocesamiento sin esto vacia la cuenta sin que nadie se entere hasta
la factura.

Los precios estan hardcodeados a proposito: son tres numeros que cambian una vez
por año, y pedirlos por API agregaria una llamada de red a un camino que tiene
que ser sincronico y confiable. Si un modelo no esta en la tabla se devuelve 0 y
se loguea: **sub-estimar el costo es preferible a fallar la extraccion**, porque
el tope es una red de seguridad y no un sistema de facturacion.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Final

from app.logging_config import get_logger

log = get_logger(__name__)

MILLION: Final = Decimal("1000000")


# USD por millon de tokens. Precios de lista de septiembre de 2026.
PRICES: Final[dict[str, tuple[Decimal, Decimal]]] = {
    # modelo: (input, output)
    "gemini-2.5-flash": (Decimal("0.30"), Decimal("2.50")),
    "gemini-2.5-flash-lite": (Decimal("0.10"), Decimal("0.40")),
    "gemini-2.5-pro": (Decimal("1.25"), Decimal("10.00")),
    "mistral-small-latest": (Decimal("0.20"), Decimal("0.60")),
    # El fake no cuesta nada, pero tiene que estar para que el camino de costo se
    # ejercite igual en los tests.
    "fake": (Decimal("0"), Decimal("0")),
}

_warned: set[str] = set()


def estimate(model: str, *, input_tokens: int, output_tokens: int) -> Decimal:
    """Costo de una llamada. Seis decimales, que es lo que guarda la columna."""
    prices = PRICES.get(model)
    if prices is None:
        prices = _fallback(model)

    input_price, output_price = prices
    total = (Decimal(input_tokens) * input_price + Decimal(output_tokens) * output_price) / MILLION
    return total.quantize(Decimal("0.000001"))


def _fallback(model: str) -> tuple[Decimal, Decimal]:
    """Un modelo desconocido no corta la extraccion, pero avisa una vez."""
    if model not in _warned:
        _warned.add(model)
        log.warning(
            "modelo sin precio en la tabla: el gasto va a quedar subestimado",
            model=model,
            known=sorted(PRICES),
        )
    return (Decimal("0"), Decimal("0"))
