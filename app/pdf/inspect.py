"""Validacion, desencriptado y saneado del PDF subido.

Todo lo que entra al sistema pasa por aca antes de tocar Storage. Tres razones,
en orden de importancia:

1. **Contraseñas.** Galicia, Santander y BBVA mandan el resumen cifrado con el
   DNI o el CUIT del titular. No es un caso raro: es el caso normal para la mitad
   de los emisores. Se descifra al subir y se guarda descifrado, porque la etapa
   2 corre despues, en un job sin la contraseña a mano. La contraseña **no se
   persiste nunca** — ni en la base, ni en el log, ni en el payload del job.

2. **Un PDF es un formato ejecutable.** Puede traer JavaScript, acciones al
   abrir y archivos adjuntos. Nada de eso tiene sentido en un resumen de tarjeta,
   y el archivo despues se descarga al navegador del usuario. Se sacan antes de
   guardar: es barato y elimina la categoria entera.

3. **Limites.** Tamaño y cantidad de paginas se validan antes de gastar CPU en
   extraer texto de algo que no es un resumen.

El modulo es sincrono a proposito (pikepdf es CPU-bound y libera el GIL a medias);
quien lo llama desde una ruta async lo corre con `asyncio.to_thread`.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from typing import Final

import pikepdf

from app.logging_config import get_logger

log = get_logger(__name__)

PDF_MAGIC: Final = b"%PDF-"

# Claves del catalogo y de cada pagina que pueden ejecutar algo al abrir el
# archivo. Se borran siempre, aunque vengan vacias.
ACTIVE_CONTENT_KEYS: Final = ("/OpenAction", "/AA", "/AcroForm")
NAME_TREE_KEYS: Final = ("/JavaScript", "/EmbeddedFiles")


class PdfError(Exception):
    """Base de los rechazos de esta etapa. `message` se le muestra al usuario."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class NotAPdfError(PdfError):
    """Los bytes no empiezan con `%PDF-`, o pikepdf no puede abrirlos."""


class PasswordRequiredError(PdfError):
    """El PDF esta cifrado y no vino contraseña."""


class WrongPasswordError(PdfError):
    """Vino contraseña y no abre con ella."""


class TooManyPagesError(PdfError):
    """Mas paginas que el maximo configurado."""


@dataclass(frozen=True)
class NormalizedPdf:
    """El PDF listo para guardar: descifrado, saneado y contado."""

    data: bytes
    page_count: int
    was_encrypted: bool


def _strip_active_content(pdf: pikepdf.Pdf) -> None:
    """Saca JavaScript, acciones automaticas y adjuntos.

    Un resumen de tarjeta es una tabla de numeros. Si trae contenido activo, o
    esta mal generado o no es lo que dice ser; en cualquiera de los dos casos no
    se gana nada conservandolo.
    """
    root = pdf.Root

    for key in ACTIVE_CONTENT_KEYS:
        if key in root:
            del root[key]

    names = root.get("/Names")
    if names is not None:
        for key in NAME_TREE_KEYS:
            if key in names:
                del names[key]

    for page in pdf.pages:
        for key in ("/AA", "/JS"):
            if key in page:
                del page[key]


def _open(data: bytes, password: str | None) -> tuple[pikepdf.Pdf, bool]:
    """Abre el PDF, descifrandolo si hace falta. Devuelve si venia cifrado."""
    try:
        return pikepdf.open(io.BytesIO(data)), False
    except pikepdf.PasswordError:
        pass
    except pikepdf.PdfError as exc:
        raise NotAPdfError("el archivo no es un PDF valido o esta dañado") from exc

    if not password:
        raise PasswordRequiredError("el PDF esta protegido con contraseña")

    try:
        return pikepdf.open(io.BytesIO(data), password=password), True
    except pikepdf.PasswordError as exc:
        raise WrongPasswordError("la contraseña no abre el PDF") from exc
    except pikepdf.PdfError as exc:
        raise NotAPdfError("el archivo no es un PDF valido o esta dañado") from exc


def normalize(data: bytes, *, password: str | None = None, max_pages: int) -> NormalizedPdf:
    """Valida, descifra y sanea. Levanta `PdfError` si el archivo no sirve.

    Devuelve **bytes nuevos**: lo que se guarda en Storage es la version
    descifrada y saneada, no lo que subio el usuario. Guardar el original
    obligaria a pedir la contraseña de nuevo en cada reproceso.
    """
    if not data.startswith(PDF_MAGIC):
        # El content-type del multipart lo elige el cliente: no es evidencia.
        raise NotAPdfError("el archivo no es un PDF")

    pdf, was_encrypted = _open(data, password)

    with pdf:
        page_count = len(pdf.pages)
        if page_count == 0:
            raise NotAPdfError("el PDF no tiene paginas")
        if page_count > max_pages:
            raise TooManyPagesError(f"el PDF tiene {page_count} paginas y el maximo es {max_pages}")

        _strip_active_content(pdf)

        buffer = io.BytesIO()
        pdf.save(buffer)

    log.info("pdf normalizado", pages=page_count, was_encrypted=was_encrypted, bytes=buffer.tell())
    return NormalizedPdf(data=buffer.getvalue(), page_count=page_count, was_encrypted=was_encrypted)
