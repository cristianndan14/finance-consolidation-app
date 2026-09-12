"""Lo que el LLM devuelve, validado en el borde.

# Por que los montos viajan como STRING y no como number

Un JSON `number` llega a Python como `float`, y `float` no puede representar
`0.1` exactamente. Sobre doscientas transacciones el error se acumula y rompe la
validacion mas importante del proyecto — el cuadre contra el total del resumen —
de una forma indistinguible de "el modelo se comio una linea".

Asi que el schema que se le manda al modelo pide `"1234.56"` con un regex, y
Pydantic lo convierte a `Decimal` aca, en el borde, una sola vez. De aca para
adentro no existe ningun float.

`parse_amount` acepta ademas el formato es-AR (`1.234,56`) como red de seguridad:
si el modelo ignora el formato pedido, se interpreta bien igual en lugar de
fallar el documento entero.

# Por que las fechas quedan como texto

El modelo ve `15/03` en el PDF, sin año. Podria inferirlo, pero es justo donde
mas se equivoca (el cruce diciembre/enero manda los consumos un año al futuro).
El texto crudo se guarda tal cual y el año lo pone `app.domain.dates.infer_year`,
que resuelve el cruce con la fecha de cierre. Es deterministico y testeable; la
inferencia del modelo no.

# Por que `direction` va separado del signo del monto

Los resumenes argentinos marcan un credito con sufijos que no tienen consenso
entre emisores (`CR`, `-`, `H`, `HABER`). Interpretar el signo es fragil; pedir
un entero explicito, no. El monto siempre es positivo — la base lo exige con
`check (amount >= 0)` — y el sentido vive en `direction`.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, field_validator

from app.domain.money import AmountParseError, parse_amount

Currency = Literal["ARS", "USD"]


def _to_direction(value: Any) -> Any:
    """`"-1"` -> `-1`.

    Gemini no admite `enum` sobre un `integer`, asi que el schema declara
    `direction` como un enum de strings (`"1"` / `"-1"`) y el modelo devuelve
    texto. Pydantic **no** coacciona un string a un `Literal[-1, 1]` ni en modo
    lax, asi que sin esto toda extraccion real fallaria la validacion entera.
    """
    if isinstance(value, str):
        text = value.strip().lstrip("+")
        if text in ("1", "-1"):
            return int(text)
    return value


# 1 aumenta la deuda (consumo, interes, impuesto). -1 la baja (pago, devolucion).
Direction = Annotated[Literal[-1, 1], BeforeValidator(_to_direction)]

# Enum cerrado y no texto libre: los impuestos (IVA, percepcion RG 4815, IIBB,
# sellos) son ~30% de las lineas de un resumen argentino y no son consumo.
# Separarlos ya en la extraccion evita ensuciar el dashboard despues.
TransactionKind = Literal[
    "charge",
    "installment",
    "payment",
    "refund",
    "interest",
    "fee",
    "tax",
    "fx_charge",
    "adjustment",
    "unknown",
]

MAX_INSTALLMENTS = 120


def _to_amount(value: Any) -> Any:
    """Texto -> Decimal positivo. El sentido lo lleva `direction`."""
    if value is None or isinstance(value, Decimal):
        return value
    try:
        return abs(parse_amount(value))
    except AmountParseError as exc:
        raise ValueError(str(exc)) from exc


def _blank_to_none(value: Any) -> Any:
    """Los modelos devuelven `""` y `"null"` donde deberian omitir el campo."""
    if isinstance(value, str) and value.strip().lower() in ("", "null", "none", "n/a", "-"):
        return None
    return value


def _to_signed_amount(value: Any) -> Any:
    """Texto -> Decimal conservando el signo.

    Para los SALDOS de la cabecera, a diferencia de los montos de transaccion.
    Un saldo puede ser legitimamente negativo: Mercado Pago imprime
    `Saldo a favor del periodo anterior  -$ 179.367,27` cuando pagaste de mas, y
    perder ese signo convierte un credito de 179 mil pesos en una deuda de 179
    mil pesos. El `abs()` que corresponde al monto de una transaccion —donde el
    sentido lo lleva `direction`— aca es un error de 358 mil.
    """
    if value is None or isinstance(value, Decimal):
        return value
    try:
        return parse_amount(value)
    except AmountParseError as exc:
        raise ValueError(str(exc)) from exc


Amount = Annotated[Decimal, BeforeValidator(_to_amount)]
OptionalAmount = Annotated[
    Decimal | None, BeforeValidator(_blank_to_none), BeforeValidator(_to_amount)
]
# Los saldos conservan el signo; las magnitudes de la cabecera (consumos, pagos)
# van en positivo porque la formula `previous - payments + charges` ya les asigna
# el sentido, y un signo duplicado la invertiria.
OptionalBalance = Annotated[
    Decimal | None, BeforeValidator(_blank_to_none), BeforeValidator(_to_signed_amount)
]
OptionalText = Annotated[str | None, BeforeValidator(_blank_to_none)]


class LLMTransaction(BaseModel):
    """Una linea de consumo tal como la leyo el modelo, sin normalizar."""

    # `extra="ignore"`: que el modelo invente un campo de mas no debe tirar el
    # documento entero. Que falte uno obligatorio, si.
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    posted_date: str
    transaction_date: OptionalText = None
    description_raw: str = Field(min_length=1)

    # El ancla anti-alucinacion: la linea literal del PDF de la que salio el
    # dato. Se verifica por similitud contra el texto de entrada (check 3 de la
    # validacion) y la pantalla de revision la muestra al lado del dato.
    source_line: str = Field(min_length=1)
    source_page: int | None = None

    amount: Amount
    currency: Currency
    direction: Direction
    kind: TransactionKind = "unknown"

    installment_number: int | None = None
    installment_total: int | None = None
    purchase_total_amount: OptionalAmount = None
    purchase_date: OptionalText = None

    confidence: float | None = Field(default=None, ge=0, le=1)

    @field_validator("installment_number", "installment_total")
    @classmethod
    def _sane_installments(cls, value: int | None) -> int | None:
        """Fuera de rango se descarta en vez de fallar.

        Un `12/0` o un `3/999` es ruido de lectura. La transaccion en si sigue
        siendo valida y vale mas guardarla sin el dato de cuotas que perderla.
        """
        if value is None or not 1 <= value <= MAX_INSTALLMENTS:
            return None
        return value

    @property
    def has_installments(self) -> bool:
        return (
            self.installment_number is not None
            and self.installment_total is not None
            and self.installment_number <= self.installment_total
        )


class TransactionsPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    transactions: list[LLMTransaction] = Field(default_factory=list)
    # Canal para que el modelo diga "esta pagina parece cortada" en lugar de
    # inventar el resto. Queda en el ValidationReport.
    warnings: list[str] = Field(default_factory=list)


class LLMStatementHeader(BaseModel):
    """Los totales de una seccion del resumen. Uno por moneda."""

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    currency: Currency
    closing_date: OptionalText = None
    due_date: OptionalText = None

    previous_balance: OptionalBalance = None
    payments_credits: OptionalAmount = None
    # El cuadre se valida contra ESTE campo. `total_due` incluye el saldo
    # anterior; validar la suma de transacciones contra el es el error clasico.
    new_charges: OptionalAmount = None
    total_due: OptionalBalance = None
    minimum_payment: OptionalAmount = None

    card_last4: OptionalText = None
    issuer_name: OptionalText = None

    @field_validator("card_last4")
    @classmethod
    def _only_digits(cls, value: str | None) -> str | None:
        if value is None:
            return None
        digits = "".join(ch for ch in value if ch.isdigit())
        return digits[-4:] if len(digits) >= 4 else None


class StatementHeaderPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    statements: list[LLMStatementHeader] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    def for_currency(self, currency: str) -> LLMStatementHeader | None:
        for statement in self.statements:
            if statement.currency == currency.upper():
                return statement
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Enriquecimiento (Fase 5): descripcion cruda -> comercio + categoria
# ─────────────────────────────────────────────────────────────────────────────
class MerchantMapping(BaseModel):
    """Lo que el modelo dice que hay detras de una descripcion de resumen."""

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    # La clave con la que se pregunto. Se devuelve para poder aparear la
    # respuesta con la pregunta: el modelo puede reordenar o saltear elementos,
    # y aparear por posicion asignaria el comercio equivocado en silencio.
    raw_key: str = Field(min_length=1)
    canonical_name: str = Field(min_length=1)
    # Un slug de la lista cerrada que se le pasa. Si no encaja en ninguno, el
    # modelo devuelve null y la transaccion queda sin categoria, que es honesto.
    category_slug: OptionalText = None
    confidence: float | None = Field(default=None, ge=0, le=1)


class MerchantsPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    merchants: list[MerchantMapping] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
