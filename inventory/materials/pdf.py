"""Generates the official quotation PDF. Entirely derived from a Quotation
record's own fields (and its line_items), never stored on disk — regenerated
fresh every time it's needed (attached to the Send Quote email, or
re-downloaded later), the same "derive it, don't persist it" approach
coil_tag.html already uses for QR codes.

Content mirrors the client's real, existing (previously non-app) quotation
format — company letterhead, Quote No./Ref No./Rev No., a "Quotation by/to"
pair, an itemized table with HSN/SAC/GST%/discount/tool cost/MOQ, a
CGST/SGST/IGST total breakdown, numbered Terms & Conditions, and an
amount-in-words line — restyled to this app's own accent-blue look instead
of copying the reference's exact colors/fonts. Deliberately compact (small
fonts, tight padding, Terms and Totals side by side rather than stacked) so
a normal 1-3 line item quote fits on a single page — the reference itself
was one page, and a quotation that spills onto a second page just to show
a few line items reads as sloppy, not thorough."""
import io
from xml.sax.saxutils import escape

from django.conf import settings
from django.utils import timezone
from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
)

ACCENT = colors.HexColor('#4e73df')
BOX_BG = colors.HexColor('#EFF2FF')
MUTED = colors.HexColor('#6b7280')
DARK = colors.HexColor('#1a1a1a')
BORDER = colors.HexColor('#dddddd')

CONTENT_WIDTH = 170 * mm  # A4 width minus 20mm margins on each side
COL_WIDTH = 83 * mm       # a two-column row: COL_WIDTH, 4mm gap, COL_WIDTH


def _esc(value):
    """Any text bound for a Paragraph must be escaped — Paragraph parses a
    real markup subset, and some of this text (customer name, grade, terms)
    can originate from a raw WhatsApp reply or free-typed staff input with
    no HTML-safety check. Plain Table cell strings elsewhere don't need
    this (reportlab draws those directly, without parsing markup)."""
    return escape(str(value)) if value else ''


def _info_box(rows, styles):
    """A label/value card like the reference's boxed 'Quotation by/to'
    panels — one row per field. Rows with a falsy value are skipped."""
    data = [
        [Paragraph(f"<font color='#6b7280' size=7.5>{label}</font>", styles['box_label']), value_flowable]
        for label, value_flowable in rows if value_flowable is not None
    ]
    box = Table(data, colWidths=[22 * mm, COL_WIDTH - 22 * mm])
    box.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), BOX_BG),
        ('ROUNDEDCORNERS', [6, 6, 6, 6]),
        ('LEFTPADDING', (0, 0), (-1, -1), 7),
        ('RIGHTPADDING', (0, 0), (-1, -1), 7),
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
    ]))
    return box


def _fmt(amount):
    return f"{amount:,.2f}"


def generate_quotation_pdf(quotation):
    """Returns the rendered PDF as bytes."""
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4,
        topMargin=13 * mm, bottomMargin=12 * mm, leftMargin=20 * mm, rightMargin=20 * mm,
    )
    base = getSampleStyleSheet()
    styles = {
        'title':      ParagraphStyle('Title', parent=base['Heading1'], fontSize=18, textColor=ACCENT, spaceAfter=0, leading=20),
        'meta_label': ParagraphStyle('MetaLabel', parent=base['Normal'], fontSize=7.5, textColor=MUTED, leading=9),
        'meta_value': ParagraphStyle('MetaValue', parent=base['Normal'], fontSize=8.5, textColor=DARK, leading=10),
        'logo_name':  ParagraphStyle('LogoName', parent=base['Normal'], fontSize=13, textColor=DARK, leading=15),
        'logo_letter': ParagraphStyle('LogoLetter', parent=base['Normal'], fontSize=13, textColor=colors.white, alignment=1),
        'box_label':  ParagraphStyle('BoxLabel', parent=base['Normal'], fontSize=7.5, textColor=MUTED, leading=10),
        'box_value':  ParagraphStyle('BoxValue', parent=base['Normal'], fontSize=9.5, textColor=DARK, leading=13),
        'intro':      ParagraphStyle('Intro', parent=base['Normal'], fontSize=8.5, textColor=MUTED, leading=11, spaceAfter=0),
        'th':         ParagraphStyle('TH', parent=base['Normal'], fontSize=7.5, textColor=colors.white, leading=9),
        'td':         ParagraphStyle('TD', parent=base['Normal'], fontSize=8, textColor=DARK, leading=10.5),
        'section':    ParagraphStyle('Section', parent=base['Heading3'], fontSize=10, textColor=ACCENT, spaceBefore=0, spaceAfter=1.5 * mm, leading=12),
        'terms':      ParagraphStyle('Terms', parent=base['Normal'], fontSize=8, textColor=MUTED, leading=12, spaceAfter=2),
        'totals_label': ParagraphStyle('TotalsLabel', parent=base['Normal'], fontSize=8, textColor=MUTED, alignment=TA_RIGHT, leading=10),
        'totals_value': ParagraphStyle('TotalsValue', parent=base['Normal'], fontSize=8, textColor=DARK, alignment=TA_RIGHT, leading=10),
        'total_label': ParagraphStyle('TotalLabel', parent=base['Normal'], fontSize=10, textColor=ACCENT, alignment=TA_RIGHT, leading=13),
        'total_value': ParagraphStyle('TotalValue', parent=base['Normal'], fontSize=12, textColor=ACCENT, alignment=TA_RIGHT, leading=14),
        'words_label': ParagraphStyle('WordsLabel', parent=base['Normal'], fontSize=7.5, textColor=MUTED, leading=9),
        'words':      ParagraphStyle('Words', parent=base['Normal'], fontSize=8.5, textColor=DARK, leading=11),
        'closing':    ParagraphStyle('Closing', parent=base['Normal'], fontSize=8, textColor=MUTED, leading=11),
        'sig':        ParagraphStyle('Sig', parent=base['Normal'], fontSize=8.5, textColor=DARK, alignment=TA_RIGHT, leading=11),
        'footer':     ParagraphStyle('Footer', parent=base['Normal'], fontSize=7, textColor=MUTED, alignment=TA_RIGHT),
    }

    story = []
    company_name = settings.COMPANY_NAME or 'Company'

    # ── Header: quote title + meta (Quote No./Date/Ref/Rev) on the left,
    # a company "logo" mark + name on the right — full contact details
    # live once, in the "Quotation by" box below, not duplicated here.
    meta_rows = [["Quote No.", quotation.formatted_no(), "Date", timezone.localtime(quotation.created_at).strftime('%d/%m/%Y')]]
    if quotation.ref_no or quotation.rev_no:
        rev_date = quotation.rev_date.strftime('%d/%m/%Y') if quotation.rev_date else ''
        meta_rows.append([
            "Ref. No.", quotation.ref_no or '—',
            "Rev.", f"{quotation.rev_no} {rev_date}".strip(),
        ])
    meta_table_data = [
        [Paragraph(f"<font color='#6b7280' size=7.5>{a}</font>", styles['meta_label']), Paragraph(f"<b>{_esc(b)}</b>", styles['meta_value']),
         Paragraph(f"<font color='#6b7280' size=7.5>{c}</font>", styles['meta_label']), Paragraph(f"<b>{_esc(d)}</b>", styles['meta_value'])]
        for a, b, c, d in meta_rows
    ]
    meta_table = Table(meta_table_data, colWidths=[16 * mm, 24 * mm, 12 * mm, 24 * mm])
    meta_table.setStyle(TableStyle([('TOPPADDING', (0, 0), (-1, -1), 1), ('BOTTOMPADDING', (0, 0), (-1, -1), 1)]))

    left_header = [Paragraph("Quotation", styles['title']), Spacer(1, 1.5 * mm), meta_table]

    letter_mark = Table([[Paragraph(f"<b>{_esc(company_name[0].upper())}</b>", styles['logo_letter'])]],
                         colWidths=[10 * mm], rowHeights=[10 * mm])
    letter_mark.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), ACCENT),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('ROUNDEDCORNERS', [3, 3, 3, 3]),
    ]))
    logo_row = Table([[letter_mark, Paragraph(f"<b>{_esc(company_name)}</b>", styles['logo_name'])]],
                      colWidths=[12 * mm, 58 * mm])
    logo_row.setStyle(TableStyle([('VALIGN', (0, 0), (-1, -1), 'MIDDLE'), ('ALIGN', (1, 0), (1, 0), 'RIGHT')]))

    header = Table([[left_header, [logo_row]]], colWidths=[85 * mm, 85 * mm])
    header.setStyle(TableStyle([('VALIGN', (0, 0), (-1, -1), 'TOP')]))
    story.append(header)
    story.append(Spacer(1, 5 * mm))

    # ── Quotation by / Quotation to boxes — each field gets its own row
    # (cramming Phone/Email onto one "Contact" line read poorly), so the
    # two boxes won't always match in row count — that's fine, they're
    # still visually paired by matching width/style, just not forced level.
    by_rows = [("Quotation by", Paragraph(f"<b>{_esc(company_name)}</b>", styles['box_value']))]
    if settings.COMPANY_ADDRESS:
        by_rows.append(("Address", Paragraph(_esc(settings.COMPANY_ADDRESS), styles['box_value'])))
    if settings.COMPANY_PHONE:
        by_rows.append(("Phone", Paragraph(_esc(settings.COMPANY_PHONE), styles['box_value'])))
    if settings.COMPANY_EMAIL:
        by_rows.append(("Email", Paragraph(_esc(settings.COMPANY_EMAIL), styles['box_value'])))
    if settings.COMPANY_WEBSITE:
        by_rows.append(("Website", Paragraph(_esc(settings.COMPANY_WEBSITE), styles['box_value'])))
    if settings.COMPANY_GST:
        by_rows.append(("GSTIN", Paragraph(_esc(settings.COMPANY_GST), styles['box_value'])))
    if quotation.sales_person:
        by_rows.append(("Sales Person", Paragraph(_esc(quotation.sales_person), styles['box_value'])))
    by_box = _info_box(by_rows, styles)

    customer = quotation.customer
    to_rows = [("Quotation to", Paragraph(f"<b>M/s. {_esc(customer.name)}</b>", styles['box_value']))]
    if quotation.customer_address:
        to_rows.append(("Address", Paragraph(_esc(quotation.customer_address), styles['box_value'])))
    if customer.email:
        to_rows.append(("Email", Paragraph(_esc(customer.email), styles['box_value'])))
    if customer.phone:
        to_rows.append(("Phone", Paragraph(_esc(customer.phone), styles['box_value'])))
    if quotation.kind_attn:
        to_rows.append(("Kind Attn", Paragraph(_esc(quotation.kind_attn), styles['box_value'])))
    to_box = _info_box(to_rows, styles)

    boxes = Table([[by_box, '', to_box]], colWidths=[COL_WIDTH, 4 * mm, COL_WIDTH])
    boxes.setStyle(TableStyle([('VALIGN', (0, 0), (-1, -1), 'TOP')]))
    story.append(boxes)
    story.append(Spacer(1, 4 * mm))

    if quotation.subject:
        story.append(Paragraph(f"<b>Subject:</b> {_esc(quotation.subject)}", styles['intro']))
        story.append(Spacer(1, 1.5 * mm))
    story.append(Paragraph("We are pleased to submit our offer as follows:", styles['intro']))
    story.append(Spacer(1, 4 * mm))

    # ── Item table
    header_row = ["Sr.", "Description", "HSN/SAC", "Qty", "Unit", "Rate", "Disc %", "GST %", "Tool Cost", "MOQ", "Amount"]
    table_data = [[Paragraph(h, styles['th']) for h in header_row]]
    for item in quotation.line_items.all():
        desc_bits = " / ".join(filter(None, [_esc(item.grade), f"{item.size} mm" if item.size is not None else None]))
        description = Paragraph(
            f"{_esc(item.description)}" + (f"<br/><font color='#6b7280'>{desc_bits}</font>" if desc_bits else ''),
            styles['td'],
        )
        table_data.append([
            Paragraph(str(item.order), styles['td']),
            description,
            Paragraph(_esc(item.hsn_sac) or '—', styles['td']),
            Paragraph(f"{item.quantity:g}", styles['td']),
            Paragraph(_esc(item.unit), styles['td']),
            Paragraph(_fmt(item.rate_per_kg), styles['td']),
            Paragraph(f"{item.discount_pct:g}", styles['td']),
            Paragraph(f"{item.gst_pct:g}", styles['td']),
            Paragraph(_fmt(item.tool_cost), styles['td']),
            Paragraph(f"{item.moq:g}" if item.moq else '—', styles['td']),
            Paragraph(_fmt(item.amount()), styles['td']),
        ])

    item_table = Table(
        table_data,
        colWidths=[7 * mm, 56 * mm, 14 * mm, 12 * mm, 10 * mm, 14 * mm, 10 * mm, 10 * mm, 12 * mm, 10 * mm, 15 * mm],
        repeatRows=1,
    )
    item_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), ACCENT),
        ('TOPPADDING', (0, 0), (-1, 0), 5),
        ('BOTTOMPADDING', (0, 0), (-1, 0), 5),
        ('TOPPADDING', (0, 1), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 1), (-1, -1), 4),
        ('LEFTPADDING', (0, 0), (-1, -1), 4),
        ('RIGHTPADDING', (0, 0), (-1, -1), 4),
        ('ALIGN', (2, 0), (-1, -1), 'RIGHT'),
        ('ALIGN', (0, 0), (0, -1), 'CENTER'),
        ('LINEBELOW', (0, 0), (-1, -2), 0.5, BORDER),
        ('LINEBELOW', (0, -1), (-1, -1), 1, ACCENT),
    ]))
    story.append(item_table)
    story.append(Spacer(1, 5 * mm))

    # ── Terms and Conditions (left) + Totals (right), side by side — two
    # blocks that used to stack sequentially now share one row, which is
    # what actually keeps a normal quote to a single page.
    terms_flowables = [Paragraph("Terms and Conditions", styles['section'])]
    terms = [
        ("Price Basis", quotation.price_basis),
        ("GST", quotation.gst_terms),
        ("Insurance", quotation.insurance_terms),
        ("Freight", quotation.freight_terms),
        ("Payment Terms", quotation.payment_terms),
        ("Delivery", quotation.delivery_terms),
        ("Validity", quotation.validity_terms),
    ]
    for i, (label, value) in enumerate(terms, start=1):
        if value:
            terms_flowables.append(Paragraph(f"{i}. <b>{_esc(label)}:</b> {_esc(value)}", styles['terms']))

    totals_rows = [
        ("Subtotal", quotation.subtotal()),
        ("Tool Cost", quotation.tool_cost_total()),
        ("P&F Amount", quotation.pf_amount),
        ("Freight / Courier", quotation.freight_amount),
        ("CGST", quotation.cgst()),
        ("SGST", quotation.sgst()),
        ("IGST", quotation.igst()),
    ]
    totals_data = [
        [Paragraph(label, styles['totals_label']), Paragraph(_fmt(value), styles['totals_value'])]
        for label, value in totals_rows
    ]
    totals_data.append([
        Paragraph("Total Amount", styles['total_label']),
        Paragraph(f"Rs. {_fmt(quotation.total_amount())}", styles['total_value']),
    ])
    totals_table = Table(totals_data, colWidths=[42 * mm, 41 * mm])
    totals_table.setStyle(TableStyle([
        ('TOPPADDING', (0, 0), (-1, -2), 2.5),
        ('BOTTOMPADDING', (0, 0), (-1, -2), 2.5),
        ('LINEABOVE', (0, -1), (-1, -1), 0.75, ACCENT),
        ('TOPPADDING', (0, -1), (-1, -1), 3),
    ]))

    columns_row = Table([[terms_flowables, totals_table]], colWidths=[COL_WIDTH, COL_WIDTH])
    columns_row.setStyle(TableStyle([('VALIGN', (0, 0), (-1, -1), 'TOP'), ('LEFTPADDING', (1, 0), (1, 0), 4 * mm)]))
    story.append(columns_row)
    story.append(Spacer(1, 5 * mm))

    story.append(Paragraph(
        "Hope this is in line with your requirement — please reach out for any clarification.",
        styles['closing'],
    ))
    story.append(Spacer(1, 5 * mm))

    # ── Amount in words + signature, side by side
    words_block = [
        Paragraph("Amount in (Words)", styles['words_label']),
        Paragraph(f"<b>{_esc(quotation.amount_in_words())}</b>", styles['words']),
    ]
    sig_block = [
        Paragraph(f"For {_esc(company_name)}", styles['sig']),
        Spacer(1, 9 * mm),
        Paragraph("(Authorized Signatory)", styles['sig']),
    ]
    words_row = Table([[words_block, sig_block]], colWidths=[COL_WIDTH, COL_WIDTH])
    words_row.setStyle(TableStyle([('VALIGN', (0, 0), (-1, -1), 'BOTTOM')]))
    story.append(words_row)

    story.append(Spacer(1, 5 * mm))
    story.append(Paragraph(
        f"Date: {timezone.localtime(quotation.created_at).strftime('%d/%m/%Y')}",
        styles['footer'],
    ))

    doc.build(story)
    return buffer.getvalue()
