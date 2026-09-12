"""El corte del texto en fragmentos.

Lo que se verifica no es "corta bien" en abstracto, sino las tres propiedades de
las que depende la etapa 2: que ningun fragmento pase el limite, que el
solapamiento realmente repita el final del anterior (si no, una transaccion
partida entre paginas se pierde) y que una pagina gigante se parta en lugar de
mandarse entera.
"""

from __future__ import annotations

import pytest

from app.pdf import chunking


def _page(number: int, text: str, *, flow: str | None = None) -> dict[str, object]:
    return {"page": number, "text_layout": text, "text_flow": flow or text, "tables": []}


def _pages(count: int, *, chars: int = 100) -> list[dict[str, object]]:
    return [_page(n, f"pagina {n}\n" + ("x" * chars)) for n in range(1, count + 1)]


class TestChunkPages:
    def test_paginas_chicas_entran_en_un_solo_fragmento(self) -> None:
        chunks = chunking.chunk_pages(_pages(3), max_chars=5000)

        assert len(chunks) == 1
        assert chunks[0].first_page == 1
        assert chunks[0].last_page == 3
        assert "pagina 2" in chunks[0].text

    def test_ningun_fragmento_supera_el_limite(self) -> None:
        chunks = chunking.chunk_pages(_pages(10, chars=400), max_chars=1000, overlap=100)

        # El solapamiento se suma al cuerpo, asi que el tope real es max + overlap.
        assert all(chunk.char_count <= 1000 + 100 + 64 for chunk in chunks)
        assert len(chunks) > 1

    def test_los_indices_son_consecutivos_desde_cero(self) -> None:
        chunks = chunking.chunk_pages(_pages(8, chars=400), max_chars=1000)

        assert [chunk.index for chunk in chunks] == list(range(len(chunks)))

    def test_el_solapamiento_repite_el_final_del_fragmento_anterior(self) -> None:
        pages = [
            _page(1, "linea A\nlinea B\nUNA MARCA UNICA AL FINAL"),
            _page(2, "linea C\nlinea D"),
        ]

        chunks = chunking.chunk_pages(pages, max_chars=60, overlap=40)

        assert len(chunks) == 2
        assert "UNA MARCA UNICA AL FINAL" in chunks[1].text
        assert chunks[1].text.startswith("[continuacion")

    def test_sin_solapamiento_no_se_repite_nada(self) -> None:
        pages = [_page(1, "MARCA FINAL" + "x" * 80), _page(2, "y" * 80)]

        chunks = chunking.chunk_pages(pages, max_chars=120, overlap=0)

        assert len(chunks) == 2
        assert "MARCA FINAL" not in chunks[1].text

    def test_una_pagina_que_no_entra_se_parte_por_lineas(self) -> None:
        big = _page(1, "\n".join(f"linea {n:04d} " + "z" * 40 for n in range(200)))

        chunks = chunking.chunk_pages([big], max_chars=1000, overlap=0)

        assert len(chunks) > 1
        # Se partio en limites de linea: ninguna linea quedo cortada al medio.
        for chunk in chunks:
            for line in chunk.text.splitlines():
                assert line.startswith(("linea ", "--- pagina")) or not line

    def test_las_paginas_en_blanco_se_saltean(self) -> None:
        pages = [_page(1, "   "), _page(2, "contenido real"), _page(3, "")]

        chunks = chunking.chunk_pages(pages)

        assert len(chunks) == 1
        assert chunks[0].first_page == 2
        assert chunks[0].last_page == 2

    def test_sin_paginas_no_hay_fragmentos(self) -> None:
        assert chunking.chunk_pages([]) == []

    @pytest.mark.parametrize(
        ("max_chars", "overlap"),
        [(0, 10), (-1, 10), (100, 100), (100, 200), (100, -1)],
    )
    def test_parametros_incoherentes_fallan_temprano(self, max_chars: int, overlap: int) -> None:
        with pytest.raises(ValueError):
            chunking.chunk_pages(_pages(2), max_chars=max_chars, overlap=overlap)


class TestPageText:
    def test_usa_el_layout_por_defecto(self) -> None:
        page = _page(1, "con columnas", flow="en orden de lectura")

        assert chunking.page_text(page) == "con columnas"

    def test_cae_al_flow_cuando_el_layout_vino_vacio(self) -> None:
        page = {"page": 1, "text_layout": "", "text_flow": "algo es mejor que nada"}

        assert chunking.page_text(page) == "algo es mejor que nada"


class TestHeaderText:
    def test_si_el_documento_entra_entero_se_manda_entero(self) -> None:
        """Recortar de más fue un bug real.

        Naranja X pone el saldo anterior en la página 2 de 4. Con el recorte a
        "primera y última" el modelo nunca lo veía y devolvía null, y el síntoma
        —un resumen sin totales con los que verificar— parecía un problema de
        prompt cuando era de recorte.
        """
        pages = [_page(n, f"contenido de la pagina {n}") for n in range(1, 6)]

        text = chunking.header_text(pages)

        for n in range(1, 6):
            assert f"contenido de la pagina {n}" in text

    def test_cuando_no_entra_entero_prioriza_las_primeras_y_la_ultima(self) -> None:
        pages = [_page(n, f"pagina {n} " + "x" * 900) for n in range(1, 8)]

        text = chunking.header_text(pages, max_chars=3000)

        assert "--- pagina 1 ---" in text
        assert "--- pagina 2 ---" in text
        assert "--- pagina 7 ---" in text
        assert "--- pagina 4 ---" not in text
        assert len(text) <= 3000

    def test_si_ni_asi_entra_recorta_del_medio(self) -> None:
        pages = [_page(n, f"pagina {n} " + "x" * 4000) for n in range(1, 6)]

        text = chunking.header_text(pages, max_chars=1000)

        assert "[... texto omitido ...]" in text

    def test_con_una_sola_pagina_no_la_duplica(self) -> None:
        text = chunking.header_text([_page(1, "unica")])

        assert text.count("unica") == 1

    def test_recorta_del_medio_cuando_es_muy_largo(self) -> None:
        pages = [_page(1, "A" * 5000), _page(2, "B" * 5000)]

        text = chunking.header_text(pages, max_chars=1000)

        assert "[... texto omitido ...]" in text
        assert text.startswith("--- pagina 1 ---")
        assert text.rstrip().endswith("B")
        assert len(text) <= 1000 + 64

    def test_sin_paginas_devuelve_vacio(self) -> None:
        assert chunking.header_text([]) == ""
