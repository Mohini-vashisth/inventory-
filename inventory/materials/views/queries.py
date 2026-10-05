"""Query dashboard: logging and editing inbound sales inquiries (pre-quote leads)."""

from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from decimal import InvalidOperation
from django.core.exceptions import ValidationError

from ..models import ProductType, Query
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
        elif source not in dict(Query.SOURCE_CHOICES):
            error = "Please select where this query came from."
        elif Query.objects.filter(contact_phone=contact_phone).exclude(status__in=['converted', 'not_interested']).exists():
            # Logging the same number twice would fire a second template
            # message and create a duplicate Query that the customer's
            # replies (routed to the newest match) would never reach —
            # silently orphaning it in 'new' status forever.
            error = f"A query for {contact_phone} is already in progress — check the table below."
        else:
            Query.objects.create(source=source, contact_phone=contact_phone)
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
        'error': error,
        'post': request.POST if error else {},
        'quote_base_url': _public_quote_base_url(request),
    })


@staff_required
def query_not_interested(request, pk):
    if request.method != 'POST':
        return redirect('query_dashboard')
    query = get_object_or_404(Query, pk=pk)
    query.status = 'not_interested'
    query.save(update_fields=['status'])
    return redirect('query_dashboard')


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
        raw_size = request.POST.get('size') or None
        raw_quantity = request.POST.get('quantity') or None
        try:
            query.company_name = request.POST.get('company_name', '').strip()
            query.contact_phone = whatsapp._normalize_phone(request.POST.get('contact_phone', ''))
            query.contact_email = request.POST.get('contact_email', '').strip()
            query.product_type_id = request.POST.get('product_type') or None
            query.grade = request.POST.get('grade', '').strip()
            query.size = raw_size
            query.quantity = raw_quantity
            query.notes = request.POST.get('notes', '').strip()
            for field, _label in Query.INTAKE_TEXT_FIELDS:
                setattr(query, field, request.POST.get(field, '').strip())
            query.gst_number = query.gst_number.replace(' ', '').upper()
            query.full_clean(exclude=['source', 'status', 'customer'])
        except ValidationError as e:
            error = e.messages[0] if e.messages else "Check the values entered."
        except (InvalidOperation, ValueError):
            error = "Check that size and quantity are valid numbers."
        else:
            query.save(update_fields=[
                'company_name', 'contact_phone', 'contact_email',
                'product_type', 'grade', 'size', 'quantity', 'notes',
                *[field for field, _label in Query.INTAKE_TEXT_FIELDS],
            ])
            return redirect('query_dashboard')

    return render(request, 'materials/query_edit.html', {
        'query': query,
        'product_types': product_types,
        'intake_fields': [(f, label, getattr(query, f)) for f, label in Query.INTAKE_TEXT_FIELDS],
        'error': error,
    })
