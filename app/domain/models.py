"""Las transacciones y cabeceras ya normalizadas, listas para persistir.

# Por que hay dos representaciones y no una

`app/schemas/llm.py` describe lo que **dijo el modelo**: fechas como texto, el año
sin resolver, las cuotas como vinieron. Este modulo describe lo que el sistema
**cree**: fechas resueltas a `date`, la clave de dedupe calculada, el grupo de
cuotas armado.

Separarlas permite que la conversion sea una funcion pura, testeable con casos
borde escritos a mano, en vez de logica desperdigada en el servicio. Y deja claro
donde termina el dato ajeno y empieza el dato del sistema — que en un pipeline
alimentado por un LLM no es un detalle de estilo.

La conversion nunca inventa: si una fecha no se puede resolver, la transaccion se
descarta con el motivo, y el descarte queda en el reporte de validacion. Una
transaccion con una fecha adivinada aparece en el mes equivocado del dashboard y
nadie se entera nunca.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from app.domain import dedupe as dedupe_mod
from app.domain.dates import (
    DateParseError,
    is_plausible_for_statement,
    parse_date,
    period_from_closing,
)
from app.domain.money import quantize, signed
from app.schemas.llm import LLMStatementHeader, LLMTransaction


class NormalizationError(ValueError):
    """La transaccion que devolvio el modelo no se puede usar."""


@dataclass(frozen=True)
class ParsedStatementHeader:
    """Los totales de una moneda, ya con las fechas resueltas."""

    currency: str
    closing_date: date | None = None
    due_date: date | None = None
    previous_balance: Decimal | None = None
    payments_credits: Decimal | None = None
    new_charges: Decimal | None = None
    total_due: Decimal | None = None
    minimum_payment: Decimal | None = None
    card_last4: str | None = None
    issuer_name: str | None = None

    @property
    def net_movement(self) -> Decimal | None:
        """Lo que se movio la cuenta en el periodo: `total_due - previous_balance`.

        Es una identidad contable del resumen y no depende de como el emisor
        reparta la informacion, asi que es la referencia mas confiable para
        contrastar la suma de las transacciones.
        """
        if self.total_due is None or self.previous_balance is None:
            return None
        return quantize(self.total_due - self.previous_balance)

    @property
    def period(self) -> tuple[int, int] | None:
        """(año, mes) al que se imputa el resumen, si se conoce el cierre."""
        if self.closing_date is None:
            return None
        period = period_from_closing(self.closing_date)
        return (period.year, period.month)

    @classmethod
    def from_llm(
        cls, header: LLMStatementHeader, *, fallback_closing: date | None = None
    ) -> ParsedStatementHeader:
        closing = _optional_date(header.closing_date) or fallback_closing
        return cls(
            currency=header.currency,
            closing_date=closing,
            # El vencimiento es posterior al cierre, asi que el cierre sirve de
            # ancla para inferirle el año si vino sin el.
            due_date=_optional_date(header.due_date, closing=closing),
            previous_balance=header.previous_balance,
            payments_credits=header.payments_credits,
            new_charges=header.new_charges,
            total_due=header.total_due,
            minimum_payment=header.minimum_payment,
            card_last4=header.card_last4,
            issuer_name=header.issuer_name,
        )


@dataclass(frozen=True)
class ParsedTransaction:
    """Una transaccion normalizada. Es lo que se inserta en `app.transactions`."""

    posted_date: date
    description_raw: str
    amount: Decimal
    currency: str
    direction: int
    kind: str
    dedupe_key: str

    source_line: str
    source_page: int | None = None
    transaction_date: date | None = None

    installment_number: int | None = None
    installment_total: int | None = None
    purchase_total_amount: Decimal | None = None
    purchase_date: date | None = None
    installment_group_key: str | None = None

    confidence: Decimal | None = None

    @property
    def signed_amount(self) -> Decimal:
        """Monto con signo. Es lo que se suma para el cuadre."""
        return signed(self.amount, self.direction)

    @property
    def is_installment(self) -> bool:
        return self.installment_number is not None and self.installment_total is not None

    def with_confidence(self, value: Decimal) -> ParsedTransaction:
        from dataclasses import replace

        return replace(self, confidence=value)


def normalize_transaction(
    tx: LLMTransaction, *, closing: date | None, default_page: int | None = None
) -> ParsedTransaction:
    """Convierte lo que dijo el modelo en lo que el sistema guarda.

    `closing` es lo que permite resolver un `15/03` sin año. Si no se conoce (el
    modelo no pudo leer la cabecera) se exige que la fecha venga completa: sin
    ancla, cualquier año es una invencion.
    """
    posted = _required_date(tx.posted_date, closing=closing, field="posted_date")

    number = tx.installment_number if tx.has_installments else None
    total = tx.installment_total if tx.has_installments else None

    purchase_date = _optional_date(tx.purchase_date, closing=closing)

    # Varios emisores imprimen, en la columna de fecha de una cuota, la fecha de
    # la COMPRA ORIGINAL y no la del resumen que la cobra. Sin corregirlo, una
    # cuota 4/12 comprada en mayo aparece como gasto de mayo en un resumen de
    # agosto, y el dashboard —que es de flujo de caja— muestra el mes al que la
    # cuota no pertenece.
    posted, purchase_date = _relocate_installment_date(
        posted, purchase_date, number=number, closing=closing
    )

    group_key = (
        dedupe_mod.installment_group_key(
            description=tx.description_raw,
            installment_total=total,
            purchase_total_amount=tx.purchase_total_amount,
            purchase_date=purchase_date,
        )
        if total is not None
        else None
    )

    return ParsedTransaction(
        posted_date=posted,
        transaction_date=_optional_date(tx.transaction_date, closing=closing),
        description_raw=tx.description_raw,
        source_line=tx.source_line,
        source_page=tx.source_page or default_page,
        amount=quantize(tx.amount),
        currency=tx.currency,
        direction=int(tx.direction),
        kind=tx.kind,
        installment_number=number,
        installment_total=total,
        purchase_total_amount=tx.purchase_total_amount,
        purchase_date=purchase_date,
        installment_group_key=group_key,
        confidence=Decimal(str(tx.confidence)).quantize(Decimal("0.01"))
        if tx.confidence is not None
        else None,
        dedupe_key=dedupe_mod.dedupe_key(
            description=tx.description_raw,
            posted_date=posted,
            amount=quantize(tx.amount),
            currency=tx.currency,
            installment_number=number,
            installment_total=total,
        ),
    )


def _relocate_installment_date(
    posted: date, purchase_date: date | None, *, number: int | None, closing: date | None
) -> tuple[date, date | None]:
    """Mueve a `purchase_date` la fecha de compra que el emisor puso donde va la del resumen.

    # El problema

    Galicia y BNA imprimen, en la columna de fecha de una cuota, la fecha de la
    compra original:

        * PUMA ABASTO 06/06        21/03      17.499,87
        WWW.FRAVEGA.COM C.04/12    13/05     124.999,91

    en un resumen que cierra el 27/08. El modelo copia esa fecha —que es
    exactamente lo que se le pide— y la cuota termina contada como gasto de marzo
    o de mayo. El dashboard del proyecto es de flujo de caja: esa cuota se paga en
    agosto y tiene que aparecer en agosto.

    # Por que la condicion es doble

    Mover la fecha cada vez que cae lejos del cierre seria adivinar. Aca se exige
    ademas que el mes coincida **exactamente** con el que corresponderia a la
    primera cuota (`periodo - (N-1)` meses), que es una prediccion falsable: si
    coincide, la interpretacion "esto es la fecha de compra" queda confirmada por
    la aritmetica de las cuotas, no supuesta.

    Con las dos condiciones, un emisor que si imprima la fecha del resumen no se
    toca nunca, porque su fecha cae dentro de la ventana.

    # Que queda como `posted_date`

    La fecha de cierre del resumen. Es la unica fecha real del periodo que se
    conoce; inventar un dia dentro del mes (el mismo dia que la compra, por
    ejemplo) seria fabricar un dato que no esta en ningun lado.
    """
    if number is None or number < 2 or closing is None:
        return posted, purchase_date

    if is_plausible_for_statement(posted, closing=closing):
        return posted, purchase_date

    expected = period_from_closing(closing).shift(-(number - 1))
    if (posted.year, posted.month) != (expected.year, expected.month):
        # Esta lejos del cierre pero no donde caeria la compra: puede ser un año
        # mal inferido u otra cosa. No se toca, y el check de coherencia de
        # fechas la marca para que la mires vos.
        return posted, purchase_date

    # Si el modelo ya habia informado la fecha de compra por separado, esa manda.
    return closing, purchase_date or posted


@dataclass(frozen=True)
class Discarded:
    """Una transaccion que no se pudo normalizar, con el motivo."""

    index: int
    reason: str
    source_line: str


def normalize_all(
    transactions: list[LLMTransaction], *, closing: date | None
) -> tuple[list[ParsedTransaction], list[Discarded]]:
    """Normaliza el lote entero. Una linea rota no tira las demas.

    Devolver los descartes en vez de tragarlos es lo que permite que la pantalla
    de revision diga "el modelo devolvio 84 lineas, 2 no se pudieron usar" en
    lugar de mostrar 82 y que el usuario no sepa que falta algo.
    """
    parsed: list[ParsedTransaction] = []
    discarded: list[Discarded] = []

    for index, tx in enumerate(transactions):
        try:
            parsed.append(normalize_transaction(tx, closing=closing))
        except (NormalizationError, DateParseError) as exc:
            discarded.append(Discarded(index=index, reason=str(exc), source_line=tx.source_line))

    return parsed, discarded


# ─── Helpers ────────────────────────────────────────────────────────────────
def _optional_date(raw: str | None, *, closing: date | None = None) -> date | None:
    if not raw:
        return None
    try:
        return parse_date(raw, closing=closing)
    except DateParseError:
        # Una fecha secundaria ilegible (la de la compra original) no justifica
        # perder la transaccion entera.
        return None


def _required_date(raw: str, *, closing: date | None, field: str) -> date:
    try:
        return parse_date(raw, closing=closing)
    except DateParseError as exc:
        raise NormalizationError(f"{field}: {exc}") from exc
