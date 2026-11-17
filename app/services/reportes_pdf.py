"""Utilidades compartidas para exportar reportes a PDF con fpdf2."""
from __future__ import annotations

from fpdf import FPDF
from fpdf.enums import XPos, YPos


def texto_pdf(valor) -> str:
    """Adapta cualquier valor a la fuente estándar (Latin-1) de fpdf2."""
    return str("" if valor is None else valor).encode("latin-1", "replace").decode(
        "latin-1"
    )


def crear_pdf(titulo: str, subtitulo: str | None = None) -> FPDF:
    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 15)
    pdf.cell(
        0,
        10,
        texto_pdf(titulo),
        new_x=XPos.LMARGIN,
        new_y=YPos.NEXT,
    )
    if subtitulo:
        pdf.set_font("Helvetica", size=9)
        pdf.multi_cell(0, 5, texto_pdf(subtitulo))
    pdf.ln(2)
    return pdf


def parrafo(pdf: FPDF, texto: str, *, negrita: bool = False, tamano: int = 10) -> None:
    pdf.set_font("Helvetica", "B" if negrita else "", tamano)
    pdf.multi_cell(0, 5, texto_pdf(texto))
    pdf.ln(1)


def tabla(
    pdf: FPDF,
    encabezados: list[str],
    filas: list[list],
    *,
    anchos: list[float] | None = None,
    tamano: int = 8,
) -> None:
    """Dibuja una tabla simple con salto de página automático."""
    if anchos is None:
        ancho = 190 / max(len(encabezados), 1)
        anchos = [ancho] * len(encabezados)

    pdf.set_font("Helvetica", "B", tamano)
    for encabezado, ancho in zip(encabezados, anchos):
        pdf.cell(ancho, 6, texto_pdf(encabezado), border=1)
    pdf.ln()

    pdf.set_font("Helvetica", size=tamano)
    for fila in filas:
        if pdf.get_y() > 260:
            pdf.add_page()
            pdf.set_font("Helvetica", "B", tamano)
            for encabezado, ancho in zip(encabezados, anchos):
                pdf.cell(ancho, 6, texto_pdf(encabezado), border=1)
            pdf.ln()
            pdf.set_font("Helvetica", size=tamano)
        for valor, ancho in zip(fila, anchos):
            pdf.cell(ancho, 5, texto_pdf(valor)[:80], border=1)
        pdf.ln()
    pdf.ln(2)


def salida_pdf(pdf: FPDF) -> bytes:
    """fpdf2 regresa bytearray en unas versiones y str en otras muy antiguas."""
    return bytes(pdf.output())
