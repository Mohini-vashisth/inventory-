"""The summary of an order that is attached to the order-confirmation email the customer gets when staff
confirm it: who ordered, and exactly what was asked for — product type, code, grade, width and thickness,
quantity, delivery form with its length or approximate weight, tolerances, requirements, and whether a
drawing and a purchase order came with it. Derived from the Order records every time and never stored,
like the quotation PDF."""
import io

from django.conf import settings
from django.utils import timezone
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from .pdf import ACCENT, BORDER, BOX_BG, CONTENT_WIDTH, DARK, MUTED, _esc


def _number(value):
    return format(value.normalize(), 'f') if value is not None else ''


def order_rows(order, show_order_number=True):
    """[(label, text)] for one order: every detail the order page shows, with a dash where nothing was
    entered, so the document is the complete record and a blank is visibly blank."""
    delivery = order.delivery_detail_text() if order.delivery_form else ''
    rows = [
        ('Order no.', f"ORD-{order.order_no:04d}" if show_order_number and order.order_no else ''),
        ('Status', order.get_status_display()),
        ('Product type', order.product_type_name()),
        ('Product code', order.product_type.item_code if order.product_type_id else 'To be assigned'),
        ('Grade', order.grade),
        ('Width', f"{_number(order.width)} mm" if order.width is not None else ''),
        ('Thickness', f"{_number(order.thickness)} mm" if order.thickness is not None else ''),
        ('Quantity', f"{_number(order.quantity)} kg" if order.quantity is not None else ''),
        ('Delivery form', delivery),
        ('Expected delivery', f"{order.delivery_date:%d %b %Y}" if order.delivery_date else ''),
        ('Frequency', order.get_frequency_display() if order.frequency else ''),
        ('Tolerances', '\n'.join(order.tolerance_lines())),
        ('Dimensions (typed)', order.drawing_dimensions),
        ('Mechanical properties', order.mechanical_properties),
        ('Processes', order.processes),
        ('End usage', order.end_usage),
        ('Mill make', order.mill_make),
        ('Notes', order.notes),
        ('Drawing', 'Attached' if order.drawing_file else 'Not attached'),
        ('Purchase order', 'Attached' if order.purchase_order else 'Not attached'),
    ]
    # Only the rows that make no sense are left out: the order number when it isn't being shown, and the
    # typed dimensions, which only older orders and staff-entered ones have.
    skipped = {'Dimensions (typed)'} if not order.drawing_dimensions.strip() else set()
    if not (show_order_number and order.order_no):
        skipped.add('Order no.')
    return [(label, (str(text).strip() or '—')) for label, text in rows if label not in skipped]


def generate_order_summary_pdf(orders, customer, quotation=None, placed_at=None, show_order_numbers=True, confirmed_at=None):
    """The complete order as PDF bytes, for the customer: what they ordered and that it is confirmed.
    `orders` are the orders to list; `confirmed_at` is stamped in the header when given. Order numbers are
    the ones at the moment of printing (they close up if an earlier order is later deleted)."""
    placed_at = timezone.localtime(placed_at or min((o.created_at for o in orders if o.created_at), default=timezone.now()))
    base = getSampleStyleSheet()
    styles = {
        'title': ParagraphStyle('Title', parent=base['Heading1'], fontSize=18, textColor=ACCENT, spaceAfter=0, leading=20),
        'company': ParagraphStyle('Company', parent=base['Normal'], fontSize=12, textColor=DARK, leading=14, alignment=2),
        'meta': ParagraphStyle('Meta', parent=base['Normal'], fontSize=9, textColor=MUTED, leading=12),
        'section': ParagraphStyle('Section', parent=base['Heading3'], fontSize=10.5, textColor=ACCENT, spaceBefore=4 * mm, spaceAfter=1.5 * mm, leading=13),
        'label': ParagraphStyle('Label', parent=base['Normal'], fontSize=8, textColor=MUTED, leading=10),
        'value': ParagraphStyle('Value', parent=base['Normal'], fontSize=9.5, textColor=DARK, leading=12.5),
        'footer': ParagraphStyle('Footer', parent=base['Normal'], fontSize=7.5, textColor=MUTED, leading=10),
    }

    story = []
    header = Table([[Paragraph('ORDER SUMMARY', styles['title']), Paragraph(f"<b>{_esc(settings.COMPANY_NAME or 'Company')}</b>", styles['company'])]],
                   colWidths=[CONTENT_WIDTH * 0.6, CONTENT_WIDTH * 0.4])
    header.setStyle(TableStyle([('VALIGN', (0, 0), (-1, -1), 'MIDDLE'), ('LEFTPADDING', (0, 0), (-1, -1), 0), ('RIGHTPADDING', (0, 0), (-1, -1), 0)]))
    story += [header, Spacer(1, 2 * mm)]

    numbers = ', '.join(f"ORD-{o.order_no:04d}" for o in orders if o.order_no) if show_order_numbers else ''
    meta = [f"Order placed on {placed_at:%d %b %Y, %H:%M}"]
    if confirmed_at is not None:
        meta.append(f"Confirmed on {timezone.localtime(confirmed_at):%d %b %Y, %H:%M}")
    meta.append(f"{len(orders)} item{'s' if len(orders) != 1 else ''}")
    if numbers:
        meta.append(f"Order no.: {numbers}")
    if quotation is not None:
        meta.append(f"Against quotation {quotation.formatted_no()}")
    story.append(Paragraph(_esc(' · '.join(meta)), styles['meta']))

    source = next((o.source_query for o in orders if o.source_query_id), None)   # what they told us on WhatsApp
    customer_rows = [('Customer', customer.name), ('Email', customer.email), ('Phone', customer.phone),
                     ('GST number', source.gst_number if source else ''), ('GST address', source.gst_address if source else '')]
    customer_table = Table(
        [[Paragraph(label, styles['label']), Paragraph(_esc(text), styles['value'])] for label, text in customer_rows if text],
        colWidths=[28 * mm, CONTENT_WIDTH - 28 * mm])
    customer_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), BOX_BG), ('LEFTPADDING', (0, 0), (-1, -1), 7),
        ('TOPPADDING', (0, 0), (-1, -1), 3), ('BOTTOMPADDING', (0, 0), (-1, -1), 3), ('VALIGN', (0, 0), (-1, -1), 'TOP')]))
    story += [Spacer(1, 4 * mm), customer_table]

    for number, order in enumerate(orders, start=1):
        spec = order.spec_text()
        title = Paragraph(f"Item {number}" + (f" — {_esc(spec)}" if spec else ''), styles['section'])
        table = Table(
            [[Paragraph(label, styles['label']), Paragraph(_esc(text).replace('\n', '<br/>'), styles['value'])]
             for label, text in order_rows(order, show_order_numbers)],
            colWidths=[38 * mm, CONTENT_WIDTH - 38 * mm])
        table.setStyle(TableStyle([
            ('LINEBELOW', (0, 0), (-1, -1), 0.4, BORDER), ('LEFTPADDING', (0, 0), (-1, -1), 4),
            ('TOPPADDING', (0, 0), (-1, -1), 3), ('BOTTOMPADDING', (0, 0), (-1, -1), 3), ('VALIGN', (0, 0), (-1, -1), 'TOP')]))
        story.append(KeepTogether([title, table]))

    story += [Spacer(1, 6 * mm), Paragraph(
        "This summary was generated automatically from your order. Please tell us if anything here needs correcting.",
        styles['footer'])]

    buffer = io.BytesIO()
    SimpleDocTemplate(buffer, pagesize=A4, topMargin=14 * mm, bottomMargin=12 * mm, leftMargin=20 * mm, rightMargin=20 * mm,
                      title=f"Order summary — {customer.name}").build(story)
    return buffer.getvalue()
