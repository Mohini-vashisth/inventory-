"""Dispatch: shipments of orders. The owners plan them (a date, the weight, the vehicle and the LR / invoice number) for
orders in production and mark each one dispatched when it leaves; the plant (employee portal) and finance see the same
list read-only. Nothing here shows a price."""

import re

from django import forms
from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from ..decorators import employee_or_staff_required, finance_required, redirect_to_admin_login, staff_required
from ..models import Dispatch, Order

SHIPMENTS = Dispatch.objects.select_related('order__customer', 'order__product_code__product_type', 'order__product_type')


class DispatchForm(forms.ModelForm):
    class Meta:
        model = Dispatch
        fields = ['planned_date', 'weight', 'vehicle_no', 'lr_no']
        widgets = {'planned_date': forms.DateInput(attrs={'type': 'date'})}

    def clean_vehicle_no(self):
        return re.sub(r'[^A-Z0-9]', '', self.cleaned_data['vehicle_no'].upper())   # letters and digits only, like the gate entry


def _lists():
    """What the read-only pages show: shipments still to go, soonest first, and the latest ones that left."""
    return {'lists': [
        ('Planned', SHIPMENTS.filter(dispatched_at__isnull=True).order_by('planned_date', 'pk'), 'Planned for'),
        ('Dispatched (latest 30)', SHIPMENTS.filter(dispatched_at__isnull=False).order_by('-dispatched_at')[:30], 'Left on')]}


@staff_required(on_denied=redirect_to_admin_login)
def dispatch_planning(request):
    orders = (Order.objects.filter(status='in_production')
              .select_related('customer', 'product_code__product_type', 'product_type')
              .prefetch_related('dispatches').order_by('delivery_date', 'order_no'))
    rows = []
    for order in orders:
        shipments = list(order.dispatches.all())
        for d in shipments:
            if not d.dispatched_at:
                d.edit_form = DispatchForm(instance=d, prefix=f'e{d.pk}')
        sent = sum(d.weight for d in shipments if d.dispatched_at)
        planned = sum(d.weight for d in shipments if not d.dispatched_at)
        rows.append({'order': order, 'shipments': shipments, 'sent': sent, 'planned': planned,
                     'left': max(order.quantity - sent - planned, 0) if order.quantity else None,
                     'form': DispatchForm(prefix=f'o{order.pk}', initial={'planned_date': timezone.localdate()})})
    return render(request, 'materials/dispatch_planning.html', {'rows': rows})


@staff_required(on_denied=redirect_to_admin_login)
@require_POST
def dispatch_add(request, order_pk):
    order = get_object_or_404(Order, pk=order_pk, status='in_production')
    form = DispatchForm(request.POST, prefix=f'o{order.pk}')
    if form.is_valid():
        form.instance.order = order
        form.save()
        messages.success(request, f"Shipment planned for ORD-{order.order_no:04d}.")
    else:
        messages.error(request, "Could not add the shipment: " + "; ".join(e for errors in form.errors.values() for e in errors))
    return redirect('dispatch_planning')


@staff_required(on_denied=redirect_to_admin_login)
@require_POST
def dispatch_edit(request, pk):
    shipment = get_object_or_404(Dispatch, pk=pk, dispatched_at__isnull=True)   # one that already left is a record, not a plan
    form = DispatchForm(request.POST, instance=shipment, prefix=f'e{pk}')
    if form.is_valid():
        form.save()
        messages.success(request, f"ORD-{shipment.order.order_no:04d}: shipment updated.")
    else:
        messages.error(request, "Could not change the shipment: " + "; ".join(e for errors in form.errors.values() for e in errors))
    return redirect('dispatch_planning')


@staff_required(on_denied=redirect_to_admin_login)
@require_POST
def dispatch_mark(request, pk):
    shipment = get_object_or_404(Dispatch, pk=pk)
    if not shipment.dispatched_at:
        shipment.dispatched_at = timezone.now()
        shipment.save(update_fields=['dispatched_at'])
        messages.success(request, f"ORD-{shipment.order.order_no:04d}: {shipment.weight:.3f} kg marked dispatched.")
    return redirect('dispatch_planning')


@staff_required(on_denied=redirect_to_admin_login)
@require_POST
def dispatch_delete(request, pk):
    shipment = get_object_or_404(Dispatch, pk=pk, dispatched_at__isnull=True)   # one that already left is a record, not a plan
    shipment.delete()
    messages.success(request, "Shipment removed.")
    return redirect('dispatch_planning')


@finance_required
def finance_dispatch(request):
    return render(request, 'materials/dispatch_page.html', {
        'title': 'Dispatch', 'back_url': reverse('finance'), 'back_label': '← Back to finance', **_lists()})


@employee_or_staff_required
def employee_dispatch(request):
    return render(request, 'materials/employee_dispatch.html', _lists())
