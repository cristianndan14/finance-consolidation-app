"""Armado de los ejemplos few-shot a partir de las correcciones del usuario."""

from __future__ import annotations

from app.llm import fewshot
from app.llm.ports import MerchantExample

SLUGS = ["suscripciones", "supermercado", "transporte"]


def test_normaliza_la_descripcion_a_la_clave_del_cache() -> None:
    """El ejemplo tiene que estar en el mismo vocabulario que la pregunta."""
    built = fewshot.build(
        [("MERPAGO*SPOTIFY 1234 BUENOS AIRES AR", "Spotify", "suscripciones")],
        category_slugs=SLUGS,
    )

    assert built == [
        MerchantExample(raw_key="SPOTIFY", canonical_name="Spotify", category_slug="suscripciones")
    ]


def test_descarta_slugs_que_ya_no_estan_en_el_catalogo() -> None:
    """Enseñarle una categoria que el sistema descarta es enseñarle a fallar."""
    built = fewshot.build(
        [("NETFLIX", "Netflix", "streaming-viejo")],
        category_slugs=SLUGS,
    )

    assert built == []


def test_no_le_da_la_respuesta_de_lo_que_se_esta_preguntando() -> None:
    built = fewshot.build(
        [
            ("MERPAGO*SPOTIFY 99", "Spotify", "suscripciones"),
            ("COTO SUC 4", "Coto", "supermercado"),
        ],
        category_slugs=SLUGS,
        exclude_keys=["MERPAGO*SPOTIFY 1234 CABA"],
    )

    assert [example.raw_key for example in built] == ["COTO SUC"]


def test_ante_dos_correcciones_de_la_misma_clave_gana_la_primera() -> None:
    """La entrada viene con lo mas reciente adelante: la ultima palabra es del usuario."""
    built = fewshot.build(
        [("SPOTIFY 1", "Spotify", "suscripciones"), ("SPOTIFY 2", "Spotify AB", "transporte")],
        category_slugs=SLUGS,
    )

    assert len(built) == 1
    assert built[0].category_slug == "suscripciones"


def test_corta_en_el_limite() -> None:
    # Nombres y no numeros: `merchant_key` borra los digitos, y veinte claves que
    # solo se diferencian en un numero son una sola clave.
    corrections = [(f"COMERCIO {chr(65 + i)}", "Comercio", "supermercado") for i in range(20)]

    assert len(fewshot.build(corrections, category_slugs=SLUGS, limit=3)) == 3


def test_render_sin_ejemplos_dice_que_no_hay() -> None:
    """Un titulo sin contenido invita al modelo a inventarle contenido."""
    rendered = fewshot.render([])

    assert rendered.strip()
    assert "`" not in rendered


def test_render_arma_una_linea_por_ejemplo() -> None:
    rendered = fewshot.render(
        [
            MerchantExample(
                raw_key="SPOTIFY", canonical_name="Spotify", category_slug="suscripciones"
            ),
            MerchantExample(raw_key="COTO", canonical_name="Coto", category_slug="supermercado"),
        ]
    )

    assert rendered.splitlines() == [
        "- `SPOTIFY` → Spotify (suscripciones)",
        "- `COTO` → Coto (supermercado)",
    ]
