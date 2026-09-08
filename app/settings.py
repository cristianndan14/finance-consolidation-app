"""Configuracion de la aplicacion, leida del entorno.

`SecretStr` en todo lo que sea credencial: evita que una key termine impresa en un
traceback o en un log estructurado por el repr del objeto de settings.
"""

from __future__ import annotations

from decimal import Decimal
from functools import lru_cache
from typing import Literal

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Env = Literal["development", "test", "production"]
LLMProvider = Literal["gemini", "mistral", "fake"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ─── Entorno ─────────────────────────────────────────────────────────────
    env: Env = "development"
    log_level: str = "INFO"
    base_url: str = "http://localhost:8000"

    # ─── Supabase ────────────────────────────────────────────────────────────
    supabase_url: str = "http://127.0.0.1:54321"
    supabase_anon_key: SecretStr = SecretStr("")

    # OJO: esta key bypassea RLS. Se lee unicamente desde
    # app/infra/supabase_admin.py, y scripts/check_service_role.py lo verifica.
    supabase_service_role_key: SecretStr = SecretStr("")

    # ─── Postgres ────────────────────────────────────────────────────────────
    # Rol app_runtime, SIN BYPASSRLS. Es lo que hace que RLS sea real.
    database_url: str = "postgresql+asyncpg://app_runtime:app_runtime_dev@127.0.0.1:54322/postgres"
    app_runtime_password: SecretStr = SecretStr("app_runtime_dev")

    # Owner del schema. Solo migraciones y tests; nunca el proceso servidor.
    migration_database_url: str | None = None

    db_pool_size: int = 5
    db_max_overflow: int = 5
    db_command_timeout_seconds: int = 30

    # ─── Sesion ──────────────────────────────────────────────────────────────
    session_secret: SecretStr = SecretStr("")
    session_cookie_name: str = "fc_session"
    session_max_age_seconds: int = 60 * 60 * 24 * 14

    # ─── LLM ─────────────────────────────────────────────────────────────────
    llm_provider: LLMProvider = "gemini"
    gemini_api_key: SecretStr = SecretStr("")
    gemini_model: str = "gemini-2.5-flash"
    mistral_api_key: SecretStr = SecretStr("")
    mistral_model: str = "mistral-small-latest"

    # Tope duro por usuario y por mes. Se chequea antes de encolar un job, para
    # que un bucle de reprocesamiento no pueda vaciar la cuenta.
    llm_monthly_budget_usd: Decimal = Decimal("5.00")
    llm_max_concurrency: int = 3

    # ─── Storage / upload ────────────────────────────────────────────────────
    storage_bucket: str = "statements"
    max_upload_bytes: int = 20 * 1024 * 1024
    max_pdf_pages: int = 40

    # ─── Jobs ────────────────────────────────────────────────────────────────
    job_poll_seconds: int = 5
    job_concurrency: int = 2
    job_stale_minutes: int = 10
    job_max_attempts: int = 3

    # ─── Validacion de extraccion ────────────────────────────────────────────
    # Tolerancia del cuadre de saldo: el mayor entre un porcentaje del total y un
    # piso absoluto por moneda. El piso absorbe redondeos de centavos; el
    # porcentaje, resumenes grandes.
    reconciliation_tolerance_pct: Decimal = Decimal("0.005")
    reconciliation_floor_ars: Decimal = Decimal("50.00")
    reconciliation_floor_usd: Decimal = Decimal("1.00")
    # Similitud minima de source_line contra el texto del PDF. Debajo de esto la
    # transaccion se marca como posible alucinacion del LLM.
    source_line_min_ratio: float = 0.85

    @field_validator("log_level")
    @classmethod
    def _upper_log_level(cls, value: str) -> str:
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        upper = value.upper()
        if upper not in allowed:
            raise ValueError(f"log_level debe ser uno de {sorted(allowed)}, no {value!r}")
        return upper

    @model_validator(mode="after")
    def _require_production_secrets(self) -> Settings:
        """En produccion, fallar al arrancar antes que servir algo inseguro."""
        if self.env != "production":
            return self

        missing = [
            name
            for name, value in (
                ("SESSION_SECRET", self.session_secret),
                ("SUPABASE_ANON_KEY", self.supabase_anon_key),
            )
            if not value.get_secret_value()
        ]
        if missing:
            raise ValueError(f"faltan secretos obligatorios en produccion: {', '.join(missing)}")

        if len(self.session_secret.get_secret_value()) < 32:
            raise ValueError(
                "SESSION_SECRET debe tener al menos 32 caracteres. "
                'Generar con: python -c "import secrets; print(secrets.token_urlsafe(48))"'
            )

        if self.llm_provider == "gemini" and not self.gemini_api_key.get_secret_value():
            raise ValueError("LLM_PROVIDER=gemini requiere GEMINI_API_KEY")

        # El proceso servidor no debe tener a mano credenciales de owner del schema.
        if self.migration_database_url:
            raise ValueError(
                "MIGRATION_DATABASE_URL no debe estar seteada en produccion: "
                "el proceso servidor se conecta solo como app_runtime"
            )
        return self

    @property
    def is_production(self) -> bool:
        return self.env == "production"

    @property
    def jwks_url(self) -> str:
        return f"{self.supabase_url.rstrip('/')}/auth/v1/.well-known/jwks.json"

    @property
    def jwt_issuer(self) -> str:
        return f"{self.supabase_url.rstrip('/')}/auth/v1"

    def reconciliation_floor(self, currency: str) -> Decimal:
        """Piso absoluto de tolerancia para el cuadre, por moneda."""
        return {
            "ARS": self.reconciliation_floor_ars,
            "USD": self.reconciliation_floor_usd,
        }.get(currency.upper(), self.reconciliation_floor_usd)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Settings cacheadas. Usar como dependencia de FastAPI o llamar directo."""
    return Settings()


# Los tests que cambian el entorno deben llamar a esto.
def reset_settings_cache() -> None:
    get_settings.cache_clear()
