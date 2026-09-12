"""La etapa 1 de punta a punta, contra un Postgres real.

Lo unico que se reemplaza es Supabase Storage (no hay un Storage local en CI):
`upload_pdf` guarda en un dict, `create_signed_url` devuelve una clave de ese
dict y `download` la lee. Todo lo demas es el codigo de produccion — el servicio
de ingesta, la cola, las funciones `security definer` de la migracion, el runner
y el handler — corriendo con RLS activo y conectado como `app_runtime`.

Es el test que responde la pregunta de la fase: subo un PDF, ¿queda el texto
extraido en la base y el documento en un estado que la UI pueda mostrar?
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator

import asyncpg
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.infra import db, storage
from app.jobs.runner import JobRunner
from app.repositories import document_texts as texts_repo
from app.repositories import documents as documents_repo
from app.repositories import jobs as jobs_repo
from app.services import ingest
from app.settings import Settings
from tests.conftest import Actor, requires_db
from tests.fixtures import pdfs

pytestmark = [requires_db, pytest.mark.db]

PASSWORD = "12345678"


def _settings() -> Settings:
    return Settings(_env_file=None, supabase_url="https://proyecto.test", job_stale_minutes=10)


def _claims(user_id: uuid.UUID | str) -> dict[str, str]:
    return {"sub": str(user_id), "role": "authenticated"}


class FakeStorage:
    """Storage en memoria, con la misma interfaz que el modulo real."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.removed: list[str] = []

    async def upload_pdf(self, token: str, path: str, data: bytes, *, settings: object) -> None:
        self.objects[path] = data

    async def create_signed_url(
        self, token: str, path: str, *, expires_in: int = 60, settings: object = None
    ) -> str:
        if path not in self.objects:
            raise storage.StorageError("no existe el objeto")
        return f"https://storage.test/signed/{path}"

    async def download(self, url: str) -> bytes:
        path = url.removeprefix("https://storage.test/signed/")
        if path not in self.objects:
            raise storage.StorageError("no existe el objeto")
        return self.objects[path]

    async def remove(self, token: str, path: str, *, settings: object = None) -> None:
        self.removed.append(path)
        self.objects.pop(path, None)


@pytest.fixture
def fake_storage(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeStorage]:
    fake = FakeStorage()
    for module in (storage, ingest.storage):
        monkeypatch.setattr(module, "upload_pdf", fake.upload_pdf)
        monkeypatch.setattr(module, "create_signed_url", fake.create_signed_url)
        monkeypatch.setattr(module, "download", fake.download)
        monkeypatch.setattr(module, "remove", fake.remove)
    yield fake


@pytest_asyncio.fixture
async def user(
    app_runtime_engine: None, two_users: tuple[Actor, Actor]
) -> AsyncIterator[uuid.UUID]:
    yield two_users[0].id


async def _ingest(user_id: uuid.UUID, data: bytes, *, password: str | None = None) -> object:
    return await ingest.ingest_pdf(
        claims=_claims(user_id),
        user_id=str(user_id),
        access_token="token-de-prueba",
        filename="resumen-enero.pdf",
        data=data,
        password=password,
        card_id=None,
        settings=_settings(),
    )


class TestIngesta:
    async def test_subir_deja_documento_y_job(
        self, user: uuid.UUID, fake_storage: FakeStorage
    ) -> None:
        result = await _ingest(user, pdfs.text_pdf(pages=2))

        assert result.already_imported is False
        assert result.document.status == "uploaded"
        assert result.document.page_count == 2
        assert result.job_id is not None
        # El path empieza con el user_id: es lo que hace expresable la politica
        # del bucket.
        assert result.document.storage_path.startswith(f"{user}/")
        assert result.document.storage_path in fake_storage.objects

    async def test_el_mismo_archivo_dos_veces_no_se_duplica(
        self, user: uuid.UUID, fake_storage: FakeStorage
    ) -> None:
        """Idempotencia por sha256: es lo que evita pagar dos veces la extraccion."""
        data = pdfs.text_pdf(pages=1)
        first = await _ingest(user, data)
        second = await _ingest(user, data)

        assert second.already_imported is True
        assert second.document.id == first.document.id
        assert second.job_id is None

    async def test_un_pdf_con_contraseña_se_guarda_descifrado(
        self, user: uuid.UUID, fake_storage: FakeStorage
    ) -> None:
        result = await _ingest(user, pdfs.encrypted_pdf(PASSWORD), password=PASSWORD)

        assert result.document.was_encrypted is True
        # Lo guardado abre sin contraseña: el job de extraccion no la tiene.
        guardado = fake_storage.objects[result.document.storage_path]
        assert PASSWORD.encode() not in guardado

    async def test_sin_contraseña_no_se_sube_nada(
        self, user: uuid.UUID, fake_storage: FakeStorage
    ) -> None:
        """El rechazo pasa antes de tocar Storage: no queda basura de un intento."""
        from app.pdf import inspect as pdf_inspect

        with pytest.raises(pdf_inspect.PasswordRequiredError):
            await _ingest(user, pdfs.encrypted_pdf(PASSWORD))

        assert fake_storage.objects == {}

    async def test_un_documento_no_lo_ve_el_otro_usuario(
        self, user: uuid.UUID, two_users: tuple[Actor, Actor], fake_storage: FakeStorage
    ) -> None:
        """El aislamiento tambien en el camino nuevo, no solo en el test generico."""
        result = await _ingest(user, pdfs.text_pdf(pages=1))
        _, otro = two_users

        async with db.user_tx(_claims(otro.id)) as conn:
            assert await documents_repo.get(conn, result.document.id) is None
            assert await documents_repo.list_recent(conn) == []


class TestPipelineCompleto:
    async def test_el_job_extrae_el_texto(self, user: uuid.UUID, fake_storage: FakeStorage) -> None:
        result = await _ingest(user, pdfs.text_pdf(pages=2))

        assert await JobRunner(_settings()).run_once() is True

        async with db.user_tx(_claims(user)) as conn:
            document = await documents_repo.get(conn, result.document.id)
            extracted = await texts_repo.latest_for_document(conn, result.document.id)
            rows = (
                await conn.execute(
                    text(
                        "select job_type, status from app.processing_jobs "
                        "where document_id = :id order by created_at"
                    ),
                    {"id": result.document.id},
                )
            ).all()

        assert document is not None and document.status == "text_extracted"
        assert extracted is not None
        assert extracted.has_text_layer is True
        assert extracted.page_count == 2
        assert "SUPERMERCADO COTO" in extracted.full_text

        # La etapa 1 encola la etapa 2 en vez de hacerla: asi, mejorar un prompt
        # se reprocesa sin volver a bajar ni releer el PDF, y si el LLM esta
        # caido el texto —lo caro de obtener— ya quedo guardado.
        assert [tuple(row) for row in rows] == [
            ("extract_text", "succeeded"),
            ("parse_statement", "queued"),
        ]

    async def test_un_escaneo_falla_sin_reintentar(
        self, user: uuid.UUID, fake_storage: FakeStorage
    ) -> None:
        """Reintentar un PDF sin capa de texto da tres veces el mismo resultado."""
        result = await _ingest(user, pdfs.scanned_pdf(pages=3))

        await JobRunner(_settings()).run_once()

        async with db.user_tx(_claims(user)) as conn:
            document = await documents_repo.get(conn, result.document.id)
            job = await jobs_repo.latest_for_document(conn, result.document.id)
            extracted = await texts_repo.latest_for_document(conn, result.document.id)

        assert document is not None
        assert document.status == "failed"
        assert document.failure_reason == "no_text_layer"
        assert job is not None and job.status == "failed"
        # El texto se guarda igual: sirve para diagnosticar sin rebajar el PDF.
        assert extracted is not None and extracted.has_text_layer is False

    async def test_un_fallo_transitorio_vuelve_a_la_cola(
        self, user: uuid.UUID, fake_storage: FakeStorage
    ) -> None:
        """Storage caido no es motivo para dar el documento por perdido."""
        result = await _ingest(user, pdfs.text_pdf(pages=1))
        fake_storage.objects.clear()  # como si Storage no respondiera

        await JobRunner(_settings()).run_once()

        async with db.user_tx(_claims(user)) as conn:
            document = await documents_repo.get(conn, result.document.id)
            job = await jobs_repo.latest_for_document(conn, result.document.id)

        assert job is not None and job.status == "queued", "tiene que reintentarse"
        assert job.attempts == 1
        # El documento sigue "procesando": marcarlo fallido y desmarcarlo un
        # minuto despues seria peor que no decir nada.
        assert document is not None and document.status == "uploaded"


class TestCola:
    async def test_no_se_encola_dos_veces_el_mismo_trabajo(
        self, user: uuid.UUID, fake_storage: FakeStorage
    ) -> None:
        """Dos clicks en "reprocesar" no producen dos corridas."""
        result = await _ingest(user, pdfs.text_pdf(pages=1))

        async with db.user_tx(_claims(user)) as conn:
            repetido = await jobs_repo.enqueue(
                conn, document_id=result.document.id, job_type=jobs_repo.EXTRACT_TEXT
            )

        assert repetido is None

    async def test_dos_runners_no_se_llevan_el_mismo_job(
        self, user: uuid.UUID, fake_storage: FakeStorage
    ) -> None:
        """`for update skip locked`: es lo que hace trivial escalar a N workers."""
        await _ingest(user, pdfs.text_pdf(pages=1))

        async with db.runtime_tx() as conn:
            primero = await jobs_repo.claim_next(conn, "worker-a")
        async with db.runtime_tx() as conn:
            segundo = await jobs_repo.claim_next(conn, "worker-b")

        assert primero is not None
        assert segundo is None

    async def test_el_reaper_devuelve_los_huerfanos(
        self, user: uuid.UUID, fake_storage: FakeStorage, owner_conn: asyncpg.Connection
    ) -> None:
        """Un redeploy mata los jobs en curso; sin esto quedan colgados para siempre."""
        result = await _ingest(user, pdfs.text_pdf(pages=1))

        async with db.runtime_tx() as conn:
            claimed = await jobs_repo.claim_next(conn, "worker-que-se-murio")
        assert claimed is not None

        await owner_conn.execute(
            "update app.processing_jobs set locked_at = now() - interval '30 minutes' "
            "where id = $1",
            uuid.UUID(claimed.id),
        )

        async with db.runtime_tx() as conn:
            reaped = await jobs_repo.reap_stale(conn, 10)

        assert reaped == 1
        async with db.user_tx(_claims(user)) as conn:
            job = await jobs_repo.latest_for_document(conn, result.document.id)
        assert job is not None and job.status == "queued"

    async def test_un_usuario_no_puede_tomar_jobs_ajenos(
        self, user: uuid.UUID, fake_storage: FakeStorage
    ) -> None:
        """`claim_next_job` ve la cola entera: por eso `authenticated` no la ejecuta.

        Si pudiera, un usuario cualquiera podria ir sacando jobs de otros y
        leerles el payload, que incluye la URL firmada del PDF.
        """
        await _ingest(user, pdfs.text_pdf(pages=1))

        with pytest.raises(DBAPIError) as caught:
            async with db.user_tx(_claims(user)) as conn:
                await jobs_repo.claim_next(conn, "usuario-curioso")

        assert "permission denied" in str(caught.value).lower()


class TestIdentidadResidual:
    """El detalle que hace que el runner pueda convivir con las requests.

    `set_config(..., is_local => true)` no borra el parametro al hacer COMMIT: lo
    deja en cadena **vacia**. Una conexion del pool que ya sirvio una request
    vuelve con `request.jwt.claims = ''`, y si `auth.uid()` casteara eso a json
    antes de anularlo, todo query sin identidad explotaria en lugar de no ver
    nada. Es un fallo que no aparece con un solo usuario ni con una sola request.
    """

    async def test_una_conexion_reusada_no_hereda_identidad_ni_rompe(self, user: uuid.UUID) -> None:
        async with db.user_tx(_claims(user)) as conn:
            assert (await conn.execute(text("select auth.uid()"))).scalar() == user

        async with db.runtime_tx() as conn:
            # Sin identidad: NULL, no una excepcion. El modo de falla es "no ves
            # nada", que es el que hace segura la ausencia de contexto.
            assert (await conn.execute(text("select auth.uid()"))).scalar() is None
