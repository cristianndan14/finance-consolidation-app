"""Construccion del extractor segun `LLM_PROVIDER`.

Un unico lugar donde se decide con que se habla. Los servicios reciben el puerto
ya construido, asi que ninguno importa `gemini` ni sabe que existe.

El provider `fake` sin cassette devuelve respuestas vacias. Es util para levantar
la app en un entorno sin API key —- la UI funciona y los documentos quedan en
`needs_review` en lugar de romper el arranque.
"""

from __future__ import annotations

from app.llm.ports import LLMConfigError, LLMExtractor
from app.logging_config import get_logger
from app.settings import Settings

log = get_logger(__name__)


def build(settings: Settings) -> LLMExtractor:
    """El extractor configurado. Levanta `LLMConfigError` si falta algo."""
    provider = settings.llm_provider

    if provider == "gemini":
        from app.llm.gemini import GeminiExtractor

        return GeminiExtractor(settings)

    if provider == "fake":
        from app.llm.fake import FakeExtractor

        log.warning("LLM_PROVIDER=fake: la extraccion no va a producir transacciones reales")
        return FakeExtractor()

    if provider == "mistral":
        # El puerto esta para que agregarlo sea escribir un archivo. Mientras no
        # exista, falla claro en lugar de caer a Gemini por sorpresa y facturar
        # contra una cuenta que el usuario no eligio.
        raise LLMConfigError("el adapter de Mistral todavia no esta implementado")

    raise LLMConfigError(f"LLM_PROVIDER desconocido: {provider!r}")
