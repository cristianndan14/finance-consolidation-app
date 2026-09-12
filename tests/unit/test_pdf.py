"""Tests de la etapa 1: validacion, descifrado, saneado y extraccion de texto.

Todo con PDFs sinteticos generados en el momento (ver `tests/fixtures/pdfs.py`):
los resumenes reales no van al repo.
"""

from __future__ import annotations

import io

import pikepdf
import pytest

from app.pdf import extract as pdf_extract
from app.pdf import inspect as pdf_inspect
from tests.fixtures import pdfs

MAX_PAGES = 40
PASSWORD = "12345678"


class TestValidacion:
    def test_un_pdf_normal_pasa(self) -> None:
        result = pdf_inspect.normalize(pdfs.text_pdf(pages=3), max_pages=MAX_PAGES)

        assert result.page_count == 3
        assert result.was_encrypted is False
        assert result.data.startswith(b"%PDF-")

    def test_algo_que_no_es_pdf_se_rechaza(self) -> None:
        """El content-type del multipart lo elige el cliente: no es evidencia."""
        with pytest.raises(pdf_inspect.NotAPdfError):
            pdf_inspect.normalize(b"GIF89a" + b"\x00" * 1000, max_pages=MAX_PAGES)

    def test_un_pdf_truncado_se_rechaza(self) -> None:
        truncado = pdfs.text_pdf(pages=1)[:400]
        with pytest.raises(pdf_inspect.NotAPdfError):
            pdf_inspect.normalize(truncado, max_pages=MAX_PAGES)

    def test_demasiadas_paginas(self) -> None:
        with pytest.raises(pdf_inspect.TooManyPagesError):
            pdf_inspect.normalize(pdfs.text_pdf(pages=4), max_pages=3)


class TestPdfConContraseña:
    """El caso normal para media Argentina: el resumen viene cifrado con el DNI."""

    def test_sin_contraseña_pide_contraseña(self) -> None:
        with pytest.raises(pdf_inspect.PasswordRequiredError):
            pdf_inspect.normalize(pdfs.encrypted_pdf(PASSWORD), max_pages=MAX_PAGES)

    def test_con_la_contraseña_equivocada(self) -> None:
        """Se distingue de "falta la contraseña": el usuario tiene que saber cual
        de los dos errores es."""
        with pytest.raises(pdf_inspect.WrongPasswordError):
            pdf_inspect.normalize(
                pdfs.encrypted_pdf(PASSWORD), password="otra-cosa", max_pages=MAX_PAGES
            )

    def test_se_guarda_descifrado(self) -> None:
        """Lo que va a Storage abre sin contraseña.

        Es lo que permite reprocesar el documento mas adelante sin volver a
        pedirsela al usuario — el job de extraccion no la tiene.
        """
        result = pdf_inspect.normalize(
            pdfs.encrypted_pdf(PASSWORD), password=PASSWORD, max_pages=MAX_PAGES
        )

        assert result.was_encrypted is True
        with pikepdf.open(io.BytesIO(result.data)) as reopened:
            assert len(reopened.pages) == 1

    def test_la_contraseña_no_queda_en_el_resultado(self) -> None:
        result = pdf_inspect.normalize(
            pdfs.encrypted_pdf(PASSWORD), password=PASSWORD, max_pages=MAX_PAGES
        )
        assert PASSWORD.encode() not in result.data


class TestSaneado:
    def test_se_saca_el_javascript_y_las_acciones(self) -> None:
        """Un PDF es un formato ejecutable y este archivo despues se descarga."""
        result = pdf_inspect.normalize(pdfs.pdf_with_javascript(), max_pages=MAX_PAGES)

        with pikepdf.open(io.BytesIO(result.data)) as reopened:
            assert "/OpenAction" not in reopened.Root
            names = reopened.Root.get("/Names")
            assert names is None or "/JavaScript" not in names


class TestExtraccion:
    def test_las_tres_representaciones_por_pagina(self) -> None:
        extracted = pdf_extract.extract(pdfs.text_pdf(pages=2))

        assert extracted.page_count == 2
        for page in extracted.page_texts:
            assert set(page) == {"page", "text_layout", "text_flow", "tables"}
            assert "SUPERMERCADO COTO" in page["text_flow"]

    def test_el_layout_conserva_las_columnas(self) -> None:
        """`layout=True` es lo que mantiene fecha, descripcion y monto alineados;
        sin eso, la etapa 2 recibe las columnas mezcladas."""
        extracted = pdf_extract.extract(pdfs.text_pdf(pages=1))
        layout = extracted.page_texts[0]["text_layout"]

        linea = next(line for line in layout.splitlines() if "SUPERMERCADO COTO" in line)
        assert linea.index("05/01") < linea.index("SUPERMERCADO")
        assert linea.index("SUPERMERCADO") < linea.index("45.320,15")

    def test_el_texto_completo_marca_las_paginas(self) -> None:
        """La etapa 2 necesita poder decir de que pagina salio cada transaccion."""
        extracted = pdf_extract.extract(pdfs.text_pdf(pages=3))

        assert "--- pagina 1 ---" in extracted.full_text
        assert "--- pagina 3 ---" in extracted.full_text

    def test_un_pdf_con_texto_tiene_capa_de_texto(self) -> None:
        extracted = pdf_extract.extract(pdfs.text_pdf(pages=2))

        assert extracted.has_text_layer is True
        assert extracted.char_count > pdf_extract.MIN_CHARS_PER_PAGE * 2

    def test_un_escaneo_no_tiene_capa_de_texto(self) -> None:
        """El umbral es por pagina: un documento largo con cuatro palabras sueltas
        es un escaneo, no un resumen corto."""
        extracted = pdf_extract.extract(pdfs.scanned_pdf(pages=3))

        assert extracted.has_text_layer is False
        assert extracted.char_count < pdf_extract.MIN_CHARS_PER_PAGE

    def test_la_version_del_extractor_queda_registrada(self) -> None:
        """Es parte de la clave unica: permite mejorar la extraccion sin perder
        la anterior."""
        extracted = pdf_extract.extract(pdfs.text_pdf(pages=1))

        assert extracted.extractor == "pdfplumber"
        assert "layout+flow+tables" in extracted.extractor_version
