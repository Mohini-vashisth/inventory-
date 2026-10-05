"""Orders: the staff dashboard and actions, plus the customer-facing quote form."""

import uuid

from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.db.models import Sum
from django.http import JsonResponse

from ..models import ProductType, Customer, Query, Order
from ..forms import OrderForm
from ..decorators import redirect_to_admin_login, staff_required
from .common import _first_form_error
from .quotations import _public_quote_base_url


@staff_required(on_denied=redirect_to_admin_login)
def order_dashboard(request):

    orders = (Order.objects
              .select_related('customer', 'product_type')
              .annotate(weight_cut=Sum('coil_picks__weight_allocated'))
              .order_by('-created_at'))
    customers = Customer.objects.order_by('name')
    product_types = ProductType.objects.order_by('item_code')
    product_type_data = {
        str(pt.pk): {'grade': pt.grade, 'size': str(pt.size) if pt.size else ''}
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
    customer = get_object_or_404(Customer, quote_token=token)
    product_types = ProductType.objects.order_by('item_code')
    product_type_data = {
        str(pt.pk): {'grade': pt.grade, 'size': str(pt.size) if pt.size else ''}
        for pt in product_types
    }
    # The query (if any) that led to this quote being sent — its info
    # pre-fills the form below so the customer isn't re-typing what they
    # already told us on a call/referral/IndiaMART message.
    query = Query.objects.filter(customer=customer, status='quote_sent').order_by('-created_at').first()
    error = None

    if request.method == 'POST':
        form = OrderForm(request.POST, request.FILES)
        if not form.is_valid():
            error = _first_form_error(form)
        else:
            order = form.save(commit=False)
            order.customer = customer
            order.source_query = query
            order.status = 'pending'
            order.save()
            if query:
                query.status = 'converted'
                query.save(update_fields=['status'])
            # Invalidate this link — regenerate token so the URL becomes a 404
            customer.quote_token = uuid.uuid4()
            customer.save(update_fields=['quote_token'])
            return render(request, 'materials/quote_submitted.html', {'customer': customer})

    initial = {}
    if query and not error:
        initial = {
            'product_type': str(query.product_type_id) if query.product_type_id else '',
            'grade': query.grade,
            'size': str(query.size) if query.size is not None else '',
            'quantity': str(query.quantity) if query.quantity is not None else '',
            'notes': query.notes,
        }

    return render(request, 'materials/quote_form.html', {
        'customer': customer,
        'product_types': product_types,
        'product_type_data': product_type_data,
        'error': error,
        'post': request.POST if error else initial,
    })
