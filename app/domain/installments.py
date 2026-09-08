"""Cuotas: flujo de caja, devengado y proyeccion de lo comprometido.

# Las dos preguntas que no son la misma

Una compra de $120.000 en 12 cuotas admite dos lecturas, y las dos son utiles:

- **Flujo de caja** (el modelo principal del proyecto): en marzo pagaste $10.000.
  Es lo que sale de tu cuenta ese mes y lo que el resumen te cobra. Es el numero que
  importa para saber si te alcanza la plata.
- **Devengado**: en marzo gastaste $120.000. Es lo que importa para saber cuanto
  consumis realmente, sin que las cuotas disimulen una compra grande.

El esquema guarda lo necesario para las dos: `amount` es la cuota del mes,
`purchase_total_amount` e `installment_total` permiten reconstruir el devengado.

# Y la tercera, que no aparece en ningun resumen

Cuanto de los meses que vienen ya esta gastado. Un resumen te dice lo que debes
ahora; no te dice que arrastras nueve cuotas de algo que compraste en enero.
`remaining_commitment` calcula eso, y es probablemente la vista mas util del
dashboard.

Nota sobre los intereses: en Argentina las cuotas casi nunca son la division exacta
del precio. `cuota * total` suele ser mayor que el precio de lista porque incluye el
costo de financiacion. Por eso `expected_installment_amount` tiene una tolerancia
amplia y `accrued_amount` prefiere el total informado por el emisor antes que
multiplicar.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from app.domain.dates import Period
from app.domain.money import quantize

# Las cuotas argentinas incluyen interes, asi que `cuota * n` puede superar
# bastante el precio de lista. La tolerancia es amplia a proposito: sirve para
# detectar un error de lectura (un monto de otra columna), no para auditar el CFT.
INSTALLMENT_AMOUNT_TOLERANCE = Decimal("0.15")

MAX_INSTALLMENTS = 120


class InstallmentError(ValueError):
    """Los datos de cuotas son incoherentes."""


@dataclass(frozen=True)
class Installment:
    """Una cuota tal como aparece en un resumen."""

    number: int
    total: int
    amount: Decimal
    purchase_total_amount: Decimal | None = None

    def __post_init__(self) -> None:
        if not 1 <= self.total <= MAX_INSTALLMENTS:
            raise InstallmentError(f"total de cuotas fuera de rango: {self.total}")
        if not 1 <= self.number <= self.total:
            raise InstallmentError(f"cuota {self.number} de {self.total} no es coherente")
        if self.amount < 0:
            raise InstallmentError(f"el monto de la cuota no puede ser negativo: {self.amount}")

    @property
    def is_last(self) -> bool:
        return self.number == self.total

    @property
    def remaining_count(self) -> int:
        """Cuotas que faltan pagar despues de esta."""
        return self.total - self.number

    @property
    def remaining_amount(self) -> Decimal:
        """Plata que falta pagar de esta compra."""
        return quantize(self.amount * self.remaining_count)

    @property
    def accrued_amount(self) -> Decimal:
        """Monto devengado: el total de la compra.

        Prefiere el total que informa el emisor; si no lo trae, lo estima
        multiplicando. La estimacion sobrestima el precio de lista cuando hay
        interes, y es lo mejor disponible con la informacion del resumen.
        """
        if self.purchase_total_amount is not None:
            return quantize(self.purchase_total_amount)
        return quantize(self.amount * self.total)


def expected_installment_amount(total_amount: Decimal, installments: int) -> Decimal:
    """Cuanto deberia ser cada cuota si no hubiera interes."""
    if installments < 1:
        raise InstallmentError(f"cantidad de cuotas invalida: {installments}")
    return quantize(total_amount / installments)


def amount_is_plausible(
    *,
    amount: Decimal,
    purchase_total_amount: Decimal,
    installments: int,
    tolerance: Decimal = INSTALLMENT_AMOUNT_TOLERANCE,
) -> bool:
    """True si el monto de la cuota es coherente con el total informado.

    Es el check 6 de la validacion. Atrapa el caso en que el LLM leyo el monto de
    otra columna del resumen (el total en lugar de la cuota, tipicamente), que
    inflaria el gasto del mes en un orden de magnitud.

    La comparacion es asimetrica a proposito: una cuota MAYOR a la division exacta
    es normal (interes), una MENOR no tiene explicacion.
    """
    if installments < 1 or purchase_total_amount <= 0:
        return False

    exact = expected_installment_amount(purchase_total_amount, installments)
    if amount < exact * (Decimal(1) - tolerance):
        return False
    # Cota superior generosa: el CFT de 12 cuotas puede duplicar el precio.
    return amount <= exact * (Decimal(1) + tolerance) * Decimal("2.5")


@dataclass(frozen=True)
class Commitment:
    """Cuotas pendientes de una compra, proyectadas hacia adelante."""

    group_key: str
    currency: str
    merchant_name: str
    installment_amount: Decimal
    installments_left: int
    next_period: Period

    @property
    def total_remaining(self) -> Decimal:
        return quantize(self.installment_amount * self.installments_left)

    def schedule(self) -> list[tuple[Period, Decimal]]:
        """Cuanto cae en cada mes futuro.

        Devuelve una lista de (periodo, monto), un elemento por cuota pendiente. Es
        lo que alimenta el grafico de compromisos futuros del dashboard.
        """
        return [
            (self.next_period.shift(offset), self.installment_amount)
            for offset in range(self.installments_left)
        ]


def remaining_commitment(
    *,
    group_key: str,
    currency: str,
    merchant_name: str,
    last_installment: Installment,
    last_period: Period,
) -> Commitment | None:
    """Proyecta las cuotas que faltan de una compra. None si ya termino."""
    if last_installment.remaining_count <= 0:
        return None

    return Commitment(
        group_key=group_key,
        currency=currency,
        merchant_name=merchant_name,
        installment_amount=last_installment.amount,
        installments_left=last_installment.remaining_count,
        next_period=last_period.shift(1),
    )


def infer_purchase_period(installment: Installment, posted_period: Period) -> Period:
    """En que mes se hizo la compra, dado el periodo en que cae la cuota N.

    Se asume una cuota por mes, que es como funcionan las cuotas de tarjeta en
    Argentina. La cuota 1 cae el mes de la compra, asi que la cuota N cae N-1 meses
    despues.
    """
    return posted_period.shift(-(installment.number - 1))


def purchase_date_or_estimate(
    installment: Installment, posted_period: Period, purchase_date: date | None
) -> date:
    """Fecha de la compra: la informada, o el primer dia del mes estimado.

    Cuando el emisor no informa la fecha original — frecuente en cuotas viejas — se
    usa el primer dia del mes inferido. Sirve para agrupar en la vista devengada; no
    pretende ser exacta.
    """
    if purchase_date is not None:
        return purchase_date
    return infer_purchase_period(installment, posted_period).first_day
