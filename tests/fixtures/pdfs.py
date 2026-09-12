"""PDFs sinteticos para los tests, generados en memoria.

No hay archivos: los resumenes reales tienen todos los consumos de una persona y
no van al repo (`.gitignore` bloquea cualquier `.pdf` fuera de
`tests/fixtures/pdfs/`). Estos se arman con reportlab en cada corrida, lo que
ademas hace que los tests no dependan de ningun binario commiteado.

Lo que se reproduce es la *forma* de un resumen de tarjeta —columnas de fecha,
descripcion y monto— porque es lo que ejercita la extraccion con `layout=True`.
"""

from __future__ import annotations

import io

import pikepdf
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

CONSUMOS = [
    ("05/01", "SUPERMERCADO COTO ABASTO", "45.320,15"),
    ("07/01", "MERCADOPAGO*SPOTIFY", "2.999,00"),
    ("12/01", "YPF SERVICENTRO PALERMO", "38.000,00"),
    ("18/01", "FARMACITY SUC 231", "12.450,80"),
    ("22/01", "AEROLINEAS ARG CUOTA 3/6", "89.999,99"),
]


def text_pdf(pages: int = 2, title: str = "RESUMEN DE CUENTA") -> bytes:
    """Un PDF con capa de texto y layout de columnas, como un resumen real."""
    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)

    for page in range(1, pages + 1):
        pdf.setFont("Helvetica-Bold", 14)
        pdf.drawString(60, 780, f"{title} - HOJA {page}")

        pdf.setFont("Courier", 10)
        y = 740
        for repeat in range(6):
            for fecha, descripcion, monto in CONSUMOS:
                pdf.drawString(60, y, fecha)
                pdf.drawString(120, y, f"{descripcion} {repeat}")
                pdf.drawRightString(520, y, monto)
                y -= 16

        pdf.setFont("Helvetica-Bold", 11)
        pdf.drawString(60, y - 20, "TOTAL CONSUMOS DEL PERIODO      1.234.567,89")
        pdf.showPage()

    pdf.save()
    return buffer.getvalue()


def scanned_pdf(pages: int = 3) -> bytes:
    """Un PDF sin texto util: lo que devuelve un escaneo.

    Se le deja un titulo minusculo para que no sea un caso degenerado de cero
    caracteres: lo que se testea es el umbral por pagina, no `text == ""`.
    """
    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    for page in range(1, pages + 1):
        pdf.setFont("Helvetica", 9)
        pdf.drawString(60, 780, f"pag {page}")
        pdf.showPage()
    pdf.save()
    return buffer.getvalue()


def encrypted_pdf(password: str, pages: int = 1) -> bytes:
    """Un PDF protegido con contraseña, como los manda Galicia o Santander."""
    source = text_pdf(pages=pages)
    buffer = io.BytesIO()
    with pikepdf.open(io.BytesIO(source)) as pdf:
        pdf.save(buffer, encryption=pikepdf.Encryption(owner=password, user=password, R=6))
    return buffer.getvalue()


def pdf_with_javascript() -> bytes:
    """Un PDF con JavaScript y una accion al abrir.

    Un resumen de tarjeta no tiene por que traer nada de esto; el test verifica
    que el saneado lo saca antes de guardar el archivo.
    """
    buffer = io.BytesIO()
    with pikepdf.open(io.BytesIO(text_pdf(pages=1))) as pdf:
        pdf.Root[pikepdf.Name("/OpenAction")] = pdf.make_indirect(
            pikepdf.Dictionary(S=pikepdf.Name("/JavaScript"), JS="app.alert('hola');")
        )
        pdf.Root[pikepdf.Name("/Names")] = pdf.make_indirect(
            pikepdf.Dictionary(
                JavaScript=pikepdf.Dictionary(
                    Names=pikepdf.Array(
                        [
                            "script",
                            pdf.make_indirect(
                                pikepdf.Dictionary(
                                    S=pikepdf.Name("/JavaScript"), JS="app.alert('x');"
                                )
                            ),
                        ]
                    )
                )
            )
        )
        pdf.save(buffer)
    return buffer.getvalue()
