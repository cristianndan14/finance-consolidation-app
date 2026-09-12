"""La pantalla de revision, contra un Postgres real y la app real.

Es la fase que convierte el sistema en confiable, asi que lo que se verifica no
es que los endpoints respondan: es que las reglas que sostienen esa confianza no
se puedan saltear.

Las tres que importan y por que se testean de punta a punta:

1. **Confirmar exige haber mirado todo.** Si quedaran pendientes, confirmar seria
   una afirmacion falsa sobre datos que despues alimentan el dashboard.
2. **Editar conserva la `dedupe_key`.** La clave identifica la linea del modelo
   de la que salio la fila, no su contenido actual. Si se recalculara, el proximo
   reparse no reconoceria la fila corregida: insertaria de nuevo la version mal
   leida y dejaria la corregida como huerfana. Verificarlo exige correr una
   correccion y despues un reparse de verdad contra la base.
3. **Dividir conserva el total.** Un split que cambia la suma mueve el cuadre sin
   que nadie lo haya pedido.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any

import httpx
import jwt as pyjwt
import pytest
import pytest_asyncio
from sqlalchemy import text

from app.infra import db
from app.llm.fake import FakeExtractor
from app.repositories import document_texts as texts_repo
from app.repositories import documents as documents_repo
from app.repositories import statements as statements_repo
from app.repositories import transaction_revisions as revisions_repo
from app.repositories import transactions as transactions_repo
from app.services import parse as parse_service
from app.services import review as review_service
from app.settings import Settings, get_settings, reset_settings_cache
from tests.conftest import Actor, requires_db

pytestmark = [requires_db, pytest.mark.db]

CLOSING = "20/03/2026"
SUPABASE_URL = "https://project.supabase.test"
JWT_SECRET = "secreto-compartido-del-supabase-local"

LINES = [
    ("15/03", "SUPERMERCADO COTO", "60000.00", 1, "charge"),
    ("16/03", "FARMACIA DEL SOL", "25000.00", 1, "charge"),
    ("17/03", "SU PAGO EN PESOS", "10000.00", -1, "payment"),
    ("18/03", "IVA RG 4815", "5000.00", 1, "tax"),
]
TOTAL_ARS = Decimal("80000.00")


def _settings(**overrides: Any) -> Settings:
    return Settings(_env_file=None, llm_provider="fake", **overrides)


def _line_text(day: str, description: str, amount: str) -> str:
    return f"{day} {description}{'':>10}{amount}"


def _full_text() -> str:
    body = "\n".join(_line_text(d, desc, a) for d, desc, a, _dir, _k in LINES)
    return f"--- pagina 1 ---\nRESUMEN DE CUENTA - CIERRE {CLOSING}\n{body}"


def _tx_payload(index: int) -> dict[str, Any]:
    day, description, amount, direction, kind = LINES[index]
    return {
        "posted_date": day,
        "description_raw": description,
        "source_line": _line_text(day, description, amount),
        "source_page": 1,
        "amount": amount,
        "currency": "ARS",
        "direction": direction,
        "kind": kind,
    }


def _cassette(*, indexes: list[int] | None = None, new_charges: str = "80000.00") -> FakeExtractor:
    chosen = indexes if indexes is not None else list(range(len(LINES)))
    return FakeExtractor.from_dict(
        {
            "header": {
                "statements": [
                    {"currency": "ARS", "closing_date": CLOSING, "new_charges": new_charges}
                ]
            },
            "chunks": [{"transactions": [_tx_payload(i) for i in chosen]}],
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


async def _make_document(user: uuid.UUID) -> documents_repo.Document:
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
        page = _full_text()
        await texts_repo.upsert(
            conn,
            document_id=doc.id,
            extractor="pdfplumber",
            extractor_version="test",
            page_texts=[{"page": 1, "text_layout": page, "text_flow": page, "tables": []}],
            full_text=page,
            char_count=len(page),
            has_text_layer=True,
        )
    return doc


@pytest_asyncio.fixture
async def statement(user: uuid.UUID) -> AsyncIterator[statements_repo.Statement]:
    """Un resumen ya parseado, con sus cuatro transacciones en `pending`."""
    document = await _make_document(user)
    outcome = await parse_service.parse_document(
        user_id=str(user),
        document_id=document.id,
        extractor=_cassette(),
        settings=_settings(),
    )
    async with db.system_tx(str(user)) as conn:
        row = await statements_repo.get(conn, outcome.statement_ids[0])
    assert row is not None
    yield row


async def _state(user: uuid.UUID, statement_id: str) -> review_service.ReviewState:
    async with db.system_tx(str(user)) as conn:
        state = await review_service.load(conn, statement_id, _settings())
    assert state is not None
    return state


async def _rows(user: uuid.UUID, statement_id: str) -> list[transactions_repo.Transaction]:
    async with db.system_tx(str(user)) as conn:
        return await transactions_repo.list_for_statement(conn, statement_id)


async def _find(
    user: uuid.UUID, statement_id: str, description: str
) -> transactions_repo.Transaction:
    rows = await _rows(user, statement_id)
    return next(row for row in rows if row.description_raw == description)


# ─────────────────────────────────────────────────────────────────────────────
# El estado que ve la pantalla
# ─────────────────────────────────────────────────────────────────────────────
class TestEstado:
    async def test_el_cuadre_se_calcula_leyendo_la_base(
        self, user: uuid.UUID, statement: statements_repo.Statement
    ) -> None:
        state = await _state(user, statement.id)

        assert state.balanced
        assert state.balances[0].computed == TOTAL_ARS
        assert state.balances[0].declared == TOTAL_ARS
        assert state.balances[0].delta == Decimal("0.00")

    async def test_arranca_con_todo_pendiente_y_no_se_puede_confirmar(
        self, user: uuid.UUID, statement: statements_repo.Statement
    ) -> None:
        state = await _state(user, statement.id)

        assert state.pending == len(LINES)
        assert not state.can_confirm
        assert "sin revisar" in (state.blocking_reason or "")

    async def test_lo_descartado_sale_del_total(
        self, user: uuid.UUID, statement: statements_repo.Statement
    ) -> None:
        """Si sumara, un resumen con una línea mal leída no cuadraría nunca."""
        target = await _find(user, statement.id, "IVA RG 4815")

        async with db.system_tx(str(user)) as conn:
            await review_service.bulk_review(
                conn, statement.id, transaction_ids=[target.id], action="reject"
            )

        state = await _state(user, statement.id)
        assert state.balances[0].computed == TOTAL_ARS - Decimal("5000.00")
        assert state.balances[0].rejected == 1


# ─────────────────────────────────────────────────────────────────────────────
# Editar
# ─────────────────────────────────────────────────────────────────────────────
class TestEdicion:
    async def test_corregir_un_monto_lo_deja_editado_y_actualiza_el_cuadre(
        self, user: uuid.UUID, statement: statements_repo.Statement
    ) -> None:
        target = await _find(user, statement.id, "SUPERMERCADO COTO")

        async with db.system_tx(str(user)) as conn:
            updated = await review_service.edit_transaction(
                conn, target.id, {"amount": "61.000,50"}
            )

        assert updated.amount == Decimal("61000.50")
        assert updated.review_status == "edited"
        assert updated.confidence == Decimal("1.00")

        state = await _state(user, statement.id)
        assert state.balances[0].computed == TOTAL_ARS + Decimal("1000.50")
        assert not state.balanced

    async def test_acepta_el_formato_es_ar_que_el_usuario_escribe(
        self, user: uuid.UUID, statement: statements_repo.Statement
    ) -> None:
        target = await _find(user, statement.id, "FARMACIA DEL SOL")

        async with db.system_tx(str(user)) as conn:
            updated = await review_service.edit_transaction(
                conn, target.id, {"amount": "1.234,56", "posted_date": "16/03/2026"}
            )

        assert updated.amount == Decimal("1234.56")

    async def test_el_cambio_queda_en_la_bitacora_con_el_valor_anterior(
        self, user: uuid.UUID, statement: statements_repo.Statement
    ) -> None:
        target = await _find(user, statement.id, "SUPERMERCADO COTO")

        async with db.system_tx(str(user)) as conn:
            await review_service.edit_transaction(
                conn, target.id, {"amount": "61000.00", "kind": "fee"}
            )
            revisions = await revisions_repo.for_transaction(conn, target.id)

        campos = {rev.field: (rev.old_value, rev.new_value) for rev in revisions}
        assert campos["amount"] == ("60000.00", "61000.00")
        assert campos["kind"] == ("charge", "fee")

    async def test_reenviar_la_fila_sin_cambios_no_genera_revisiones(
        self, user: uuid.UUID, statement: statements_repo.Statement
    ) -> None:
        target = await _find(user, statement.id, "SUPERMERCADO COTO")

        async with db.system_tx(str(user)) as conn:
            await review_service.edit_transaction(
                conn,
                target.id,
                {
                    "amount": str(target.amount),
                    "description_raw": target.description_raw,
                    "kind": target.kind,
                },
            )
            revisions = await revisions_repo.for_transaction(conn, target.id)

        assert revisions == []

    async def test_un_monto_negativo_se_rechaza_con_una_explicacion_util(
        self, user: uuid.UUID, statement: statements_repo.Statement
    ) -> None:
        target = await _find(user, statement.id, "SUPERMERCADO COTO")

        async with db.system_tx(str(user)) as conn:
            with pytest.raises(review_service.ReviewError, match="crédito"):
                await review_service.edit_transaction(conn, target.id, {"amount": "-500.00"})

    @pytest.mark.parametrize(
        ("field", "value", "match"),
        [
            ("posted_date", "el martes", "fecha"),
            ("description_raw", "   ", "descripción"),
            ("amount", "mucha plata", "monto"),
            ("kind", "inventado", "desconocido"),
            ("direction", "0", "sentido"),
        ],
    )
    async def test_los_valores_invalidos_se_rechazan(
        self,
        user: uuid.UUID,
        statement: statements_repo.Statement,
        field: str,
        value: str,
        match: str,
    ) -> None:
        target = await _find(user, statement.id, "SUPERMERCADO COTO")

        async with db.system_tx(str(user)) as conn:
            with pytest.raises(review_service.ReviewError, match=match):
                await review_service.edit_transaction(conn, target.id, {field: value})

    async def test_una_cuota_a_medias_se_rechaza(
        self, user: uuid.UUID, statement: statements_repo.Statement
    ) -> None:
        """La base exige número y total juntos, o ninguno."""
        target = await _find(user, statement.id, "SUPERMERCADO COTO")

        async with db.system_tx(str(user)) as conn:
            with pytest.raises(review_service.ReviewError, match="número y el total"):
                await review_service.edit_transaction(conn, target.id, {"installment_number": "3"})

    async def test_la_clave_de_dedupe_no_cambia_al_corregir(
        self, user: uuid.UUID, statement: statements_repo.Statement
    ) -> None:
        """La clave identifica la línea del modelo, no el contenido actual.

        Es lo que permite que el próximo reparse reconozca la fila corregida en
        vez de insertar de nuevo la versión mal leída. Y de paso hace imposible
        que corregir una transacción para que quede igual a otra choque contra
        el unique.
        """
        otra = await _find(user, statement.id, "FARMACIA DEL SOL")
        target = await _find(user, statement.id, "SUPERMERCADO COTO")

        async with db.system_tx(str(user)) as conn:
            updated = await review_service.edit_transaction(
                conn,
                target.id,
                {
                    "description_raw": otra.description_raw,
                    "amount": str(otra.amount),
                    "posted_date": otra.posted_date.strftime("%d/%m/%Y"),
                },
            )

        assert updated.dedupe_key == target.dedupe_key
        assert updated.dedupe_key != otra.dedupe_key
        assert updated.description_raw == otra.description_raw


# ─────────────────────────────────────────────────────────────────────────────
# Lo corregido sobrevive a un reparse
# ─────────────────────────────────────────────────────────────────────────────
class TestEdicionYReparse:
    async def test_una_correccion_no_se_duplica_al_reprocesar(
        self, user: uuid.UUID, statement: statements_repo.Statement
    ) -> None:
        """Si al editar no se recalculara la clave, quedarían dos filas.

        Una con el monto corregido (huérfana) y otra recién insertada con el
        monto que el modelo vuelve a leer mal. El usuario no vería el problema:
        vería el mes inflado.
        """
        target = await _find(user, statement.id, "SUPERMERCADO COTO")

        async with db.system_tx(str(user)) as conn:
            await review_service.edit_transaction(conn, target.id, {"amount": "61000.00"})

        await parse_service.parse_document(
            user_id=str(user),
            document_id=statement.document_id,
            extractor=_cassette(),
            settings=_settings(),
            allow_repair=False,
        )

        rows = await _rows(user, statement.id)
        coto = [row for row in rows if row.description_raw == "SUPERMERCADO COTO"]

        assert len(coto) == 1
        assert coto[0].amount == Decimal("61000.00")
        assert coto[0].review_status == "edited"


# ─────────────────────────────────────────────────────────────────────────────
# Acciones masivas
# ─────────────────────────────────────────────────────────────────────────────
class TestAccionesMasivas:
    async def test_confirmar_todo_lo_pendiente_de_una(
        self, user: uuid.UUID, statement: statements_repo.Statement
    ) -> None:
        async with db.system_tx(str(user)) as conn:
            changed = await review_service.bulk_review(
                conn, statement.id, transaction_ids=[], action="confirm"
            )

        assert changed == len(LINES)
        assert (await _state(user, statement.id)).pending == 0

    async def test_lo_ya_editado_no_lo_pisa_la_accion_masiva(
        self, user: uuid.UUID, statement: statements_repo.Statement
    ) -> None:
        """'Confirmar lo pendiente' significa lo pendiente, no todo."""
        target = await _find(user, statement.id, "SUPERMERCADO COTO")

        async with db.system_tx(str(user)) as conn:
            await review_service.edit_transaction(conn, target.id, {"amount": "61000.00"})
            await review_service.bulk_review(
                conn, statement.id, transaction_ids=[], action="confirm"
            )

        assert (await _find(user, statement.id, "SUPERMERCADO COTO")).review_status == "edited"

    async def test_una_accion_desconocida_se_rechaza(
        self, user: uuid.UUID, statement: statements_repo.Statement
    ) -> None:
        async with db.system_tx(str(user)) as conn:
            with pytest.raises(review_service.ReviewError, match="desconocida"):
                await review_service.bulk_review(
                    conn, statement.id, transaction_ids=[], action="borrar_todo"
                )

    async def test_un_id_de_otro_resumen_no_hace_nada(
        self, user: uuid.UUID, statement: statements_repo.Statement
    ) -> None:
        otro_doc = await _make_document(user)
        otro = await parse_service.parse_document(
            user_id=str(user),
            document_id=otro_doc.id,
            extractor=_cassette(),
            settings=_settings(),
        )
        ajena = (await _rows(user, otro.statement_ids[0]))[0]

        async with db.system_tx(str(user)) as conn:
            changed = await review_service.bulk_review(
                conn, statement.id, transaction_ids=[ajena.id], action="reject"
            )

        assert changed == 0
        assert (await _rows(user, otro.statement_ids[0]))[0].review_status == "pending"


# ─────────────────────────────────────────────────────────────────────────────
# Agregar y dividir
# ─────────────────────────────────────────────────────────────────────────────
class TestAgregar:
    async def test_agregar_lo_que_falta_cierra_el_cuadre(self, user: uuid.UUID) -> None:
        document = await _make_document(user)
        outcome = await parse_service.parse_document(
            user_id=str(user),
            document_id=document.id,
            extractor=_cassette(indexes=[0, 1, 2]),
            settings=_settings(),
            allow_repair=False,
        )
        statement_id = outcome.statement_ids[0]
        assert not (await _state(user, statement_id)).balanced

        async with db.system_tx(str(user)) as conn:
            created = await review_service.add_transaction(
                conn,
                statement_id,
                {
                    "posted_date": "18/03",
                    "description_raw": "IVA RG 4815",
                    "amount": "5.000,00",
                    "direction": "1",
                    "kind": "tax",
                },
            )

        # Sin `source_line`: queda visible que no salió del PDF.
        assert created.source_line is None
        assert created.review_status == "edited"
        assert (await _state(user, statement_id)).balanced

    async def test_sin_descripcion_se_rechaza(
        self, user: uuid.UUID, statement: statements_repo.Statement
    ) -> None:
        async with db.system_tx(str(user)) as conn:
            with pytest.raises(review_service.ReviewError, match="descripción"):
                await review_service.add_transaction(
                    conn, statement.id, {"posted_date": "18/03", "amount": "100.00"}
                )


class TestDividir:
    async def test_las_partes_reemplazan_a_la_original_sin_mover_el_total(
        self, user: uuid.UUID, statement: statements_repo.Statement
    ) -> None:
        target = await _find(user, statement.id, "SUPERMERCADO COTO")

        async with db.system_tx(str(user)) as conn:
            parts = await review_service.split_transaction(
                conn,
                target.id,
                [
                    {"amount": "45000.00", "description_raw": "COTO ALIMENTOS"},
                    {"amount": "15000.00", "description_raw": "COTO NAFTA"},
                ],
            )

        assert len(parts) == 2
        state = await _state(user, statement.id)
        assert state.balances[0].computed == TOTAL_ARS
        assert state.balanced

        # La original no se borra: queda descartada y con su `source_line`, así
        # que se puede ver de dónde salieron las partes.
        original = await _find(user, statement.id, "SUPERMERCADO COTO")
        assert original.review_status == "rejected"
        assert original.source_line is not None

    async def test_partes_que_no_suman_el_original_se_rechazan(
        self, user: uuid.UUID, statement: statements_repo.Statement
    ) -> None:
        target = await _find(user, statement.id, "SUPERMERCADO COTO")

        async with db.system_tx(str(user)) as conn:
            with pytest.raises(review_service.ReviewError, match="no puede cambiar el total"):
                await review_service.split_transaction(
                    conn,
                    target.id,
                    [{"amount": "45000.00"}, {"amount": "10000.00"}],
                )

    async def test_una_sola_parte_no_es_una_division(
        self, user: uuid.UUID, statement: statements_repo.Statement
    ) -> None:
        target = await _find(user, statement.id, "SUPERMERCADO COTO")

        async with db.system_tx(str(user)) as conn:
            with pytest.raises(review_service.ReviewError, match="al menos dos"):
                await review_service.split_transaction(conn, target.id, [{"amount": "60000.00"}])


# ─────────────────────────────────────────────────────────────────────────────
# Confirmar: la puerta al dashboard
# ─────────────────────────────────────────────────────────────────────────────
class TestConfirmacion:
    async def test_no_se_puede_confirmar_con_transacciones_sin_revisar(
        self, user: uuid.UUID, statement: statements_repo.Statement
    ) -> None:
        async with db.system_tx(str(user)) as conn:
            with pytest.raises(review_service.ReviewError) as exc:
                await review_service.confirm_statement(conn, statement.id, _settings())

        assert exc.value.code == "pending_transactions"

    async def test_revisado_y_cuadrado_se_confirma(
        self, user: uuid.UUID, statement: statements_repo.Statement
    ) -> None:
        async with db.system_tx(str(user)) as conn:
            await review_service.bulk_review(
                conn, statement.id, transaction_ids=[], action="confirm"
            )
            confirmed = await review_service.confirm_statement(conn, statement.id, _settings())

        assert confirmed.status == "confirmed"
        assert confirmed.accepted_delta is None

    async def test_sin_cuadrar_no_se_confirma_por_accidente(self, user: uuid.UUID) -> None:
        document = await _make_document(user)
        outcome = await parse_service.parse_document(
            user_id=str(user),
            document_id=document.id,
            extractor=_cassette(indexes=[0, 1, 2]),
            settings=_settings(),
            allow_repair=False,
        )

        async with db.system_tx(str(user)) as conn:
            await review_service.bulk_review(
                conn, outcome.statement_ids[0], transaction_ids=[], action="confirm"
            )
            with pytest.raises(review_service.ReviewError) as exc:
                await review_service.confirm_statement(conn, outcome.statement_ids[0], _settings())

        assert exc.value.code == "unbalanced"
        assert "5000.00" in exc.value.message

    async def test_la_diferencia_aceptada_queda_registrada(self, user: uuid.UUID) -> None:
        """Aceptar el delta es una decisión, y como tal se guarda."""
        document = await _make_document(user)
        outcome = await parse_service.parse_document(
            user_id=str(user),
            document_id=document.id,
            extractor=_cassette(indexes=[0, 1, 2]),
            settings=_settings(),
            allow_repair=False,
        )

        async with db.system_tx(str(user)) as conn:
            await review_service.bulk_review(
                conn, outcome.statement_ids[0], transaction_ids=[], action="confirm"
            )
            confirmed = await review_service.confirm_statement(
                conn, outcome.statement_ids[0], _settings(), accept_delta=True
            )

        assert confirmed.status == "confirmed"
        assert confirmed.accepted_delta == Decimal("5000.00")

    async def test_un_resumen_confirmado_no_se_edita(
        self, user: uuid.UUID, statement: statements_repo.Statement
    ) -> None:
        target = await _find(user, statement.id, "SUPERMERCADO COTO")

        async with db.system_tx(str(user)) as conn:
            await review_service.bulk_review(
                conn, statement.id, transaction_ids=[], action="confirm"
            )
            await review_service.confirm_statement(conn, statement.id, _settings())

            with pytest.raises(review_service.ReviewError) as exc:
                await review_service.edit_transaction(conn, target.id, {"amount": "1.00"})

        assert exc.value.code == "confirmed"

    async def test_reabrir_devuelve_el_resumen_a_revision_sin_perder_lo_confirmado(
        self, user: uuid.UUID, statement: statements_repo.Statement
    ) -> None:
        async with db.system_tx(str(user)) as conn:
            await review_service.bulk_review(
                conn, statement.id, transaction_ids=[], action="confirm"
            )
            await review_service.confirm_statement(conn, statement.id, _settings())
            reopened = await review_service.reopen_statement(conn, statement.id, _settings())

        assert reopened.status == "draft"
        rows = await _rows(user, statement.id)
        assert all(row.review_status == "confirmed" for row in rows)


# ─────────────────────────────────────────────────────────────────────────────
# Las rutas
# ─────────────────────────────────────────────────────────────────────────────
class TestRutas:
    @pytest_asyncio.fixture
    async def client(
        self, user: uuid.UUID, monkeypatch: pytest.MonkeyPatch
    ) -> AsyncIterator[httpx.AsyncClient]:
        from app.security import session as session_store

        monkeypatch.setenv("ENV", "test")
        monkeypatch.setenv("SESSION_SECRET", "un-secreto-de-mas-de-treinta-y-dos-caracteres")
        monkeypatch.setenv("SUPABASE_URL", SUPABASE_URL)
        monkeypatch.setenv("SUPABASE_ANON_KEY", "anon-key-de-prueba")
        monkeypatch.setenv("SUPABASE_JWT_SECRET", JWT_SECRET)
        reset_settings_cache()
        settings = get_settings()

        now = int(time.time())
        token = pyjwt.encode(
            {
                "sub": str(user),
                "aud": "authenticated",
                "role": "authenticated",
                "iss": f"{SUPABASE_URL}/auth/v1",
                "email": f"{user}@test.local",
                "session_id": str(uuid.uuid4()),
                "iat": now,
                "exp": now + 3600,
            },
            JWT_SECRET,
            algorithm="HS256",
        )
        cookie = session_store.encode(
            session_store.Session(
                user_id=str(user),
                email=f"{user}@test.local",
                access_token=token,
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
            yield http_client

        reset_settings_cache()

    def _csrf(self, user: uuid.UUID) -> str:
        from app.security import csrf

        return csrf.issue(str(user), get_settings())

    async def test_la_pantalla_muestra_el_cuadre_y_las_lineas_del_pdf(
        self, client: httpx.AsyncClient, statement: statements_repo.Statement
    ) -> None:
        response = await client.get(f"/statements/{statement.id}/review")

        assert response.status_code == 200
        assert "SUPERMERCADO COTO" in response.text
        # El monto se muestra en es-AR, como el resto de la app.
        assert "80.000,00" in response.text
        # Y la línea literal del PDF, al lado del dato.
        assert "15/03 SUPERMERCADO COTO" in response.text

    async def test_el_banner_muestra_el_total_a_pagar_del_resumen(
        self, client: httpx.AsyncClient, user: uuid.UUID, statement: statements_repo.Statement
    ) -> None:
        """El número que el usuario puede contrastar contra el PDF.

        El movimiento neto del período es con lo que se verifica, pero no está
        impreso en ningún lado del resumen. Mostrarlo solo a él hacía que una
        extracción correcta pareciera equivocada.
        """
        async with db.system_tx(str(user)) as conn:
            await conn.execute(
                text(
                    "update app.statements set previous_balance = 427937.66, "
                    "total_due = 507937.66 where id = :id"
                ),
                {"id": statement.id},
            )

        response = await client.get(f"/statements/{statement.id}/review")

        assert "Saldo anterior" in response.text
        assert "427.937,66" in response.text
        assert "Total a pagar" in response.text
        # 427.937,66 + 80.000,00 de movimiento del período.
        assert "507.937,66" in response.text
        assert "Pagos y créditos" in response.text
        assert "Cargos del período" in response.text

    async def test_confirmar_sin_revisar_responde_el_motivo_y_no_confirma(
        self, client: httpx.AsyncClient, user: uuid.UUID, statement: statements_repo.Statement
    ) -> None:
        response = await client.post(
            f"/statements/{statement.id}/confirm",
            data={"csrf_token": self._csrf(user)},
            headers={"HX-Request": "true"},
        )

        assert response.status_code == 200
        assert "sin revisar" in response.text

        async with db.system_tx(str(user)) as conn:
            fresh = await statements_repo.get(conn, statement.id)
        assert fresh is not None and fresh.status != "confirmed"

    async def test_editar_por_htmx_devuelve_el_panel_con_el_cuadre_actualizado(
        self, client: httpx.AsyncClient, user: uuid.UUID, statement: statements_repo.Statement
    ) -> None:
        """El banner depende de la suma de todas las filas: no puede quedar viejo."""
        target = await _find(user, statement.id, "SUPERMERCADO COTO")

        response = await client.post(
            f"/statements/{statement.id}/transactions/{target.id}",
            data={"csrf_token": self._csrf(user), "amount": "70.000,00"},
            headers={"HX-Request": "true"},
        )

        assert response.status_code == 200
        assert "review-panel" in response.text
        # 80.000 + 10.000 de diferencia por la corrección.
        assert "90.000,00" in response.text

    async def test_sin_htmx_redirige_a_la_pagina(
        self, client: httpx.AsyncClient, user: uuid.UUID, statement: statements_repo.Statement
    ) -> None:
        response = await client.post(
            f"/statements/{statement.id}/bulk",
            data={"csrf_token": self._csrf(user), "action": "confirm"},
            follow_redirects=False,
        )

        assert response.status_code == 303
        assert response.headers["location"] == f"/statements/{statement.id}/review"

    async def test_sin_csrf_no_se_toca_nada(
        self, client: httpx.AsyncClient, user: uuid.UUID, statement: statements_repo.Statement
    ) -> None:
        response = await client.post(f"/statements/{statement.id}/bulk", data={"action": "confirm"})

        assert response.status_code == 403
        assert (await _state(user, statement.id)).pending == len(LINES)

    async def test_la_pantalla_explica_las_huerfanas(
        self, client: httpx.AsyncClient, user: uuid.UUID, statement: statements_repo.Statement
    ) -> None:
        """Una fila que sobrevivió a la re-corrida no puede verse como duplicado."""
        async with db.system_tx(str(user)) as conn:
            await review_service.bulk_review(
                conn, statement.id, transaction_ids=[], action="confirm"
            )

        await parse_service.parse_document(
            user_id=str(user),
            document_id=statement.document_id,
            extractor=_cassette(indexes=[0, 1, 2]),
            settings=_settings(),
            allow_repair=False,
        )

        response = await client.get(f"/statements/{statement.id}/review")

        assert "no aparecen en la última extracción" in response.text
        assert "ya no la extrae" in response.text
        assert "IVA RG 4815" in response.text

    async def test_el_resumen_de_otro_usuario_no_existe(
        self, client: httpx.AsyncClient, two_users: tuple[Actor, Actor]
    ) -> None:
        _owner, other = two_users
        document = await _make_document(other.id)
        outcome = await parse_service.parse_document(
            user_id=str(other.id),
            document_id=document.id,
            extractor=_cassette(),
            settings=_settings(),
        )

        response = await client.get(f"/statements/{outcome.statement_ids[0]}/review")

        assert response.status_code == 404


class TestHuerfanas:
    """Lo que sobrevive a una re-corrida tiene que verse, no solo registrarse.

    El caso real: confirmaste una cuota, después cambió una regla de
    normalización (la fecha de las cuotas) y su `dedupe_key` dejó de coincidir
    con lo que el modelo produce. La reconciliación conserva la confirmada —nunca
    borra lo que tocaste— e inserta la nueva. Sin marcarlo, en la pantalla eso se
    lee como un duplicado inexplicable y el total queda inflado sin motivo
    visible.
    """

    async def test_una_confirmada_que_el_modelo_ya_no_produce_se_marca(
        self, user: uuid.UUID, statement: statements_repo.Statement
    ) -> None:
        async with db.system_tx(str(user)) as conn:
            await review_service.bulk_review(
                conn, statement.id, transaction_ids=[], action="confirm"
            )

        target = await _find(user, statement.id, "IVA RG 4815")

        # El modelo deja de ver esa línea.
        await parse_service.parse_document(
            user_id=str(user),
            document_id=statement.document_id,
            extractor=_cassette(indexes=[0, 1, 2]),
            settings=_settings(),
            allow_repair=False,
        )

        state = await _state(user, statement.id)

        assert target.id in state.orphan_ids
        assert [tx.id for tx in state.orphans] == [target.id]

    async def test_sin_re_corrida_no_hay_huerfanas(
        self, user: uuid.UUID, statement: statements_repo.Statement
    ) -> None:
        state = await _state(user, statement.id)

        assert state.orphan_ids == frozenset()
        assert state.conflict_ids == frozenset()
