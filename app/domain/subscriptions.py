"""Deteccion de gastos recurrentes: que de lo que se paga todos los meses es una suscripcion.

# Que se busca y que no

Lo que interesa es el gasto que se renueva solo: streaming, software, el
gimnasio. Lo que se detecta aca entra a `app.subscriptions` con estado
`suspected` — la deteccion nunca afirma, propone; el usuario confirma.

# Los dos falsos positivos que importan

**Las cuotas.** Una compra en 12 cuotas es, linea por linea, indistinguible de
una suscripcion: mismo comercio, mismo monto, todos los meses. La diferencia la
da el dato que ya esta en la fila (`installment_total`), asi que se descartan
antes de mirar nada mas. Sin esto, cada compra financiada del año aparece como
una suscripcion que el usuario tiene que rechazar a mano.

**Los cargos del banco.** Los intereses, las comisiones y los impuestos tambien
son mensuales y de monto parecido. Son recurrentes, pero no son una suscripcion
que se pueda dar de baja, que es lo unico que esta pantalla sirve para decidir.

# Por que la tolerancia de monto se mide contra el mes anterior

Comparar cada monto contra la mediana de la serie funcionaria en una economia
estable. Con inflacion de dos digitos, una suscripcion real que arranca en 3.000
y termina en 6.000 se ve como "montos que no se parecen" y no se detecta nunca,
mientras que un supermercado con montos que saltan al azar puede tener una
mediana centrada. Comparar cada mes contra el anterior distingue las dos cosas:
lo que sube parejo es un precio que se ajusta; lo que salta para los dos lados es
consumo variable.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from itertools import pairwise
from typing import Final

# Movimientos que son del banco, no de un comercio al que uno se suscribe.
EXCLUDED_KINDS: Final[frozenset[str]] = frozenset(
    {"tax", "interest", "fee", "fx_charge", "payment", "refund"}
)

# Tres apariciones es el minimo que distingue una serie de una coincidencia: con
# dos, cualquier par de compras separadas por un mes califica.
MIN_OCCURRENCES: Final = 3

# Un mes salteado se tolera (el cargo cayo despues del cierre y entro en el
# resumen siguiente); dos ya es otra cosa.
MAX_MONTH_GAP: Final = 2

# Cuanto puede cambiar el monto de un mes al siguiente y seguir siendo el mismo
# precio. Holgado a proposito: un aumento de precio de una suscripcion argentina
# entra comodo, y el falso positivo que esto habilita lo filtra la persona.
AMOUNT_STEP_TOLERANCE: Final = Decimal("0.25")


@dataclass(frozen=True)
class Occurrence:
    """Una transaccion candidata a ser una cuota de la serie."""

    transaction_id: str
    posted_date: date
    amount: Decimal
    currency: str
    kind: str
    installment_total: int | None = None
    direction: int = 1


@dataclass(frozen=True)
class SubscriptionCandidate:
    """Una serie que parece una suscripcion. Todo lo que hace falta para persistirla."""

    label: str
    merchant_id: str
    nominal_amount: Decimal
    currency: str
    cadence: str
    first_seen: date
    last_seen: date
    transaction_ids: tuple[str, ...]

    @property
    def occurrences(self) -> int:
        return len(self.transaction_ids)


def detect(
    *, label: str, merchant_id: str, occurrences: Iterable[Occurrence]
) -> SubscriptionCandidate | None:
    """La serie de un comercio -> una suscripcion, o `None` si no lo es.

    Las ocurrencias pueden venir en cualquier orden y de cualquier moneda: se
    filtran y se ordenan aca. `None` es una respuesta esperada y frecuente — la
    mayoria de los comercios no son suscripciones.
    """
    series = _monthly_series(occurrences)
    if len(series) < MIN_OCCURRENCES:
        return None

    for previous, current in pairwise(series):
        if not _consecutive_enough(previous, current):
            return None
        if not _same_price(previous.amount, current.amount):
            return None

    return SubscriptionCandidate(
        label=label,
        merchant_id=merchant_id,
        # El ultimo monto, no el promedio: lo que importa de una suscripcion es
        # lo que se va a pagar el mes que viene, no lo que se pago en marzo.
        nominal_amount=series[-1].amount,
        currency=series[-1].currency,
        cadence="monthly",
        first_seen=series[0].posted_date,
        last_seen=series[-1].posted_date,
        transaction_ids=tuple(item.transaction_id for item in series),
    )


def detect_all(
    groups: Iterable[tuple[str, str, Sequence[Occurrence]]],
) -> list[SubscriptionCandidate]:
    """`detect` sobre varios comercios. Cada tupla es `(merchant_id, label, ocurrencias)`."""
    found = []
    for merchant_id, label, occurrences in groups:
        candidate = detect(label=label, merchant_id=merchant_id, occurrences=occurrences)
        if candidate is not None:
            found.append(candidate)
    return found


# ─────────────────────────────────────────────────────────────────────────────
# Interno
# ─────────────────────────────────────────────────────────────────────────────
def _month_index(day: date) -> int:
    return day.year * 12 + day.month


def _eligible(item: Occurrence) -> bool:
    return (
        item.direction == 1
        and item.kind not in EXCLUDED_KINDS
        and item.installment_total is None
        and item.amount > 0
    )


def _monthly_series(occurrences: Iterable[Occurrence]) -> list[Occurrence]:
    """La racha vigente de la moneda que mas meses cubre: un candidato por mes.

    Dos cargos del mismo comercio en el mismo mes no invalidan la serie (una
    suscripcion puede convivir con una compra suelta): de cada mes se elige la
    ocurrencia mas parecida en monto a la del mes anterior, no la ultima por
    fecha. Elegir siempre la ultima hacia que una compra suelta posterior al
    cargo de la suscripcion —un gimnasio que ademas vende indumentaria, un
    comercio con plan— se convirtiera en el representante del mes, rompiera la
    tolerancia de precio contra el mes previo, y tirara abajo la serie entera en
    vez de solo ese mes.

    Por el mismo motivo un mes atipico no mata la racha: corta la serie que
    veniamos armando y arranca una nueva desde ese mes, asi que un mes raro en
    medio de 18 meses de historial no le cuesta la deteccion a los otros 17.
    """
    eligible = [item for item in occurrences if _eligible(item)]
    if not eligible:
        return []

    by_currency: dict[str, dict[int, list[Occurrence]]] = {}
    for item in sorted(eligible, key=lambda i: i.posted_date):
        month_key = _month_index(item.posted_date)
        by_currency.setdefault(item.currency, {}).setdefault(month_key, []).append(item)

    # Mezclar monedas daria una serie con saltos de monto que no son de precio.
    months = max(by_currency.values(), key=len)
    return _trailing_run(months)


def _trailing_run(months: dict[int, list[Occurrence]]) -> list[Occurrence]:
    """La racha que llega hasta el mes mas reciente: la suscripcion, si sigue viva."""
    indices = sorted(months)
    run: list[Occurrence] = [months[indices[0]][-1]]

    for index in indices[1:]:
        previous = run[-1]
        candidates = months[index]
        pick = min(candidates, key=lambda item: abs(item.amount - previous.amount))

        if _consecutive_enough(previous, pick) and _same_price(previous.amount, pick.amount):
            run.append(pick)
        else:
            run = [pick]

    return run


def _consecutive_enough(previous: Occurrence, current: Occurrence) -> bool:
    gap = _month_index(current.posted_date) - _month_index(previous.posted_date)
    return 1 <= gap <= MAX_MONTH_GAP


def _same_price(previous: Decimal, current: Decimal) -> bool:
    return abs(current - previous) <= previous * AMOUNT_STEP_TOLERANCE
