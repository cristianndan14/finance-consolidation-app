"""La etapa 2 de punta a punta, contra un Postgres real.

Lo unico que se reemplaza es la llamada al modelo: `FakeExtractor` replaya
respuestas grabadas, asi que los tests son gratis y deterministicos. Todo lo
demas —el servicio, el chunking, la validacion, la reconciliacion, los
repositorios— es el codigo de produccion corriendo con RLS activo y conectado
como `app_runtime`.

La pregunta que responden estos tests es la de la fase: si reproceso un resumen
con un prompt nuevo, ¿se actualiza lo que el modelo escribio y sobrevive lo que
corregi a mano? Es lo unico que hace seguro iterar prompts sobre datos reales, y
no se puede verificar sin una base de verdad: el comportamiento depende del
`unique (user_id, statement_id, dedupe_key, occurrence_index)` y de RLS.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text

from app.domain.models import ParsedStatementHeader
from app.infra import db
from app.llm.fake import FakeExtractor
from app.repositories import document_texts as texts_repo
from app.repositories import documents as documents_repo
from app.repositories import extraction_runs as runs_repo
from app.repositories import llm_usage as usage_repo
from app.repositories import statements as statements_repo
from app.repositories import transactions as transactions_repo
from app.services import parse as parse_service
from app.settings import Settings
from tests.conftest import Actor, requires_db

pytestmark = [requires_db, pytest.mark.db]

CLOSING = "20/03/2026"


def _settings(**overrides: Any) -> Settings:
    return Settings(_env_file=None, llm_provider="fake", **overrides)


# ─────────────────────────────────────────────────────────────────────────────
# El resumen sintetico con el que se trabaja
# ─────────────────────────────────────────────────────────────────────────────
LINES = [
    ("15/03", "SUPERMERCADO COTO", "60000.00", 1, "charge"),
    ("16/03", "FARMACIA DEL SOL", "25000.00", 1, "charge"),
    ("17/03", "SU PAGO EN PESOS", "10000.00", -1, "payment"),
    ("18/03", "IVA RG 4815", "5000.00", 1, "tax"),
]

# 60000 + 25000 - 10000 + 5000 = 80000
TOTAL_ARS = Decimal("80000.00")


def _line_text(day: str, description: str, amount: str) -> str:
    return f"{day} {description}{'':>10}{amount}"


def _full_text() -> str:
    body = "\n".join(_line_text(day, desc, amount) for day, desc, amount, _d, _k in LINES)
    return f"--- pagina 1 ---\nRESUMEN DE CUENTA - CIERRE {CLOSING}\n{body}"


def _page_texts() -> list[dict[str, Any]]:
    body = "\n".join(_line_text(day, desc, amount) for day, desc, amount, _d, _k in LINES)
    page = f"RESUMEN DE CUENTA - CIERRE {CLOSING}\n{body}"
    return [{"page": 1, "text_layout": page, "text_flow": page, "tables": []}]


def _tx_payload(index: int, *, amount: str | None = None) -> dict[str, Any]:
    day, description, default_amount, direction, kind = LINES[index]
    return {
        "posted_date": day,
        "description_raw": description,
        "source_line": _line_text(day, description, amount or default_amount),
        "source_page": 1,
        "amount": amount or default_amount,
        "currency": "ARS",
        "direction": direction,
        "kind": kind,
    }


def _cassette(
    *,
    indexes: list[int] | None = None,
    new_charges: str = "80000.00",
    repair: list[int] | None = None,
    amounts: dict[int, str] | None = None,
) -> FakeExtractor:
    amounts = amounts or {}
    chosen = indexes if indexes is not None else list(range(len(LINES)))
    return FakeExtractor.from_dict(
        {
            "header": {
                "statements": [
                    {
                        "currency": "ARS",
                        "closing_date": CLOSING,
                        "new_charges": new_charges,
                        "total_due": new_charges,
                    }
                ]
            },
            "chunks": [{"transactions": [_tx_payload(i, amount=amounts.get(i)) for i in chosen]}],
            "repair": {"transactions": [_tx_payload(i) for i in (repair or [])]},
        }
    )


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────
@pytest_asyncio.fixture
async def user(
    app_runtime_engine: None, two_users: tuple[Actor, Actor]
) -> AsyncIterator[uuid.UUID]:
    yield two_users[0].id


@pytest_asyncio.fixture
async def document(user: uuid.UUID) -> AsyncIterator[documents_repo.Document]:
    """Un documento con texto ya extraido, como lo deja la etapa 1."""
    async with db.system_tx(str(user)) as conn:
        doc = await documents_repo.create(
            conn,
            storage_path=f"{user}/2026/{uuid.uuid4().hex}.pdf",
            original_filename="resumen.pdf",
            byte_size=1024,
            sha256=uuid.uuid4().hex + uuid.uuid4().hex,
            page_count=1,
            was_encrypted=False,
            card_id=None,
        )
        await texts_repo.upsert(
            conn,
            document_id=doc.id,
            extractor="pdfplumber",
            extractor_version="test",
            page_texts=_page_texts(),
            full_text=_full_text(),
            char_count=len(_full_text()),
            has_text_layer=True,
        )
        await documents_repo.set_status(conn, doc.id, "text_extracted")

    yield doc


async def _parse(
    user: uuid.UUID,
    document: documents_repo.Document,
    extractor: FakeExtractor,
    **kwargs: Any,
) -> parse_service.ParseOutcome:
    return await parse_service.parse_document(
        user_id=str(user),
        document_id=document.id,
        extractor=extractor,
        settings=_settings(),
        **kwargs,
    )


async def _transactions(user: uuid.UUID, statement_id: str) -> list[transactions_repo.Transaction]:
    async with db.system_tx(str(user)) as conn:
        return await transactions_repo.list_for_statement(conn, statement_id)


# ─────────────────────────────────────────────────────────────────────────────
# El camino feliz
# ─────────────────────────────────────────────────────────────────────────────
class TestCorridaCompleta:
    async def test_persiste_el_resumen_y_las_transacciones(
        self, user: uuid.UUID, document: documents_repo.Document
    ) -> None:
        outcome = await _parse(user, document, _cassette())

        assert outcome.report.balanced
        assert outcome.status == "draft"
        assert outcome.inserted == len(LINES)

        async with db.system_tx(str(user)) as conn:
            statements = await statements_repo.for_document(conn, document.id)

        assert len(statements) == 1
        statement = statements[0]
        assert statement.currency == "ARS"
        assert (statement.period_year, statement.period_month) == (2026, 3)
        assert statement.new_charges == TOTAL_ARS
        # 'confirmed' es siempre una accion explicita del usuario.
        assert statement.status == "draft"

        rows = await _transactions(user, statement.id)
        assert len(rows) == len(LINES)
        assert sum(row.signed_amount for row in rows) == TOTAL_ARS
        assert all(row.review_status == "pending" for row in rows)

    async def test_el_pago_queda_con_signo_negativo(
        self, user: uuid.UUID, document: documents_repo.Document
    ) -> None:
        outcome = await _parse(user, document, _cassette())

        rows = await _transactions(user, outcome.statement_ids[0])
        pago = next(row for row in rows if row.kind == "payment")

        assert pago.direction == -1
        assert pago.amount == Decimal("10000.00")
        assert pago.signed_amount == Decimal("-10000.00")

    async def test_el_año_se_infiere_de_la_fecha_de_cierre(
        self, user: uuid.UUID, document: documents_repo.Document
    ) -> None:
        outcome = await _parse(user, document, _cassette())

        rows = await _transactions(user, outcome.statement_ids[0])

        assert all(row.posted_date.year == 2026 for row in rows)

    async def test_el_documento_queda_parseado(
        self, user: uuid.UUID, document: documents_repo.Document
    ) -> None:
        await _parse(user, document, _cassette())

        async with db.system_tx(str(user)) as conn:
            refreshed = await documents_repo.get(conn, document.id)

        assert refreshed is not None
        assert refreshed.status == "parsed"

    async def test_queda_la_bitacora_de_la_corrida_con_la_validacion(
        self, user: uuid.UUID, document: documents_repo.Document
    ) -> None:
        await _parse(user, document, _cassette())

        async with db.system_tx(str(user)) as conn:
            runs = await runs_repo.latest_for_document(conn, document.id)

        assert len(runs) == 1
        assert runs[0].status == "succeeded"
        assert runs[0].validation is not None
        assert runs[0].validation["balanced"] is True

    async def test_el_gasto_queda_acumulado_para_el_tope_mensual(
        self, user: uuid.UUID, document: documents_repo.Document
    ) -> None:
        from datetime import UTC, datetime

        now = datetime.now(UTC)
        await _parse(user, document, _cassette())

        async with db.system_tx(str(user)) as conn:
            usage = await usage_repo.month_usage(conn, year=now.year, month=now.month)

        assert usage.calls == 1


# ─────────────────────────────────────────────────────────────────────────────
# Lo que no cuadra
# ─────────────────────────────────────────────────────────────────────────────
class TestCuadreFallido:
    async def test_una_extraccion_incompleta_se_guarda_igual_marcada(
        self, user: uuid.UUID, document: documents_repo.Document
    ) -> None:
        """Descartar lo que no cuadra haria desaparecer gastos en silencio."""
        outcome = await _parse(
            user, document, _cassette(indexes=[0, 1], repair=[]), allow_repair=False
        )

        assert not outcome.report.balanced
        assert outcome.status == "needs_review"
        assert outcome.inserted == 2

        async with db.system_tx(str(user)) as conn:
            statement = (await statements_repo.for_document(conn, document.id))[0]

        assert statement.status == "needs_review"
        assert len(await _transactions(user, statement.id)) == 2

    async def test_la_reparacion_completa_lo_que_faltaba(
        self, user: uuid.UUID, document: documents_repo.Document
    ) -> None:
        outcome = await _parse(user, document, _cassette(indexes=[0, 1], repair=[2, 3]))

        assert outcome.repaired
        assert outcome.report.balanced
        assert len(outcome.transactions) == len(LINES)

    async def test_una_reparacion_que_empeora_el_cuadre_se_descarta(
        self, user: uuid.UUID, document: documents_repo.Document
    ) -> None:
        """La reparacion es una apuesta; una apuesta perdida no puede dejar peor."""
        # Faltan 5.000 (se extrajeron 3 de las 4 lineas) y el modelo "encuentra"
        # una transaccion de 95.000 que no existe: pasarse es peor que faltar.
        extractor = FakeExtractor.from_dict(
            {
                "header": {
                    "statements": [
                        {"currency": "ARS", "closing_date": CLOSING, "new_charges": "80000.00"}
                    ]
                },
                "chunks": [{"transactions": [_tx_payload(i) for i in (0, 1, 2)]}],
                "repair": {
                    "transactions": [
                        {
                            "posted_date": "19/03",
                            "description_raw": "AJUSTE INVENTADO",
                            "source_line": "19/03 AJUSTE INVENTADO 95.000,00",
                            "amount": "95000.00",
                            "currency": "ARS",
                            "direction": 1,
                            "kind": "adjustment",
                        }
                    ]
                },
            }
        )

        outcome = await _parse(user, document, extractor)

        assert not outcome.repaired
        assert "repair_discarded" in {i.code for i in outcome.report.issues}
        assert len(outcome.transactions) == 3

    async def test_una_reparacion_que_no_aporta_nada_no_se_declara_aplicada(
        self, user: uuid.UUID, document: documents_repo.Document
    ) -> None:
        """Devolver lo que ya estaba es lo que hace el modelo cuando no encuentra."""
        outcome = await _parse(user, document, _cassette(indexes=[0, 1, 2], repair=[0]))

        assert not outcome.repaired
        assert len(outcome.transactions) == 3

    async def test_una_transaccion_inventada_baja_su_confianza(
        self, user: uuid.UUID, document: documents_repo.Document
    ) -> None:
        extractor = FakeExtractor.from_dict(
            {
                "header": {
                    "statements": [
                        {"currency": "ARS", "closing_date": CLOSING, "new_charges": "80000.00"}
                    ]
                },
                "chunks": [
                    {
                        "transactions": [
                            *[_tx_payload(i) for i in range(len(LINES))],
                            {
                                "posted_date": "19/03",
                                "description_raw": "CONSUMO QUE NADIE HIZO",
                                "source_line": "19/03 CONSUMO QUE NADIE HIZO 99.999,00",
                                "amount": "99999.00",
                                "currency": "ARS",
                                "direction": 1,
                                "kind": "charge",
                            },
                        ]
                    }
                ],
            }
        )

        outcome = await _parse(user, document, extractor, allow_repair=False)

        rows = await _transactions(user, outcome.statement_ids[0])
        inventada = next(row for row in rows if "NADIE" in row.description_raw)

        assert inventada.confidence == Decimal("0.20")


# ─────────────────────────────────────────────────────────────────────────────
# La reconciliacion: el corazon de la fase
# ─────────────────────────────────────────────────────────────────────────────
class TestReconciliacion:
    async def test_reprocesar_lo_mismo_no_duplica_nada(
        self, user: uuid.UUID, document: documents_repo.Document
    ) -> None:
        first = await _parse(user, document, _cassette())
        second = await _parse(user, document, _cassette())

        assert first.inserted == len(LINES)
        assert second.inserted == 0
        assert second.updated == len(LINES)

        rows = await _transactions(user, second.statement_ids[0])
        assert len(rows) == len(LINES)

    async def test_una_transaccion_confirmada_sobrevive_al_reproceso(
        self, user: uuid.UUID, document: documents_repo.Document
    ) -> None:
        outcome = await _parse(user, document, _cassette())
        statement_id = outcome.statement_ids[0]

        rows = await _transactions(user, statement_id)
        target = next(row for row in rows if row.description_raw == "SUPERMERCADO COTO")

        async with db.system_tx(str(user)) as conn:
            await conn.execute(
                text(
                    "update app.transactions set review_status = 'confirmed', "
                    "amount = 61000.00 where id = :id"
                ),
                {"id": target.id},
            )

        # El modelo ahora dice otra cosa sobre esa misma linea.
        again = await _parse(user, document, _cassette(amounts={0: "60000.00"}), allow_repair=False)

        rows = await _transactions(user, statement_id)
        survivor = next(row for row in rows if row.id == target.id)

        assert survivor.amount == Decimal("61000.00")
        assert survivor.review_status == "confirmed"
        assert again.report.conflicts
        assert again.report.conflicts[0]["review_status"] == "confirmed"

    async def test_lo_que_el_modelo_dejo_de_ver_y_estaba_pendiente_se_borra(
        self, user: uuid.UUID, document: documents_repo.Document
    ) -> None:
        outcome = await _parse(user, document, _cassette())
        statement_id = outcome.statement_ids[0]

        again = await _parse(user, document, _cassette(indexes=[0, 1]), allow_repair=False)

        assert again.deleted == 2
        rows = await _transactions(user, statement_id)
        assert len(rows) == 2

    async def test_lo_que_el_modelo_dejo_de_ver_pero_el_usuario_edito_se_conserva(
        self, user: uuid.UUID, document: documents_repo.Document
    ) -> None:
        outcome = await _parse(user, document, _cassette())
        statement_id = outcome.statement_ids[0]

        rows = await _transactions(user, statement_id)
        target = next(row for row in rows if row.description_raw == "IVA RG 4815")

        async with db.system_tx(str(user)) as conn:
            await conn.execute(
                text("update app.transactions set review_status = 'edited' where id = :id"),
                {"id": target.id},
            )

        again = await _parse(user, document, _cassette(indexes=[0, 1, 2]), allow_repair=False)

        rows = await _transactions(user, statement_id)
        assert target.id in {row.id for row in rows}
        assert again.report.orphans
        assert again.report.orphans[0]["review_status"] == "edited"

    async def test_un_resumen_confirmado_no_vuelve_a_borrador(
        self, user: uuid.UUID, document: documents_repo.Document
    ) -> None:
        outcome = await _parse(user, document, _cassette())
        statement_id = outcome.statement_ids[0]

        async with db.system_tx(str(user)) as conn:
            await statements_repo.set_status(conn, statement_id, "confirmed")

        await _parse(user, document, _cassette(indexes=[0]), allow_repair=False)

        async with db.system_tx(str(user)) as conn:
            statement = await statements_repo.get(conn, statement_id)

        assert statement is not None
        assert statement.status == "confirmed"

    async def test_las_corridas_anteriores_quedan_como_superseded(
        self, user: uuid.UUID, document: documents_repo.Document
    ) -> None:
        await _parse(user, document, _cassette())
        await _parse(user, document, _cassette())

        async with db.system_tx(str(user)) as conn:
            runs = await runs_repo.latest_for_document(conn, document.id)

        assert runs[0].status == "succeeded"
        assert runs[1].status == "superseded"


# ─────────────────────────────────────────────────────────────────────────────
# Dry run y errores
# ─────────────────────────────────────────────────────────────────────────────
class TestDryRun:
    async def test_no_escribe_nada(
        self, user: uuid.UUID, document: documents_repo.Document
    ) -> None:
        outcome = await _parse(user, document, _cassette(), dry_run=True)

        assert outcome.report.balanced
        assert len(outcome.transactions) == len(LINES)

        async with db.system_tx(str(user)) as conn:
            assert await statements_repo.for_document(conn, document.id) == []
            assert await runs_repo.latest_for_document(conn, document.id) == []


class TestErrores:
    async def test_un_documento_sin_texto_no_llama_al_modelo(self, user: uuid.UUID) -> None:
        async with db.system_tx(str(user)) as conn:
            doc = await documents_repo.create(
                conn,
                storage_path=f"{user}/2026/{uuid.uuid4().hex}.pdf",
                original_filename="sin_texto.pdf",
                byte_size=10,
                sha256=uuid.uuid4().hex + uuid.uuid4().hex,
                page_count=1,
                was_encrypted=False,
                card_id=None,
            )

        extractor = _cassette()
        with pytest.raises(parse_service.ParseError) as exc:
            await _parse(user, doc, extractor)

        assert exc.value.reason == "no_document_text"
        assert extractor.calls == []

    async def test_el_tope_de_gasto_corta_antes_de_llamar(
        self, user: uuid.UUID, document: documents_repo.Document
    ) -> None:
        from datetime import UTC, datetime

        from app.llm.ports import LLMBudgetExceededError

        now = datetime.now(UTC)
        async with db.system_tx(str(user)) as conn:
            await usage_repo.record(
                conn,
                year=now.year,
                month=now.month,
                provider="gemini",
                model="gemini-2.5-flash",
                input_tokens=1,
                output_tokens=1,
                cost_usd=Decimal("999.00"),
            )

        extractor = _cassette()
        with pytest.raises(LLMBudgetExceededError):
            await parse_service.parse_document(
                user_id=str(user),
                document_id=document.id,
                extractor=extractor,
                settings=_settings(llm_monthly_budget_usd=Decimal("5.00")),
            )

        assert extractor.calls == []

    async def test_un_fallo_del_modelo_deja_la_corrida_marcada(
        self, user: uuid.UUID, document: documents_repo.Document
    ) -> None:
        from app.llm.fake import FailingExtractor

        with pytest.raises(Exception, match="basura"):
            await parse_service.parse_document(
                user_id=str(user),
                document_id=document.id,
                extractor=FailingExtractor(),  # type: ignore[arg-type]
                settings=_settings(),
            )

        async with db.system_tx(str(user)) as conn:
            runs = await runs_repo.latest_for_document(conn, document.id)

        assert runs[0].status == "failed"
        assert runs[0].error is not None


class TestAislamiento:
    async def test_un_usuario_no_ve_las_transacciones_del_otro(
        self,
        app_runtime_engine: None,
        two_users: tuple[Actor, Actor],
        document: documents_repo.Document,
    ) -> None:
        """La garantia central del proyecto, tambien para la etapa 2."""
        owner, other = two_users
        outcome = await _parse(owner.id, document, _cassette())

        async with db.system_tx(str(other.id)) as conn:
            assert await statements_repo.for_document(conn, document.id) == []
            assert await transactions_repo.list_for_statement(conn, outcome.statement_ids[0]) == []


class TestHeaderSinCierre:
    async def test_sin_fecha_de_cierre_el_periodo_cae_en_el_mes_de_la_subida(
        self, user: uuid.UUID, document: documents_repo.Document
    ) -> None:
        """No se inventa un periodo: se usa un ancla explicable y se marca."""
        extractor = FakeExtractor.from_dict(
            {
                "header": {"statements": [{"currency": "ARS", "new_charges": "80000.00"}]},
                "chunks": [{"transactions": [_tx_payload(i) for i in range(len(LINES))]}],
            }
        )

        outcome = await _parse(user, document, extractor, allow_repair=False)

        assert outcome.headers[0].closing_date == document.uploaded_at.date()
        assert ParsedStatementHeader(currency="ARS").period is None


# ─────────────────────────────────────────────────────────────────────────────
# La ruta que dispara la etapa 2 sola
# ─────────────────────────────────────────────────────────────────────────────
class TestRutaReparse:
    """`POST /documents/{id}/reparse` contra la app real y la base real.

    Es la ruta que hace barata la iteracion de prompts desde la UI: reinterpreta
    el texto ya guardado sin volver a bajar el PDF. Se testea de punta a punta
    porque lo que importa es la combinacion —sesion, CSRF, RLS y la cola—, no
    cada pieza por separado.
    """

    @pytest_asyncio.fixture
    async def client(self, user: uuid.UUID, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Any]:
        import time

        import httpx
        import jwt as pyjwt

        from app.security import session as session_store
        from app.settings import get_settings, reset_settings_cache

        supabase_url = "https://project.supabase.test"
        jwt_secret = "secreto-compartido-del-supabase-local"

        monkeypatch.setenv("ENV", "test")
        monkeypatch.setenv("SESSION_SECRET", "un-secreto-de-mas-de-treinta-y-dos-caracteres")
        monkeypatch.setenv("SUPABASE_URL", supabase_url)
        monkeypatch.setenv("SUPABASE_ANON_KEY", "anon-key-de-prueba")
        monkeypatch.setenv("SUPABASE_JWT_SECRET", jwt_secret)
        reset_settings_cache()
        settings = get_settings()

        now = int(time.time())
        access_token = pyjwt.encode(
            {
                "sub": str(user),
                "aud": "authenticated",
                "role": "authenticated",
                "iss": f"{supabase_url}/auth/v1",
                "email": f"{user}@test.local",
                "session_id": str(uuid.uuid4()),
                "iat": now,
                "exp": now + 3600,
            },
            jwt_secret,
            algorithm="HS256",
        )
        cookie = session_store.encode(
            session_store.Session(
                user_id=str(user),
                email=f"{user}@test.local",
                access_token=access_token,
                refresh_token="refresh",
                access_expires_at=now + 3600,
            ),
            settings,
        )

        from app.main import app

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            cookies={settings.session_cookie_name: cookie},
        ) as http_client:
            http_client.headers["x-fc-csrf"] = ""
            yield http_client

        reset_settings_cache()

    def _csrf(self, user: uuid.UUID) -> str:
        from app.security import csrf
        from app.settings import get_settings

        return csrf.issue(str(user), get_settings())

    async def test_encola_la_etapa_2_sin_volver_a_tocar_el_pdf(
        self, client: Any, user: uuid.UUID, document: documents_repo.Document
    ) -> None:
        response = await client.post(
            f"/documents/{document.id}/reparse",
            data={"csrf_token": self._csrf(user)},
            follow_redirects=False,
        )

        assert response.status_code == 303

        async with db.system_tx(str(user)) as conn:
            rows = (
                await conn.execute(
                    text(
                        "select job_type, status from app.processing_jobs where document_id = :id"
                    ),
                    {"id": document.id},
                )
            ).all()

        assert [tuple(row) for row in rows] == [("parse_statement", "queued")]

    async def test_dos_clicks_no_son_dos_corridas_pagas(
        self, client: Any, user: uuid.UUID, document: documents_repo.Document
    ) -> None:
        for _ in range(2):
            await client.post(
                f"/documents/{document.id}/reparse",
                data={"csrf_token": self._csrf(user)},
                follow_redirects=False,
            )

        async with db.system_tx(str(user)) as conn:
            rows = (
                await conn.execute(
                    text("select count(*) from app.processing_jobs where document_id = :id"),
                    {"id": document.id},
                )
            ).scalar_one()

        assert rows == 1

    async def test_sin_texto_extraido_no_encola_un_job_que_fallaria_en_loop(
        self, client: Any, user: uuid.UUID
    ) -> None:
        async with db.system_tx(str(user)) as conn:
            doc = await documents_repo.create(
                conn,
                storage_path=f"{user}/2026/{uuid.uuid4().hex}.pdf",
                original_filename="sin_texto.pdf",
                byte_size=10,
                sha256=uuid.uuid4().hex + uuid.uuid4().hex,
                page_count=1,
                was_encrypted=False,
                card_id=None,
            )

        response = await client.post(
            f"/documents/{doc.id}/reparse",
            data={"csrf_token": self._csrf(user)},
            follow_redirects=False,
        )

        assert response.status_code == 409

    async def test_el_documento_de_otro_usuario_no_existe(
        self, client: Any, user: uuid.UUID, two_users: tuple[Actor, Actor]
    ) -> None:
        _owner, other = two_users
        async with db.system_tx(str(other.id)) as conn:
            ajeno = await documents_repo.create(
                conn,
                storage_path=f"{other.id}/2026/{uuid.uuid4().hex}.pdf",
                original_filename="ajeno.pdf",
                byte_size=10,
                sha256=uuid.uuid4().hex + uuid.uuid4().hex,
                page_count=1,
                was_encrypted=False,
                card_id=None,
            )

        response = await client.post(
            f"/documents/{ajeno.id}/reparse",
            data={"csrf_token": self._csrf(user)},
            follow_redirects=False,
        )

        assert response.status_code == 404
