"""Query dashboard: logging and editing inbound sales inquiries (pre-quote leads)."""

from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from decimal import InvalidOperation
from django.core.exceptions import ValidationError

from ..models import GradeOption, ProductCategory, ProductType, Query
from ..product_codes import canonical_grade, grade_key
from .common import _match_product_type
from ..decorators import redirect_to_admin_login, staff_required
from .quotations import _public_quote_base_url
from . import whatsapp


@staff_required(on_denied=redirect_to_admin_login)
def query_dashboard(request):
    """Unified log of inbound sales inquiries — phone calls, referrals,
    IndiaMART, WhatsApp — regardless of source, before any of them become a
    formal quote/order. Logging a query immediately kicks off the WhatsApp
    intake sequence (see whatsapp.py) — staff only ever type in
    a phone number here; everything else arrives via WhatsApp. Staff decide
    which ones to pursue via query_send_quote once that sequence completes."""

    queries = Query.objects.select_related('product_type', 'customer').prefetch_related('quotations').all()
    error = None

    if request.method == 'POST':
        source = request.POST.get('source', '')
        contact_phone = whatsapp._normalize_phone(request.POST.get('contact_phone', ''))

        if not contact_phone:
            error = "Phone number is required."
        elif len(contact_phone) < 10:
            # The phone field is pre-filled with "+91 " so staff don't have
            # to retype the country code — but that also means submitting
            # without adding the actual number normalizes to a short,
            # meaningless digit string ("91") instead of failing the
            # "required" check above.
            error = "That doesn't look like a complete phone number."
        elif source not in Query.MANUAL_SOURCES:
            error = "Please select where this query came from."
        elif Query.objects.filter(contact_phone=contact_phone).exclude(status__in=['converted', 'not_interested']).exists():
            # Logging the same number twice would fire a second template
            # message and create a duplicate Query that the customer's
            # replies (routed to the newest match) would never reach —
            # silently orphaning it in 'new' status forever.
            error = f"A query for {contact_phone} is already in progress — check the table below."
        else:
            # The follow-up fields are optional and only belong to their own source.
            extra = {}
            if source == 'referral':
                extra = {
                    'referrer_name': request.POST.get('referrer_name', '').strip(),
                    'referrer_phone': request.POST.get('referrer_phone', '').strip(),
                }
            elif source == 'other':
                extra = {'source_detail': request.POST.get('source_detail', '').strip()}
            new_query = Query(source=source, contact_phone=contact_phone, **extra)
            try:
                new_query.full_clean(exclude=['status', 'customer'])
            except ValidationError as e:
                error = e.messages[0]
            else:
                new_query.save()
                try:
                    whatsapp._send_whatsapp_template_message(
                        contact_phone, whatsapp.WHATSAPP_QUERY_INTAKE_TEMPLATE, language=whatsapp.WHATSAPP_QUERY_INTAKE_TEMPLATE_LANGUAGE,
                    )
                except whatsapp.WhatsAppSendError as e:
                    messages.warning(
                        request,
                        f"Query logged, but the WhatsApp intake message to {contact_phone} couldn't be "
                        f"sent automatically ({e}). Please reach out directly.",
                    )
                return redirect('query_dashboard')

    return render(request, 'materials/query_dashboard.html', {
        'queries': queries,
        'source_choices': Query.manual_source_choices(),
        'error': error,
        'post': request.POST if error else {},
        'quote_base_url': _public_quote_base_url(request),
    })


@staff_required
def query_clear_review(request, pk):
    """Staff have looked at a conversation the WhatsApp bot flagged (a reply that arrived out of
    order, or the 24-hour reply window closing) — clear the flag."""
    if request.method != 'POST':
        return redirect('query_detail', pk=pk)
    query = get_object_or_404(Query, pk=pk)
    query.needs_review = False
    query.review_note = ''
    query.save(update_fields=['needs_review', 'review_note'])
    return redirect('query_detail', pk=pk)


@staff_required
def query_not_interested(request, pk):
    if request.method != 'POST':
        return redirect('query_dashboard')
    query = get_object_or_404(Query, pk=pk)
    query.status = 'not_interested'
    query.save(update_fields=['status'])
    return redirect('query_dashboard')


@staff_required(on_denied=redirect_to_admin_login)
def query_detail(request, pk):
    """Everything known about one query on a single page — the dashboard only
    shows name and number, so this is where the rest lives. Read-only; edits
    go through query_edit, and the same status-dependent actions as the
    dashboard (send quote, copy link, ...) are offered here too."""
    query = get_object_or_404(Query.objects.select_related('product_type', 'customer'), pk=pk)
    return render(request, 'materials/query_detail.html', {
        'query': query,
        'history': query.quotation_history(),
        'quote_base_url': _public_quote_base_url(request),
    })


@staff_required
def query_edit(request, pk):
    """Fix a wrong/incomplete field on a query directly from the dashboard —
    a typo'd company name, a grade the WhatsApp bot mis-parsed, etc.
    Previously the only way to do this was Django admin. `source`, `status`,
    and `customer` are deliberately not editable here — status/customer
    changes go through query_send_quote/query_not_interested so they can't
    drift out of sync with what those actions actually did."""
    query = get_object_or_404(Query, pk=pk)
    product_types = ProductType.objects.order_by('item_code')
    error = None

    if request.method == 'POST':
        raw_width = request.POST.get('width') or None
        raw_thickness = request.POST.get('thickness') or None
        raw_quantity = request.POST.get('quantity') or None
        try:
            query.company_name = request.POST.get('company_name', '').strip()
            query.contact_phone = whatsapp._normalize_phone(request.POST.get('contact_phone', ''))
            query.contact_email = request.POST.get('contact_email', '').strip()
            query.product_type_id = request.POST.get('product_type') or None
            query.product_category_id = request.POST.get('product_category') or None
            query.grade = canonical_grade(request.POST.get('grade'))
            query.width = raw_width
            query.thickness = raw_thickness
            query.quantity = raw_quantity
            query.notes = request.POST.get('notes', '').strip()
            if query.source == 'referral':
                query.referrer_name = request.POST.get('referrer_name', '').strip()
                query.referrer_phone = request.POST.get('referrer_phone', '').strip()
            elif query.source == 'other':
                query.source_detail = request.POST.get('source_detail', '').strip()
            for field, _label in Query.INTAKE_TEXT_FIELDS:
                setattr(query, field, request.POST.get(field, '').strip())
            query.gst_number = query.gst_number.replace(' ', '').upper()
            query.full_clean(exclude=['source', 'status', 'customer'])
        except ValidationError as e:
            error = e.messages[0] if e.messages else "Check the values entered."
        except (InvalidOperation, ValueError):
            error = "Check that width, thickness and quantity are valid numbers."
        else:
            if not query.product_type_id:   # no code picked by hand: look it up from type + grade + size
                query.product_type = _match_product_type(query.grade, query.product_category)
            query.save(update_fields=[
                'company_name', 'contact_phone', 'contact_email',
                'product_type', 'product_category', 'grade', 'width', 'thickness', 'quantity', 'notes',
                'referrer_name', 'referrer_phone', 'source_detail',
                *[field for field, _label in Query.INTAKE_TEXT_FIELDS],
            ])
            return redirect('query_dashboard')

    return render(request, 'materials/query_edit.html', {
        'query': query,
        'product_types': product_types,
        'intake_fields': [(f, label, getattr(query, f)) for f, label in Query.INTAKE_TEXT_FIELDS],
        'delivery_choices': Query.DELIVERY_FORM_CHOICES,
        'categories': ProductCategory.objects.all(),
        # type + grade -> product code, for the form's live auto-match (same as the quote form)
        'product_code_map': [
            {'pk': pt.pk, 'category': pt.category_id, 'grade': grade_key(pt.grade)}
            for pt in ProductType.objects.all()
        ],
        'grade_options': list(GradeOption.objects.values_list('name', flat=True)),
        'error': error,
    })
