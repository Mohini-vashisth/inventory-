"""Orders: the staff dashboard and actions, plus the customer-facing quote form."""

import logging
import uuid

from django.conf import settings
from django.core.mail import EmailMessage
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.core.files.base import ContentFile
from django.db import transaction
from django.db.models import Sum
from django.http import JsonResponse

from ..models import ProductType, Customer, Query, Order
from ..forms import OrderForm, OrderItemFormSet
from ..decorators import redirect_to_admin_login, staff_required
from .common import _first_form_error, _first_formset_error, _match_product_type
from .quotations import _public_quote_base_url

logger = logging.getLogger(__name__)


def _send_order_confirmation_email(order):
    """Emails the customer that staff have confirmed their order, with what was ordered:
    product type, grade, width x thickness, quantity, product code and delivery details.
    Returns 'sent', 'no_email' (nothing on file for the customer), 'not_configured' (no
    mail settings) or 'failed' — never raises, so confirming an order can't be blocked by
    mail trouble (a failure is logged)."""
    customer = order.customer
    if not customer.email:
        return 'no_email'
    if not settings.EMAIL_HOST_USER:
        return 'not_configured'
    lines = [f"  {order.spec_text() or 'Item'}" + (f" — {order.quantity:.0f} kg" if order.quantity is not None else '')]
    details = []
    if order.product_type_id:
        details.append(f"Product code: {order.product_type.item_code}")
    if order.delivery_form:
        details.append(f"Delivery form: {order.get_delivery_form_display()}")
    if order.delivery_date:
        details.append(f"Expected delivery: {order.delivery_date:%d %b %Y}")
    lines += [f"  {detail}" for detail in details]
    contact = ' or '.join(part for part in (settings.COMPANY_PHONE, settings.COMPANY_EMAIL) if part)
    try:
        EmailMessage(
            subject=f"Order confirmed — {settings.COMPANY_NAME}",
            body=(
                f"Dear {customer.name},\n\n"
                f"Your order has been confirmed. Production will start shortly.\n\n"
                f"Order details:\n" + '\n'.join(lines) + "\n\n"
                + (f"If anything needs correcting, please contact us at {contact}.\n\n" if contact else '')
                + f"Regards,\n{settings.COMPANY_NAME}"
            ),
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=[customer.email],
        ).send()
        return 'sent'
    except Exception:
        logger.warning("Order confirmation email to %s failed", customer.email, exc_info=True)
        return 'failed'


@staff_required(on_denied=redirect_to_admin_login)
def order_dashboard(request):

    orders = (Order.objects
              .select_related('customer', 'product_type__category')
              .annotate(weight_cut=Sum('coil_picks__weight_allocated'))
              .order_by('-created_at'))
    customers = Customer.objects.order_by('name')
    product_types = ProductType.objects.order_by('item_code')
    product_type_data = {
        str(pt.pk): {'grade': pt.grade}
        for pt in product_types
    }
    error = None

    if request.method == 'POST':
        customer_name = request.POST.get('name', '').strip()
        form = OrderForm(request.POST)

        if not customer_name:
            error = "Company name is required."
        elif not form.is_valid():
            error = _first_form_error(form)
        else:
            customer, _ = Customer.objects.get_or_create(name=customer_name)
            email = request.POST.get('email', '').strip()
            phone = request.POST.get('phone', '').strip()
            if email or phone:
                if email:
                    customer.email = email
                if phone:
                    customer.phone = phone
                customer.save(update_fields=['email', 'phone'])

            order = form.save(commit=False)
            order.customer = customer
            order.status = 'confirmed'
            order.save()
            return redirect('order_dashboard')

    # Persistent low-stock indicator for orders already committed to
    # production — a one-time warning at confirm time can get missed, so
    # this stays visible for as long as it's actually true. Skipped for
    # pending/completed/cancelled orders where it isn't actionable.
    #
    # available_raw_material_output() depends only on product_type, not on
    # the individual order, so it's cached per product_type here — several
    # orders sharing one catalog item (the common case) reuse one result
    # instead of each re-running its own set of aggregate queries.
    raw_material_output_cache = {}
    for order in orders:
        if order.status not in ('confirmed', 'in_production'):
            order.low_stock = False
            continue
        if order.product_type_id not in raw_material_output_cache:
            raw_material_output_cache[order.product_type_id] = order.available_raw_material_output()
        available = raw_material_output_cache[order.product_type_id]
        order.low_stock = available is not None and available < order.quantity

    return render(request, 'materials/order_dashboard.html', {
        'orders': orders,
        'customers': customers,
        'product_types': product_types,
        'product_type_data': product_type_data,
        'error': error,
        'post': request.POST if error else {},
        'quote_base_url': _public_quote_base_url(request),
    })


@staff_required(on_denied=lambda request: JsonResponse([], safe=False))
def customer_autocomplete(request):
    q = request.GET.get('q', '').strip()
    if not q:
        return JsonResponse([], safe=False)
    results = list(
        Customer.objects.filter(name__icontains=q)
        .values('name', 'email', 'phone', 'quote_token')[:8]
    )
    for r in results:
        r['quote_token'] = str(r['quote_token'])
    return JsonResponse(results, safe=False)


@staff_required
def order_confirm(request, pk):
    if request.method != 'POST':
        return redirect('order_dashboard')
    order = get_object_or_404(Order, pk=pk)
    if order.status != 'pending':
        return redirect('order_dashboard')
    if not order.product_type_id:
        messages.error(request, f"ORD-{order.order_no:04d} cannot be confirmed without a product type. Edit the order to assign one.")
        return redirect('order_dashboard')
    order.status = 'confirmed'
    order.save(update_fields=['status'])
    messages.success(request, f"ORD-{order.order_no:04d} confirmed.")
    emailed = _send_order_confirmation_email(order)
    if emailed == 'sent':
        messages.success(request, f"Confirmation email sent to {order.customer.email}.")
    elif emailed == 'no_email':
        messages.warning(request, f"No email on file for {order.customer.name}, so no confirmation email was sent.")
    else:
        messages.warning(request, "The confirmation email could not be sent" + (" (email is not configured)." if emailed == 'not_configured' else " — check the mail settings."))
    if order.has_sufficient_raw_material() is False:
        available = order.available_raw_material_output()
        messages.warning(
            request,
            f"⚠️ Raw material for ORD-{order.order_no:04d} looks short — only "
            f"{available:.0f} kg of the {order.quantity:.0f} kg needed is currently in stock. "
            f"Consider ordering more."
        )
    return redirect('order_dashboard')


@staff_required
def order_dispatch(request, pk):
    if request.method != 'POST':
        return redirect('order_dashboard')
    order = get_object_or_404(Order, pk=pk)
    if order.status == 'in_production':
        order.status = 'completed'
        order.save(update_fields=['status'])
        messages.success(request, f"ORD-{order.order_no:04d} marked as dispatched.")
    return redirect('order_dashboard')


@staff_required
def order_reject(request, pk):
    if request.method != 'POST':
        return redirect('order_dashboard')
    order = get_object_or_404(Order, pk=pk)
    if order.status != 'pending':
        return redirect('order_dashboard')
    order.status = 'cancelled'
    order.save(update_fields=['status'])
    return redirect('order_dashboard')


def quote_form(request, token):
    """The customer-facing order form, reached from the link in the quote email.

    The customer never chooses a product code. When a quotation has been sent
    to them, each quoted item gets its own block with its product code, grade
    and width and thickness shown but locked — the server reads those from the quotation line
    item, never from the posted data — and submitting creates one order per
    quoted item. With no quotation to read from (an old link sent before
    quotes existed), the form falls back to a single free-form order, and the
    product code is matched from the grade they type."""
    customer = get_object_or_404(Customer, quote_token=token)
    # The query (if any) that led to this quote being sent.
    query = Query.objects.filter(customer=customer, status='quote_sent').order_by('-created_at').first()
    quotation = customer.quotations.filter(status='sent').order_by('-quotation_no').first()
    items = list(quotation.line_items.select_related('product_type__category', 'category').order_by('order')) if quotation else []
    error = None

    def _finish(created_orders_query):
        if created_orders_query:
            query.status = 'converted'
            query.save(update_fields=['status'])
        # Invalidate this link — regenerate token so the URL becomes a 404
        customer.quote_token = uuid.uuid4()
        customer.save(update_fields=['quote_token'])
        return render(request, 'materials/quote_submitted.html', {'customer': customer})

    # What the customer already told us on WhatsApp, so they aren't asked twice. The
    # query keeps delivery form as "Coil"/"Bar"; the order's choices are lower-case.
    from_query = {'end_usage': query.end_use, 'delivery_form': query.delivery_form.lower()} if query else {}

    if items:
        initial = [{'line_item': item.pk, 'quantity': item.quantity, **from_query} for item in items]
        formset = (OrderItemFormSet(request.POST, prefix='item') if request.method == 'POST'
                   else OrderItemFormSet(initial=initial, prefix='item'))
        if request.method == 'POST':
            if not formset.is_valid():
                error = _first_formset_error(formset)
            elif [f.cleaned_data.get('line_item') for f in formset.forms] != [item.pk for item in items]:
                error = "This order form is out of date — please reload the page and try again."
            else:
                po = request.FILES.get('purchase_order')
                po_bytes = po.read() if po else None
                with transaction.atomic():
                    for item_form, item in zip(formset.forms, items):
                        order = item_form.save(commit=False)
                        order.customer = customer
                        order.source_query = query
                        order.product_type = item.product_type or _match_product_type(item.grade, item.category)
                        order.grade = item.grade
                        order.width = item.width
                        order.thickness = item.thickness
                        order.status = 'pending'
                        order.save()
                        if po_bytes is not None:
                            order.purchase_order.save(po.name, ContentFile(po_bytes), save=True)
                    return _finish(query)
        return render(request, 'materials/quote_form.html', {
            'customer': customer,
            'item_forms': list(zip(formset.forms, items)),
            'formset': formset,
            'error': error,
        })

    if request.method == 'POST':
        form = OrderForm(request.POST, request.FILES)
        if not form.is_valid():
            error = _first_form_error(form)
        else:
            order = form.save(commit=False)
            order.customer = customer
            order.source_query = query
            order.product_type = _match_product_type(order.grade)
            order.status = 'pending'
            order.save()
            return _finish(query)

    initial = {}
    if query and not error:
        initial = {
            **from_query,
            'grade': query.grade,
            'width': str(query.width) if query.width is not None else '',
            'thickness': str(query.thickness) if query.thickness is not None else '',
            'quantity': str(query.quantity) if query.quantity is not None else '',
            'notes': query.notes,
        }

    return render(request, 'materials/quote_form.html', {
        'customer': customer,
        'error': error,
        'post': request.POST if error else initial,
    })