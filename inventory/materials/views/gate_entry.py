"""Employee gate entry: truck deliveries, lots, and registering coils (with QR tags)."""

import base64
import io
import qrcode

from django.shortcuts import render, redirect, get_object_or_404
from django.db import transaction
from django.db.models import Count
from django.http import JsonResponse

from ..models import GateEntry, GateEntryLot, Material, GradeOption, SizeOption
from ..forms import GateEntryForm, GateEntryLotForm, GateEntryLotFormSet, MaterialForm
from ..decorators import employee_required, employee_or_staff_required
from .common import _GateEntryOverCommitted, _first_form_error, _first_formset_error


@employee_required
def material_field_autocomplete(request):
    """Autosuggest for the free-text company/vendor fields on the gate entry
    form, drawn from values already used in Material — so 'Tata Steel'
    typed once doesn't turn into 'TATA STEEL' and 'Tata steel' as separate
    entries later. `field` is restricted to company/vendor so the query
    param can't be used to probe arbitrary model fields."""
    field = request.GET.get('field')
    if field not in ('company', 'vendor'):
        return JsonResponse([], safe=False)
    q = request.GET.get('q', '').strip()
    if not q:
        return JsonResponse([], safe=False)
    values = (
        Material.objects.filter(**{f'{field}__icontains': q})
        .exclude(**{field: ''})
        .order_by(field)
        .values_list(field, flat=True)
        .distinct()[:8]
    )
    return JsonResponse(list(values), safe=False)


@employee_required
def gate_entry_form(request):
    """Log a truck's delivery in one submission: the truck's own details
    (company/vehicle/total weight) plus one or more lots — vendor/grade/
    size/coil-count, since a single truck can carry a mixed load sourced
    from more than one vendor. Saving creates the GateEntry and every lot
    atomically, then lands on the gate entry's detail page to start
    registering coils. gate_entry_lot_form (a single extra lot) is the
    follow-up path for a delivery that turns out to have more lots than
    were known about at logging time."""
    error = None
    if request.method == "POST":
        entry_form = GateEntryForm(request.POST)
        lot_formset = GateEntryLotFormSet(request.POST, prefix='lot')
        if entry_form.is_valid() and lot_formset.is_valid():
            with transaction.atomic():
                gate_entry = entry_form.save()
                for lot_data in lot_formset.cleaned_data:
                    GateEntryLot.objects.create(gate_entry=gate_entry, **lot_data)
            return redirect('gate_entry_detail', gate_entry_pk=gate_entry.pk)
        error = _first_form_error(entry_form) if not entry_form.is_valid() else _first_formset_error(lot_formset)
    else:
        entry_form = GateEntryForm()
        lot_formset = GateEntryLotFormSet(prefix='lot')

    return render(request, "materials/gate_entry_form.html", {
        "lot_formset": lot_formset,
        "empty_lot_form": lot_formset.empty_form,
        "grades": GradeOption.objects.all(),
        "sizes": SizeOption.objects.all(),
        "error": error,
        "post": request.POST if error else {},
    })


@employee_required
def gate_entry_lot_form(request, gate_entry_pk):
    """Add one more lot to an already-logged gate entry — for a delivery
    that turns out to have another grade/size beyond what was entered on
    the main gate entry page. Lands on the gate entry's detail page."""
    gate_entry = get_object_or_404(GateEntry, pk=gate_entry_pk)
    error = None
    if request.method == "POST":
        form = GateEntryLotForm(request.POST)
        if form.is_valid():
            GateEntryLot.objects.create(gate_entry=gate_entry, **form.cleaned_data)
            return redirect('gate_entry_detail', gate_entry_pk=gate_entry.pk)
        error = _first_form_error(form)

    return render(request, "materials/gate_entry_lot_form.html", {
        "gate_entry": gate_entry,
        "grades": GradeOption.objects.all(),
        "sizes": SizeOption.objects.all(),
        "error": error,
        "post": request.POST if error else {},
    })


@employee_required
def gate_entry_detail(request, gate_entry_pk):
    """Shows the lots logged so far for this gate entry, with a link into
    coil registration for each lot that still has room, and a way to add
    another lot for the rest of a mixed-grade/size delivery."""
    gate_entry = get_object_or_404(GateEntry, pk=gate_entry_pk)
    lots = gate_entry.lots.annotate(registered=Count('coils')).order_by('id')

    return render(request, "materials/gate_entry_detail.html", {
        "gate_entry": gate_entry,
        "lots": lots,
    })


@employee_required
def gate_entry_edit(request, gate_entry_pk):
    """Fix a mistake in a gate entry's top-level details (date, vendor,
    vehicle no., invoice no., total weight) after it's already been saved —
    unlike a lot or a registered coil, nothing about the gate entry itself
    is locked once coils exist against it, since these fields are just
    paper/reference details, not something coil registration depends on
    being immutable."""
    gate_entry = get_object_or_404(GateEntry, pk=gate_entry_pk)

    error = None
    if request.method == "POST":
        form = GateEntryForm(request.POST, instance=gate_entry)
        if form.is_valid():
            form.save()
            return redirect('gate_entry_detail', gate_entry_pk=gate_entry.pk)
        error = _first_form_error(form)
        values = request.POST
    else:
        values = {
            'date': gate_entry.date.isoformat() if gate_entry.date else '',
            'vendor': gate_entry.vendor or '',
            'vehicle_no': gate_entry.vehicle_no or '',
            'invoice_no': gate_entry.invoice_no or '',
            'total_weight': gate_entry.total_weight if gate_entry.total_weight is not None else '',
        }

    return render(request, "materials/gate_entry_edit.html", {
        "gate_entry": gate_entry,
        "error": error,
        "post": values,
    })


@employee_required
def gate_entry_lot_delete(request, lot_pk):
    """Remove a lot added by mistake — only while it has no coils registered
    against it yet. (Material.lot uses on_delete=PROTECT, so this would fail
    loudly rather than orphan real coils even without the check below.)"""
    lot = get_object_or_404(GateEntryLot, pk=lot_pk)
    gate_entry_pk = lot.gate_entry_id
    if request.method == "POST" and lot.coils_registered() == 0:
        lot.delete()
    return redirect('gate_entry_detail', gate_entry_pk=gate_entry_pk)


@employee_required
def select_gate_entry(request):
    lots = []
    for lot in (GateEntryLot.objects
                .select_related('gate_entry')
                .annotate(registered=Count('coils'))
                .order_by('-gate_entry__created_at', 'id')):
        remaining = lot.no_of_coils - lot.registered
        if remaining > 0:
            lots.append({'lot': lot, 'remaining': remaining, 'registered': lot.registered})

    return render(request, "materials/select_gate_entry.html", {"lots": lots})


@employee_required
def material_form(request, lot_pk):
    lot = get_object_or_404(GateEntryLot.objects.select_related('gate_entry'), pk=lot_pk)
    gate_entry = lot.gate_entry
    complete = lot.is_complete()

    error = None
    if request.method == "POST":
        if complete:
            error = "This lot's coils have already been registered."
        else:
            # Vendor is locked to the gate entry, company/grade/size to the
            # lot — none of this is taken from the submitted form at all, the
            # same way coil_parts locks product type from an order, so a
            # tampered/stale hidden field can't submit different values.
            data = request.POST.copy()
            data['vendor'] = gate_entry.vendor
            data['company'] = lot.company
            data['grade'] = lot.grade
            data['size'] = lot.size
            form = MaterialForm(data)
            if form.is_valid():
                try:
                    with transaction.atomic():
                        coil = form.save(commit=False)
                        coil.lot = lot
                        coil.invoice_weight = gate_entry.weight_per_coil()
                        coil.save()
                        # Two employees registering the same lot's last
                        # remaining slot at nearly the same moment could both
                        # pass the `complete` check above against a stale read
                        # before either had written anything — re-check after
                        # writing and roll back rather than overshoot the count.
                        if lot.coils_registered() > lot.no_of_coils:
                            raise _GateEntryOverCommitted
                except _GateEntryOverCommitted:
                    error = "This lot's coils have already been registered — reload and check with the office."
                else:
                    return redirect('coil_tag', pk=coil.coil_no)
            else:
                error = _first_form_error(form)

    last_material = Material.objects.order_by('-coil_no').first()
    next_coil = (last_material.coil_no + 1) if last_material else 1
    formatted_coil = f"COIL{next_coil:04d}"

    return render(request, "materials/material_form.html", {
        "coil_no": formatted_coil,
        "gate_entry": gate_entry,
        "lot": lot,
        "complete": complete,
        "error": error,
        "post": request.POST if error else {},
    })


@employee_or_staff_required
def coil_tag(request, pk):
    coil = get_object_or_404(Material, pk=pk)

    # Generate QR code encoding the formatted coil number
    qr = qrcode.QRCode(box_size=6, border=2)
    qr.add_data(coil.formatted_coil())
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")

    buf = io.BytesIO()
    img.save(buf, format='PNG')
    qr_b64 = base64.b64encode(buf.getvalue()).decode()

    return render(request, 'materials/coil_tag.html', {
        'coil': coil,
        'qr_b64': qr_b64,
        'lot': coil.lot,
    })
