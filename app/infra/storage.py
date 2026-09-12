"""Supabase Storage, siempre con el token del usuario.

# Por que no se usa la service_role key

El bucket `statements` tiene politicas por carpeta: `{user_id}/...` y
`(storage.foldername(name))[1] = auth.uid()`. Esas politicas solo se evaluan si
la request lleva el JWT del usuario. Con la service_role key, Storage responde
como administrador y el aislamiento entre usuarios pasa a depender de que el
codigo arme bien el path — exactamente el tipo de garantia que este proyecto no
quiere. Por eso todas las funciones de aca reciben `access_token`.

# Por que la subida pasa por el backend y no va directo del navegador

El backend necesita los bytes igual: para el `sha256` (idempotencia que no se
delega al cliente), para verificar los magic bytes y para descifrar los PDFs con
contraseña. Con archivos de 200 KB a 3 MB y cinco usuarios, el ancho de banda no
es un problema; una signed upload URL solo agregaria un round-trip y un camino
por el que podria entrar un archivo sin validar.

# Descargas

Con signed URLs de TTL corto. La URL firmada es una credencial: no se loguea
(esta en `REDACT_KEYS`) y no se le muestra al usuario mas que como link.
"""

from __future__ import annotations

from typing import Final

import httpx

from app.logging_config import get_logger
from app.settings import Settings, get_settings

log = get_logger(__name__)

UPLOAD_TIMEOUT_SECONDS: Final = 60.0
DEFAULT_TIMEOUT_SECONDS: Final = 15.0

# TTL de la URL que se le da al usuario para descargar. Corta a proposito: es
# suficiente para que el navegador empiece la descarga y no para compartirla.
DOWNLOAD_TTL_SECONDS: Final = 60

# TTL de la URL que consume el job de extraccion. Mas larga porque tiene que
# sobrevivir a la cola, a los reintentos con backoff y al reaper (10 minutos).
JOB_TTL_SECONDS: Final = 2 * 60 * 60


class StorageError(Exception):
    """Fallo al hablar con Storage. `message` es lo que ve el usuario."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


def _base(settings: Settings) -> str:
    return f"{settings.supabase_url.rstrip('/')}/storage/v1"


def _headers(access_token: str, settings: Settings) -> dict[str, str]:
    anon = settings.supabase_anon_key.get_secret_value()
    if not anon:
        raise StorageError("el storage no esta configurado")
    return {"apikey": anon, "Authorization": f"Bearer {access_token}"}


def object_path(user_id: str, year: int, sha256: str) -> str:
    """`{user_id}/{yyyy}/{sha256}.pdf`.

    El `user_id` como primer segmento no es cosmetico: es lo que hace expresable
    la politica de RLS del bucket. El `sha256` como nombre hace que subir dos
    veces el mismo archivo escriba el mismo objeto.
    """
    return f"{user_id}/{year}/{sha256}.pdf"


def _fail(response: httpx.Response, message: str) -> StorageError:
    log.error("storage respondio con error", status=response.status_code, body=response.text[:200])
    return StorageError(message, status=response.status_code)


async def upload_pdf(
    access_token: str,
    path: str,
    data: bytes,
    *,
    settings: Settings | None = None,
) -> None:
    """Sube (o sobreescribe) el PDF ya descifrado y saneado.

    `x-upsert` esta en `true` porque el nombre del objeto es el hash del
    contenido: reescribirlo escribe exactamente lo mismo. Sin upsert, un reintento
    despues de un fallo parcial quedaria trabado con un 409 para siempre.
    """
    cfg = settings or get_settings()
    headers = _headers(access_token, cfg)
    headers.update({"Content-Type": "application/pdf", "x-upsert": "true"})

    try:
        async with httpx.AsyncClient(timeout=UPLOAD_TIMEOUT_SECONDS) as client:
            response = await client.post(
                f"{_base(cfg)}/object/{cfg.storage_bucket}/{path}", headers=headers, content=data
            )
    except httpx.HTTPError as exc:
        raise StorageError("no se pudo guardar el archivo") from exc

    if response.status_code >= 400:
        raise _fail(response, "no se pudo guardar el archivo")

    log.info("pdf guardado en storage", bytes=len(data))


async def create_signed_url(
    access_token: str,
    path: str,
    *,
    expires_in: int = DOWNLOAD_TTL_SECONDS,
    settings: Settings | None = None,
) -> str:
    """URL temporal de descarga. La firma la hace Storage con el token del usuario."""
    cfg = settings or get_settings()

    try:
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_SECONDS) as client:
            response = await client.post(
                f"{_base(cfg)}/object/sign/{cfg.storage_bucket}/{path}",
                headers=_headers(access_token, cfg),
                json={"expiresIn": expires_in},
            )
    except httpx.HTTPError as exc:
        raise StorageError("no se pudo generar el link de descarga") from exc

    if response.status_code >= 400:
        raise _fail(response, "no se pudo generar el link de descarga")

    signed = response.json().get("signedURL") or response.json().get("signedUrl")
    if not signed:
        raise StorageError("respuesta inesperada de storage")
    return f"{_base(cfg)}{signed}"


async def download(url: str) -> bytes:
    """Baja el contenido de una URL firmada.

    No lleva credenciales: la firma va en la propia URL. Es lo que le permite al
    worker leer el PDF sin tener el JWT del usuario ni la service_role key.
    """
    try:
        async with httpx.AsyncClient(timeout=UPLOAD_TIMEOUT_SECONDS, follow_redirects=True) as c:
            response = await c.get(url)
    except httpx.HTTPError as exc:
        raise StorageError("no se pudo leer el archivo guardado") from exc

    if response.status_code >= 400:
        raise _fail(response, "no se pudo leer el archivo guardado")
    return response.content


async def remove(access_token: str, path: str, *, settings: Settings | None = None) -> None:
    """Borra el objeto. Se usa para deshacer una subida cuyo insert fallo."""
    cfg = settings or get_settings()
    try:
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_SECONDS) as client:
            response = await client.delete(
                f"{_base(cfg)}/object/{cfg.storage_bucket}/{path}",
                headers=_headers(access_token, cfg),
            )
    except httpx.HTTPError as exc:
        # Un huerfano en Storage es basura, no un problema de correctitud: se
        # registra y no se propaga, porque quien llama esta manejando otro error.
        log.warning("no se pudo borrar el objeto", error=type(exc).__name__)
        return

    if response.status_code >= 400:
        log.warning("storage rechazo el borrado", status=response.status_code)
