"""El puerto del LLM: lo unico que el resto de la aplicacion conoce.

`app/services/parse.py` depende de este Protocol, nunca de `gemini.py`. De ahi
salen dos cosas que importan:

- Los tests de la etapa 2 corren contra `fake.py` con respuestas grabadas:
  gratis, deterministicos y sin red. Sin esta separacion, testear el merge de
  transacciones implicaria pagarle a Google en cada corrida de CI.
- Cambiar de proveedor es escribir un archivo, no tocar el servicio.

# Los tres tipos de error, y por que son tres

El runner decide si reintentar mirando el tipo de excepcion, asi que la
distincion no es decorativa:

- `LLMTransientError` — rate limit, 5xx, timeout. Reintentar tiene sentido: el
  runner lo reencola con backoff.
- `LLMResponseError` — el modelo devolvio algo que no encaja en el schema.
  Reintentar puede funcionar (es no deterministico), asi que tambien se reencola,
  pero se loguea distinto porque suele significar que el prompt necesita trabajo.
- `LLMConfigError` — falta la API key, el modelo no existe. Reintentar es
  garantia de volver a fallar: es permanente.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any, Protocol

from app.schemas.llm import MerchantsPayload, StatementHeaderPayload, TransactionsPayload


class LLMError(Exception):
    """Base de los errores del adapter."""


class LLMTransientError(LLMError):
    """Fallo pasajero: rate limit, 5xx, timeout. Vale la pena reintentar."""


class LLMResponseError(LLMError):
    """El modelo respondio algo que no se puede usar."""

    def __init__(self, message: str, *, raw: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        # Se guarda truncado: sirve para diagnosticar el prompt, y el texto
        # completo de un resumen no tiene por que terminar en un log.
        self.raw = (raw or "")[:2000]


class LLMConfigError(LLMError):
    """Falta configuracion. Reintentar no lo va a arreglar."""


class LLMBudgetExceededError(LLMError):
    """El usuario supero el tope mensual de gasto. Se corta antes de llamar."""

    def __init__(self, spent: Decimal, budget: Decimal) -> None:
        super().__init__(f"gasto mensual de LLM agotado: USD {spent} de USD {budget}")
        self.spent = spent
        self.budget = budget


@dataclass(frozen=True)
class LLMUsage:
    """Consumo de una llamada. Se acumula en `app.llm_usage` por usuario y mes."""

    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: Decimal = Decimal("0")

    def __add__(self, other: LLMUsage) -> LLMUsage:
        return LLMUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cost_usd=self.cost_usd + other.cost_usd,
        )


@dataclass(frozen=True)
class LLMResult[T]:
    """Lo que devolvio el modelo, mas todo lo que hace falta para auditarlo.

    `raw` se persiste entero en `extraction_runs.raw_response` a proposito:
    permite re-derivar transacciones con logica de parseo nueva sin volver a
    llamar (ni pagar) al modelo, y deja saber que produjo cada dato cuando algo
    se ve raro tres meses despues.
    """

    data: T
    raw: dict[str, Any]
    provider: str
    model: str
    prompt_version: str
    usage: LLMUsage = field(default_factory=LLMUsage)
    latency_ms: int = 0


@dataclass(frozen=True)
class HeaderHints:
    """Lo poco que se sabe del documento antes de leerlo.

    Existe para que el modelo no tenga que adivinar cosas que el sistema ya sabe
    (el emisor de la tarjeta asociada, por ejemplo) y para acotar la ventana de
    fechas plausibles.
    """

    issuer_name: str | None = None
    card_last4: str | None = None
    uploaded_on: date | None = None


class LLMExtractor(Protocol):
    """Las llamadas que la etapa 2 necesita.

    Tres, no una: el header y las transacciones son schemas de tamaño muy
    distinto, y un schema chico se cumple con mucha mas precision. Pedir las dos
    cosas en una sola llamada empeora las dos.
    """

    provider: str
    model: str

    async def extract_statement_header(
        self, *, text: str, hints: HeaderHints, prompt_version: str
    ) -> LLMResult[StatementHeaderPayload]: ...

    async def extract_transactions(
        self,
        *,
        text_chunk: str,
        chunk_index: int,
        header: StatementHeaderPayload,
        prompt_version: str,
    ) -> LLMResult[TransactionsPayload]: ...

    async def repair_transactions(
        self,
        *,
        text: str,
        extracted_summary: str,
        delta_description: str,
        prompt_version: str,
    ) -> LLMResult[TransactionsPayload]: ...

    async def normalize_merchants(
        self,
        *,
        raw_keys: list[str],
        category_slugs: list[str],
        prompt_version: str,
    ) -> LLMResult[MerchantsPayload]:
        """Descripciones crudas -> comercio y categoria, en un solo batch.

        Recibe **claves normalizadas**, no descripciones enteras, y ninguna
        informacion de monto o fecha: lo unico que se le pide decidir es que
        comercio es. Menos contexto es mas barato y, sobre todo, deja menos
        lugar a que invente correlaciones que no existen.
        """
        ...

    async def aclose(self) -> None: ...
