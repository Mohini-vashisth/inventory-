"""The company portal: one permanent link per company that has ordered. It shows that company's current and past
orders (a customer follows an order's status only where staff switched tracking on) and lets it repeat an earlier
order, changing only the quantity and the delivery date. A repeat arrives as a pending order for staff to confirm.
There is no way to order a new product here: a new product starts with a query.

Public like the quote form (reached through the Funnel path /portal), so the unguessable link is the only credential,
the pages load nothing from /static, and a company can only ever see its own orders."""

import datetime
import json
import os
from urllib.parse import quote

from django import forms
from django.conf import settings
from django.core.files.base import ContentFile
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.views.decorators.http import require_GET

from ..models import Customer, Order
from .whatsapp import portal_chat_ref

CURRENT = ('pending', 'confirmed', 'in_production')

# What a customer is told about an order whose tracking staff switched on.
TRACKED_STATUS = {
    'pending': 'Received. We will confirm it soon.',
    'confirmed': 'Confirmed',
    'in_production': 'In production',
    'completed': 'Completed',
    'cancelled': 'Cancelled',
}

# Copied from the earlier order when it is repeated; the quantity and delivery date are asked, nothing else changes.
REORDER_COPIED = [
    'product_code', 'product_type', 'grade', 'width', 'thickness', 'mill_make', 'mechanical_properties', 'processes',
    'end_usage', 'delivery_form', 'bar_length', 'length_tol_from', 'length_tol_to', 'coil_weight', 'frequency',
    'notes', 'drawing_dimensions', 'width_tol_from', 'width_tol_to', 'thickness_tol_from', 'thickness_tol_to',
    'other_tolerances',
]


def _branding(customer):
    """The header, footer and chat button every portal page shares: who we are, and a WhatsApp link that opens the
    chat with a first message naming the company (the number comes from WHATSAPP_CHAT_NUMBER; blank hides the button)."""
    chat = ''
    if settings.WHATSAPP_CHAT_NUMBER:
        chat = f"https://wa.me/{settings.WHATSAPP_CHAT_NUMBER}?text=" + quote(f"Hi, I'm from {customer.name}. Ref: {portal_chat_ref(customer)}")
    return {
        'brand': {'name': settings.COMPANY_NAME, 'address': settings.COMPANY_ADDRESS, 'phone': settings.COMPANY_PHONE,
                  'email': settings.COMPANY_EMAIL, 'gst': settings.COMPANY_GST, 'website': settings.COMPANY_WEBSITE},
        'chat_url': chat,
    }


def _company(token):
    """The company behind a portal link; a link whose company has never ordered (or was reset) is simply not found."""
    customer = get_object_or_404(Customer, portal_token=token)
    if not customer.orders.exists():
        raise Http404
    return customer


def _card(order):
    """What the portal shows for one order."""
    return {
        'order': order,
        'title': order.spec_text() or 'Order',
        'status': TRACKED_STATUS.get(order.status, '') if order.customer_can_track else '',
        'can_repeat': order.status != 'cancelled',
    }


def portal_home(request, token):
    customer = _company(token)
    orders = list(customer.orders.select_related('product_code__product_type', 'product_type').order_by('-created_at'))
    return render(request, 'materials/portal_home.html', {
        'customer': customer,
        'token': token,
        **_branding(customer),
        'current': [_card(o) for o in orders if o.status in CURRENT],
        'past': [_card(o) for o in orders if o.status == 'completed'],
    })


class ReorderForm(forms.Form):
    quantity = forms.DecimalField(max_digits=12, decimal_places=3, min_value=0.001)
    delivery_date = forms.DateField(required=False, widget=forms.DateInput(attrs={'type': 'date'}))

    def clean_delivery_date(self):
        date = self.cleaned_data['delivery_date']
        if date and date < datetime.date.today():
            raise forms.ValidationError("The delivery date can't be in the past.")
        return date


def _repeat(source, quantity, delivery_date):
    """A new pending order that repeats `source` with a new quantity and delivery date."""
    order = Order(customer=source.customer, status='pending', quantity=quantity, delivery_date=delivery_date,
                  reordered_from=source, **{name: getattr(source, name) for name in REORDER_COPIED})
    order.save()
    if source.drawing_file:   # the drawing travels with the repeat; a missing file just means none is attached
        try:
            with source.drawing_file.open('rb') as handle:
                order.drawing_file.save(os.path.basename(source.drawing_file.name), ContentFile(handle.read()), save=True)
        except OSError:
            pass
    return order


def portal_reorder(request, token, order_pk):
    customer = _company(token)
    source = get_object_or_404(customer.orders.select_related('product_code__product_type', 'product_type'), pk=order_pk)
    if source.status == 'cancelled':
        raise Http404
    form = ReorderForm(request.POST or None, initial={'quantity': source.quantity})
    if request.method == 'POST' and form.is_valid():
        _repeat(source, form.cleaned_data['quantity'], form.cleaned_data['delivery_date'])
        return render(request, 'materials/portal_reordered.html', {**_branding(customer), 'customer': customer, 'token': token})
    return render(request, 'materials/portal_reorder.html', {
        **_branding(customer), 'customer': customer, 'token': token, 'source': source, 'title': source.spec_text() or 'Order', 'form': form,
        'details': [line for line in (source.delivery_detail_text(), *source.tolerance_lines()) if line],
    })


@require_GET
def portal_manifest(request, token):
    """Makes the portal installable as an app on a phone or computer, one per company."""
    customer = _company(token)
    start = reverse('portal_home', args=[token])
    manifest = {
        'name': f"{customer.name} · {settings.COMPANY_NAME}",
        'short_name': settings.COMPANY_NAME,
        'id': start,
        'start_url': start,
        'scope': start,
        'display': 'standalone',
        'background_color': '#f3f4f2',
        'theme_color': '#2f7d5b',
        'icons': [
            {'src': reverse('portal_icon', args=[192]), 'sizes': '192x192', 'type': 'image/png'},
            {'src': reverse('portal_icon', args=[512]), 'sizes': '512x512', 'type': 'image/png'},
        ],
    }
    return HttpResponse(json.dumps(manifest), content_type='application/manifest+json')


@require_GET
def portal_icon(request, size):
    path = os.path.join(settings.BASE_DIR, 'materials', 'static', 'materials', f'portal-icon-{size}.png')
    if not os.path.exists(path):
        raise Http404
    response = FileResponse(open(path, 'rb'), content_type='image/png')
    response['Cache-Control'] = 'public, max-age=86400'
    return response


@require_GET
def portal_service_worker(request):
    """A browser only offers to install a web app that has a service worker. This one caches nothing (an order
    list must always be fresh and is private), it just passes every request through."""
    response = HttpResponse("self.addEventListener('fetch', function () {});\n", content_type='application/javascript')
    response['Cache-Control'] = 'no-cache'
    return response

