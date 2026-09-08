"""Tests de deduplicacion.

El caso central: distinguir "el mismo resumen se procesa dos veces" de "el resumen
tiene dos lineas legitimamente iguales". Resolver uno rompiendo el otro es facil;
resolver los dos es el punto de este modulo.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

import pytest

from app.domain.dedupe import (
    assign_occurrence_indexes,
    dedupe_key,
    drop_overlap_duplicates,
    installment_group_key,
    normalize_description,
)


@dataclass
class Row:
    """Doble minimo de una transaccion extraida."""

    dedupe_key: str
    label: str = ""


def key(**overrides: object) -> str:
    base: dict[str, object] = {
        "description": "SUPERMERCADO DIA 1234",
        "posted_date": date(2026, 3, 15),
        "amount": Decimal("1234.56"),
        "currency": "ARS",
    }
    base.update(overrides)
    return dedupe_key(**base)  # type: ignore[arg-type]


class TestNormalizeDescription:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Café Martínez", "CAFE MARTINEZ"),
            ("Café  Martínez -- Suc. 12", "CAFE MARTINEZ SUC 12"),
            ("  espacios   raros  ", "ESPACIOS RAROS"),
            ("MERPAGO*SPOTIFY", "MERPAGO SPOTIFY"),
            ("A.B.C.", "A B C"),
            ("ÑOÑO", "NONO"),
        ],
    )
    def test_normaliza(self, raw: str, expected: str) -> None:
        assert normalize_description(raw) == expected

    def test_variaciones_de_espaciado_dan_lo_mismo(self) -> None:
        """El extractor de texto no siempre produce el mismo espaciado."""
        assert normalize_description("DIA  1234") == normalize_description("DIA 1234")


class TestDedupeKey:
    def test_es_estable(self) -> None:
        assert key() == key()

    def test_tiene_forma_de_sha256(self) -> None:
        result = key()
        assert len(result) == 64
        assert all(c in "0123456789abcdef" for c in result)

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("description", "OTRO COMERCIO"),
            ("posted_date", date(2026, 3, 16)),
            ("amount", Decimal("1234.57")),
            ("currency", "USD"),
            ("installment_number", 3),
            ("installment_total", 12),
        ],
    )
    def test_cambiar_cualquier_campo_cambia_la_clave(self, field: str, value: object) -> None:
        assert key(**{field: value}) != key()

    def test_el_espaciado_no_cambia_la_clave(self) -> None:
        assert key(description="SUPERMERCADO  DIA   1234") == key()

    def test_las_cuotas_distinguen_consumos_identicos(self) -> None:
        """La cuota 3/12 y la 4/12 pueden tener igual monto y descripcion.

        Si el emisor informa dos cuotas en el mismo resumen, sin el numero de cuota
        en la clave se veria una sola.
        """
        c3 = key(installment_number=3, installment_total=12)
        c4 = key(installment_number=4, installment_total=12)
        assert c3 != c4

    def test_la_categoria_no_participa(self) -> None:
        """La clave se calcula solo con datos de la extraccion.

        Si incluyera la categoria o el comercio — que los asigna el enriquecimiento —
        recategorizar cambiaria la identidad de la transaccion, y el merge de un
        reparse la veria como nueva en lugar de reconocerla.
        """
        import inspect

        params = inspect.signature(dedupe_key).parameters
        assert "category_id" not in params
        assert "merchant_id" not in params


class TestAssignOccurrenceIndexes:
    def test_numera_las_repeticiones_en_orden(self) -> None:
        rows = [Row("a"), Row("b"), Row("a"), Row("a")]
        assert [idx for _, idx in assign_occurrence_indexes(rows)] == [0, 0, 1, 2]

    def test_preserva_el_orden_del_resumen(self) -> None:
        rows = [Row("a", "primera"), Row("b", "segunda"), Row("a", "tercera")]
        assert [r.label for r, _ in assign_occurrence_indexes(rows)] == [
            "primera",
            "segunda",
            "tercera",
        ]

    def test_dos_cafes_iguales_sobreviven(self) -> None:
        """El caso que una clave hash sola resolveria mal.

        Dos consumos identicos el mismo dia son dos gastos reales. Colapsarlos hace
        que el mes cierre corto y que el cuadre contra el total del resumen falle.
        """
        cafe = key(description="CAFE MARTINEZ", amount=Decimal("2500.00"))
        rows = [Row(cafe), Row(cafe)]
        result = assign_occurrence_indexes(rows)
        assert len(result) == 2
        assert {idx for _, idx in result} == {0, 1}

    def test_lote_vacio(self) -> None:
        assert assign_occurrence_indexes([]) == []

    def test_reprocesar_el_mismo_lote_da_los_mismos_indices(self) -> None:
        """Es lo que hace que el unique de la tabla sea idempotente."""
        rows = [Row("a"), Row("a"), Row("b")]
        primera = [(r.dedupe_key, i) for r, i in assign_occurrence_indexes(rows)]
        segunda = [(r.dedupe_key, i) for r, i in assign_occurrence_indexes(rows)]
        assert primera == segunda


class TestDropOverlapDuplicates:
    def test_colapsa_por_clave(self) -> None:
        rows = [Row("a"), Row("b"), Row("a")]
        assert [r.dedupe_key for r in drop_overlap_duplicates(rows)] == ["a", "b"]

    def test_conserva_la_primera_aparicion(self) -> None:
        rows = [Row("a", "del chunk 1"), Row("a", "del chunk 2")]
        assert [r.label for r in drop_overlap_duplicates(rows)] == ["del chunk 1"]

    def test_es_lo_contrario_de_assign_indexes_y_es_a_proposito(self) -> None:
        """Los dos ven claves repetidas y hacen cosas opuestas.

        `drop_overlap_duplicates` colapsa porque las repeticiones que ve son
        artefactos del solapamiento entre chunks. `assign_occurrence_indexes`
        preserva porque las que ve son consumos reales. Por eso el orden importa:
        primero se quitan los artefactos, despues se numeran los reales.
        """
        rows = [Row("a"), Row("a")]
        assert len(drop_overlap_duplicates(rows)) == 1
        assert len(assign_occurrence_indexes(rows)) == 2


class TestInstallmentGroupKey:
    def test_es_estable_entre_meses(self) -> None:
        """No puede depender de la fecha ni del numero de cuota.

        Si dependiera, cada mes generaria un grupo distinto y la proyeccion de
        cuotas futuras no podria armarse.
        """
        argumentos = {
            "description": "NOTEBOOK LENOVO",
            "installment_total": 12,
            "purchase_total_amount": Decimal("1200000.00"),
            "purchase_date": date(2026, 1, 10),
        }
        assert installment_group_key(**argumentos) == installment_group_key(**argumentos)

    def test_compras_distintas_dan_grupos_distintos(self) -> None:
        base = {
            "description": "NOTEBOOK LENOVO",
            "installment_total": 12,
            "purchase_date": date(2026, 1, 10),
        }
        a = installment_group_key(**base, purchase_total_amount=Decimal("1200000.00"))
        b = installment_group_key(**base, purchase_total_amount=Decimal("900000.00"))
        assert a != b

    def test_sin_total_ni_fecha_la_clave_es_mas_debil(self) -> None:
        """Limite conocido y aceptado.

        Algunos emisores no informan el total ni la fecha de la compra original. Sin
        esos datos, dos compras distintas en el mismo comercio con la misma cantidad
        de cuotas se agrupan juntas. La alternativa seria no agrupar nada y perder la
        proyeccion de cuotas futuras, que es peor.
        """
        a = installment_group_key(
            description="TIENDA X",
            installment_total=6,
            purchase_total_amount=None,
            purchase_date=None,
        )
        b = installment_group_key(
            description="TIENDA X",
            installment_total=6,
            purchase_total_amount=None,
            purchase_date=None,
        )
        assert a == b
