"""The company portal: one permanent link per company that has ordered. It shows that company's current and past
orders (a customer follows an order's status only where staff switched tracking on) and lets it repeat an earlier
order, changing only the quantity and the delivery date. A repeat arrives as a pending order for staff to confirm.
There is no way to order a new product here: a new product starts with a query.

Public like the quote form (reached through the Funnel path /portal), so the unguessable link is the only credential,
the pages load nothing from /static, and a company can only ever see its own orders."""

import datetime
import json
import os
import re
from urllib.parse import quote

from django import forms
from django.conf import settings
from django.core.files.base import ContentFile
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.views.decorators.http import require_GET

from ..models import GSTIN_PATTERN, CompanyChangeRequest, Customer, Order, ProductType
from ..order_pdf import generate_order_summary_pdf
from .whatsapp import portal_chat_ref

CURRENT = ('pending', 'confirmed', 'in_production')
SUMMARY_STATUSES = ('confirmed', 'in_production', 'completed')   # a pending order has nothing confirmed to show yet

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
        'has_summary': order.status in SUMMARY_STATUSES,
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


def portal_about(request, token):
    customer = _company(token)
    return render(request, 'materials/portal_about.html', {
        'customer': customer, 'token': token, **_branding(customer),
        'about': settings.COMPANY_ABOUT, 'facts': settings.COMPANY_FACTS,
        'product_types': ProductType.objects.values_list('name', flat=True),
    })


class DetailsForm(forms.Form):
    name = forms.CharField(max_length=100, label='Company name')
    email = forms.EmailField(required=False)
    phone = forms.CharField(max_length=20, required=False)
    gst_number = forms.CharField(max_length=15, required=False, label='GST number')
    gst_address = forms.CharField(required=False, label='GST address', widget=forms.Textarea(attrs={'rows': 3}))

    def clean_name(self):
        return self.cleaned_data['name'].strip().upper()   # one company is never stored under two spellings

    def clean_gst_number(self):
        number = re.sub(r'\s', '', self.cleaned_data['gst_number']).upper()
        if number and not re.fullmatch(GSTIN_PATTERN, number):
            raise forms.ValidationError('Enter a valid 15-character GST number.')
        return number

    def clean_gst_address(self):
        return self.cleaned_data['gst_address'].strip()


def portal_details(request, token):
    """The company's own details, and a way to ask for a change. Nothing is changed here: the request waits on the
    Companies page until staff approve it. A new request replaces one still waiting."""
    customer = _company(token)
    current = {field: getattr(customer, field) for field, _ in CompanyChangeRequest.CHANGEABLE}
    form = DetailsForm(request.POST or None, initial=current)
    sent = nothing_changed = False
    if request.method == 'POST' and form.is_valid():
        changes = {field: value for field, value in form.cleaned_data.items() if value != current[field]}
        if changes:
            customer.change_requests.filter(status='pending').delete()
            CompanyChangeRequest.objects.create(customer=customer, changes=changes)
            sent = True
        else:
            nothing_changed = True
    return render(request, 'materials/portal_details.html', {
        'customer': customer, 'token': token, **_branding(customer), 'form': form, 'sent': sent,
        'nothing_changed': nothing_changed, 'pending': customer.change_requests.filter(status='pending').first(),
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
def portal_order_summary(request, token, order_pk):
    """The same order summary PDF the confirmation email carries, for a company's own confirmed order."""
    customer = _company(token)
    order = get_object_or_404(customer.orders, pk=order_pk, status__in=SUMMARY_STATUSES)
    quotation = order.source_query.latest_sent_quotation() if order.source_query else None
    pdf = generate_order_summary_pdf([order], customer, quotation=quotation, placed_at=order.created_at)
    response = HttpResponse(pdf, content_type='application/pdf')
    response['Content-Disposition'] = 'inline; filename="Order summary.pdf"'
    return response


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

