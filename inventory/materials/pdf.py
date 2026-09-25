"""Generates the official quotation PDF. Entirely derived from a Quotation
record's own fields, never stored on disk — regenerated fresh every time
it's needed (attached to the Send Quote email, or re-downloaded later),
the same "derive it, don't persist it" approach coil_tag.html already uses
for QR codes."""
import io
from xml.sax.saxutils import escape

from django.conf import settings
from django.utils import timezone
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
)


def generate_quotation_pdf(quotation):
    """Returns the rendered PDF as bytes."""
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4,
        topMargin=20 * mm, bottomMargin=20 * mm, leftMargin=20 * mm, rightMargin=20 * mm,
    )
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle('QuoteTitle', parent=styles['Heading1'], fontSize=20, spaceAfter=2 * mm)
    company_style = ParagraphStyle('Company', parent=styles['Normal'], fontSize=14, leading=17)
    meta_style = ParagraphStyle('Meta', parent=styles['Normal'], fontSize=10, textColor=colors.HexColor('#555555'))
    section_style = ParagraphStyle('Section', parent=styles['Heading3'], fontSize=11, spaceBefore=6 * mm, spaceAfter=2 * mm)
    terms_style = ParagraphStyle('Terms', parent=styles['Normal'], fontSize=9, textColor=colors.HexColor('#555555'), leading=13)

    story = []

    story.append(Paragraph(escape(settings.COMPANY_NAME), company_style))
    company_lines = [line for line in [settings.COMPANY_ADDRESS, settings.COMPANY_PHONE, settings.COMPANY_EMAIL] if line]
    if settings.COMPANY_GST:
        company_lines.append(f"GSTIN: {settings.COMPANY_GST}")
    for line in company_lines:
        story.append(Paragraph(escape(line), meta_style))
    story.append(Spacer(1, 8 * mm))

    story.append(Paragraph("QUOTATION", title_style))
    story.append(Paragraph(
        f"{quotation.formatted_no()} &nbsp;&middot;&nbsp; "
        f"{timezone.localtime(quotation.created_at).strftime('%d %b %Y')}",
        meta_style,
    ))
    story.append(Spacer(1, 6 * mm))

    story.append(Paragraph("To", section_style))
    story.append(Paragraph(escape(quotation.customer.name), styles['Normal']))
    if quotation.customer.email:
        story.append(Paragraph(escape(quotation.customer.email), meta_style))
    if quotation.customer.phone:
        story.append(Paragraph(escape(quotation.customer.phone), meta_style))

    story.append(Paragraph("Quoted rate", section_style))
    # Table cells (below) take plain strings, not Paragraphs — reportlab
    # draws them directly without parsing markup, so escaping here would
    # show a literal "&amp;" instead of "&" in the PDF. Only Paragraph(...)
    # calls above need escaping, since those DO parse a markup subset.
    product_desc = quotation.product_type.item_code if quotation.product_type else "—"
    grade_size = " / ".join(filter(None, [
        quotation.grade or None,
        f"{quotation.size} mm" if quotation.size is not None else None,
    ])) or "—"
    table_data = [
        ["Product", "Grade / Size", "Rate (per kg)"],
        [product_desc, grade_size, f"Rs. {quotation.rate_per_kg}"],
    ]
    table = Table(table_data, colWidths=[60 * mm, 60 * mm, 50 * mm])
    table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#4e73df')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('FONTSIZE', (0, 0), (-1, -1), 10),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ('TOPPADDING', (0, 0), (-1, -1), 6),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#dddddd')),
    ]))
    story.append(table)
    story.append(Spacer(1, 6 * mm))

    story.append(Paragraph("Validity", section_style))
    story.append(Paragraph("This quotation is valid for 15 days from the date of issue.", styles['Normal']))

    story.append(Paragraph("Terms", section_style))
    story.append(Paragraph(
        "Prices are ex-works and exclusive of applicable taxes unless stated otherwise. "
        "Payment and delivery terms as mutually agreed at the time of order confirmation. "
        "Rates are subject to change without prior notice after the validity period above.",
        terms_style,
    ))

    doc.build(story)
    return buffer.getvalue()
