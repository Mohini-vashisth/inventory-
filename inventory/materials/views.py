from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth import authenticate, login
from django.contrib import messages
from django.core.mail import EmailMessage
from django.conf import settings
from django.urls import reverse
from django.db import transaction
from django.db.models import Count, DecimalField, F, Sum, Value
from django.db.models.functions import Coalesce
from django.http import JsonResponse, HttpResponse, HttpResponseForbidden
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.crypto import constant_time_compare
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
import uuid
import qrcode
import io
import base64
import hmac
import hashlib
import json
import logging
import re
import threading
import urllib.request
import urllib.error
from decimal import Decimal, InvalidOperation
from django.core.exceptions import ValidationError
from django.core.validators import validate_email

logger = logging.getLogger(__name__)
from .models import GateEntry, GateEntryLot, Material, OrderCoilPick, GradeOption, SizeOption, ProductType, AllowedCoilSpec, ProcessStep, ProductionJob, StepLog, Customer, Query, Order, Quotation
from .forms import GateEntryForm, GateEntryLotForm, GateEntryLotFormSet, MaterialForm, OrderForm
from .pdf import generate_quotation_pdf


class _CoilOverCommitted(Exception):
    """Raised inside pick_coil_for_order's atomic block when, after actually
    writing a new pick, the coil's total used weight now exceeds its
    quantity — catches two concurrent picks on the same coil that both
    passed the earlier read-based check against a stale "remaining" value
    before either had written anything."""


class _GateEntryOverCommitted(Exception):
    """Raised inside material_form's atomic block when, after actually
    writing a new coil, the gate entry now has more registered coils than
    invoiced — catches two concurrent registrations against the same gate
    entry's last remaining slot that both passed the earlier read-based
    check before either had written anything."""


def _safe_next(request, next_url, default):
    """Only follow `next` if it points back at this host — blocks open-redirect via a spoofed link."""
    if next_url and url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        return next_url
    return default


def _first_form_error(form):
    for errors in form.errors.values():
        return errors[0]
    return "Invalid order details."


def _safe_get(queryset, pk):
    """Look up a row by a pk that may be missing, empty, or malformed (e.g. raw POST data)
    without raising ValueError — Model.objects.filter(pk=...) still raises on a non-numeric pk."""
    if pk in (None, ''):
        return None
    try:
        return queryset.filter(pk=pk).first()
    except (ValueError, TypeError):
        return None


def _parse_rate_per_kg(raw):
    """A quotation's rate must be a real, positive number — returns None
    (rather than raising) for anything else so callers can show a plain
    error instead of a 500."""
    try:
        rate = Decimal(raw)
    except (InvalidOperation, TypeError):
        return None
    return rate if rate > 0 else None


# ── Employee auth ────────────────────────────────────────────

def employee_login(request):
    next_url = _safe_next(request, request.GET.get('next') or request.POST.get('next'), reverse('employee'))
    if request.session.get('employee_auth'):
        return redirect(next_url)
    error = None
    if request.method == 'POST':
        if constant_time_compare(request.POST.get('pin', ''), settings.EMPLOYEE_PIN):
            request.session['employee_auth'] = True
            return redirect(next_url)
        error = "Incorrect PIN."
    return render(request, 'materials/employee_login.html', {'error': error, 'next': next_url})


def employee_logout(request):
    if request.method == 'POST':
        request.session.flush()
    return redirect('employee_login')


def _employee_required(request):
    """Returns a redirect response if not authenticated, else None."""
    if not request.session.get('employee_auth'):
        return redirect(f"{reverse('employee_login')}?next={request.path}")
    return None


def home(request):
    return render(request, "home.html")


def _first_formset_error(formset):
    if formset.non_form_errors():
        return formset.non_form_errors()[0]
    for form in formset:
        for errors in form.errors.values():
            return errors[0]
    return "Check the lot details below."


def material_field_autocomplete(request):
    """Autosuggest for the free-text company/vendor fields on the gate entry
    form, drawn from values already used in Material — so 'Tata Steel'
    typed once doesn't turn into 'TATA STEEL' and 'Tata steel' as separate
    entries later. `field` is restricted to company/vendor so the query
    param can't be used to probe arbitrary model fields."""
    guard = _employee_required(request)
    if guard: return guard
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


def gate_entry_form(request):
    """Log a truck's delivery in one submission: the truck's own details
    (company/vehicle/total weight) plus one or more lots — vendor/grade/
    size/coil-count, since a single truck can carry a mixed load sourced
    from more than one vendor. Saving creates the GateEntry and every lot
    atomically, then lands on the gate entry's detail page to start
    registering coils. gate_entry_lot_form (a single extra lot) is the
    follow-up path for a delivery that turns out to have more lots than
    were known about at logging time."""
    guard = _employee_required(request)
    if guard: return guard
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


def gate_entry_lot_form(request, gate_entry_pk):
    """Add one more lot to an already-logged gate entry — for a delivery
    that turns out to have another grade/size beyond what was entered on
    the main gate entry page. Lands on the gate entry's detail page."""
    guard = _employee_required(request)
    if guard: return guard
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


def gate_entry_detail(request, gate_entry_pk):
    """Shows the lots logged so far for this gate entry, with a link into
    coil registration for each lot that still has room, and a way to add
    another lot for the rest of a mixed-grade/size delivery."""
    guard = _employee_required(request)
    if guard: return guard
    gate_entry = get_object_or_404(GateEntry, pk=gate_entry_pk)
    lots = gate_entry.lots.annotate(registered=Count('coils')).order_by('id')

    return render(request, "materials/gate_entry_detail.html", {
        "gate_entry": gate_entry,
        "lots": lots,
    })


def gate_entry_edit(request, gate_entry_pk):
    """Fix a mistake in a gate entry's top-level details (date, vendor,
    vehicle no., invoice no., total weight) after it's already been saved —
    unlike a lot or a registered coil, nothing about the gate entry itself
    is locked once coils exist against it, since these fields are just
    paper/reference details, not something coil registration depends on
    being immutable."""
    guard = _employee_required(request)
    if guard: return guard
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


def gate_entry_lot_delete(request, lot_pk):
    """Remove a lot added by mistake — only while it has no coils registered
    against it yet. (Material.lot uses on_delete=PROTECT, so this would fail
    loudly rather than orphan real coils even without the check below.)"""
    guard = _employee_required(request)
    if guard: return guard
    lot = get_object_or_404(GateEntryLot, pk=lot_pk)
    gate_entry_pk = lot.gate_entry_id
    if request.method == "POST" and lot.coils_registered() == 0:
        lot.delete()
    return redirect('gate_entry_detail', gate_entry_pk=gate_entry_pk)


def select_gate_entry(request):
    guard = _employee_required(request)
    if guard: return guard
    lots = []
    for lot in (GateEntryLot.objects
                .select_related('gate_entry')
                .annotate(registered=Count('coils'))
                .order_by('-gate_entry__created_at', 'id')):
        remaining = lot.no_of_coils - lot.registered
        if remaining > 0:
            lots.append({'lot': lot, 'remaining': remaining, 'registered': lot.registered})

    return render(request, "materials/select_gate_entry.html", {"lots": lots})


def material_form(request, lot_pk):
    guard = _employee_required(request)
    if guard: return guard
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


def coil_tag(request, pk):
    guard = _employee_required(request)
    if guard: return guard
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


def admin_login(request):
    next_url = _safe_next(request, request.GET.get('next') or request.POST.get('next'), '/admin/')
    if request.method == "POST":
        username = request.POST.get("username")
        password = request.POST.get("password")
        user = authenticate(request, username=username, password=password)

        if user is not None:
            login(request, user)
            return redirect(next_url)
        else:
            return render(request, "materials/admin_login.html", {"error": "Invalid credentials", "next": next_url})

    return render(request, "materials/admin_login.html", {"next": next_url})


# ── Order-first coil picking flow ─────────────────────────────

def select_order(request):
    guard = _employee_required(request)
    if guard: return guard
    not_started = (Order.objects
                   .filter(status='confirmed')
                   .select_related('customer', 'product_type')
                   .order_by('delivery_date'))
    in_progress  = (Order.objects
                    .filter(status='in_production')
                    .select_related('customer', 'product_type')
                    .order_by('delivery_date'))
    return render(request, 'materials/select_order.html', {
        'not_started': not_started,
        'in_progress': in_progress,
    })


def _coil_matches_order_specs(coil, order):
    """True if this coil's grade/size is allowed as raw material for the
    order's product type — same rule select_coil_for_order's browse list
    filters by, reused here so a scanned coil gets the same check."""
    if not order.product_type:
        return True
    specs = list(order.product_type.allowed_specs.all())
    if not specs:
        return True
    for spec in specs:
        grade_matches = not spec.grade or (coil.grade or '').lower() == spec.grade.lower()
        size_matches = not spec.size or coil.size == spec.size
        if grade_matches and size_matches:
            return True
    return False


def _ratio_for_coil(order, coil, specs=None):
    """The AllowedCoilSpec.raw_material_ratio for whichever spec matches this
    coil's grade/size under the order's product type — same matching rule
    OrderCoilPick.output_equivalent() uses, so the "how much raw material
    does the order still need" figure stays consistent everywhere it's
    computed. Falls back to 1:1 if no spec matches.

    `specs` lets a caller looping over many coils for the same order (e.g.
    the best-fit sort in select_coil_for_order) pass an already-fetched
    list instead of re-querying allowed_specs on every single coil."""
    if order.product_type:
        for spec in (specs if specs is not None else order.product_type.allowed_specs.all()):
            grade_matches = not spec.grade or spec.grade.lower() == (coil.grade or '').lower()
            size_matches = not spec.size or spec.size == coil.size
            if grade_matches and size_matches:
                return spec.raw_material_ratio
    return Decimal('1')


def _parse_coil_no(raw):
    """Accepts either the formatted tag text ("COIL0007") or a bare number,
    tolerant of surrounding whitespace/case — matches whatever a barcode
    scanner (which just types the QR's text into the input) sends."""
    raw = (raw or '').strip().upper()
    if raw.startswith('COIL'):
        raw = raw[4:]
    try:
        return int(raw)
    except ValueError:
        return None


def select_coil_for_order(request, order_pk):
    """The order's picking hub: shows how much raw material has been picked
    so far against how much the order needs, lists eligible coils to browse,
    and accepts a scanned/typed coil number to jump straight into picking it."""
    guard = _employee_required(request)
    if guard: return guard
    order = get_object_or_404(
        Order.objects.select_related('customer', 'product_type'),
        pk=order_pk,
    )

    scan_error = None
    if request.method == 'POST':
        coil_no = _parse_coil_no(request.POST.get('coil_no'))
        coil = _safe_get(Material.objects, coil_no) if coil_no is not None else None
        if coil is None:
            scan_error = "Coil not found. Check the number and try again."
        elif coil.is_archived():
            scan_error = f"{coil.formatted_coil()} is archived and can't be picked."
        elif not _coil_matches_order_specs(coil, order):
            scan_error = f"{coil.formatted_coil()} doesn't match this order's allowed grade/size."
        elif coil.weight_remaining() <= 0:
            scan_error = f"{coil.formatted_coil()} has no weight remaining."
        else:
            return redirect('pick_coil_for_order', order_pk=order.pk, coil_pk=coil.pk)

    coils_qs = Material.objects.filter(archived_at__isnull=True).annotate(
        _weight_used=Coalesce(Sum('order_picks__weight_allocated'), Value(Decimal('0')), output_field=DecimalField())
                     + F('legacy_used_weight'),
    )

    # Filter by allowed specs if the order has a product type configured.
    # Fetched once here and reused below for the best-fit ratio, instead of
    # each coil in the loop re-querying allowed_specs for itself.
    specs = []
    if order.product_type:
        specs = list(order.product_type.allowed_specs.all())
        if specs:
            from django.db.models import Q
            q = Q()
            for spec in specs:
                spec_q = Q()
                if spec.grade:
                    spec_q &= Q(grade__iexact=spec.grade)
                if spec.size:
                    spec_q &= Q(size=spec.size)
                if spec_q:
                    q |= spec_q
            coils_qs = coils_qs.filter(q)

    picked_output = order.picked_output_weight()
    required_output = order.quantity or Decimal('0')
    order_remaining_output = max(required_output - picked_output, Decimal('0'))

    # Only coils with remaining weight, closest-to-what's-still-needed first
    # (in this coil's own raw-material terms, via its matching spec ratio) —
    # a best-fit pick wastes less than grabbing whatever coil is newest.
    coils = []
    for coil in coils_qs.order_by('-coil_no'):
        used      = float(coil._weight_used or 0)
        total     = float(coil.quantity or 0)
        remaining = total - used
        if remaining > 0:
            order_needs = float(order_remaining_output * _ratio_for_coil(order, coil, specs))
            coils.append({
                'coil': coil,
                'remaining': remaining,
                'total': total,
                'pct_used': int((used / total * 100)) if total > 0 else 0,
                'weight_diff': abs(remaining - order_needs),
            })
    coils.sort(key=lambda item: item['weight_diff'])

    picks = order.coil_picks.select_related('coil').order_by('-picked_at')

    return render(request, 'materials/select_coil_for_order.html', {
        'order': order,
        'coils': coils,
        'picks': picks,
        'picked_output': picked_output,
        'required_output': required_output,
        'pct_fulfilled': int(min(picked_output / required_output * 100, 100)) if required_output > 0 else 0,
        'fully_picked': order.is_fully_picked(),
        'scan_error': scan_error,
    })


def pick_coil_for_order(request, order_pk, coil_pk):
    """Confirm-and-allocate screen for one coil against one order — mirrors
    material_form's single-entity-confirm pattern. Product type comes from
    the order, never resubmitted, so a tampered/stale form can't override it."""
    guard = _employee_required(request)
    if guard: return guard
    order = get_object_or_404(Order.objects.select_related('product_type', 'customer'), pk=order_pk)
    coil = get_object_or_404(Material, pk=coil_pk)

    coil_total = coil.quantity or Decimal('0')
    coil_remaining = coil_total - coil.weight_used()
    exhausted = coil_total > 0 and coil_remaining <= 0
    archived = coil.is_archived()
    matches_specs = _coil_matches_order_specs(coil, order)
    fully_picked = order.is_fully_picked()
    blocked = exhausted or archived or not matches_specs or fully_picked or order.product_type is None

    # Suggest a weight capped at both what's left on the coil and what the
    # order still needs (in this coil's raw-material terms), so an employee
    # isn't nudged into over-allocating by default.
    ratio = _ratio_for_coil(order, coil)
    order_remaining_raw = max(order.quantity - order.picked_output_weight(), Decimal('0')) * ratio
    suggested_weight = max(min(coil_remaining, order_remaining_raw), Decimal('0'))

    error = None
    if request.method == 'POST':
        if blocked:
            return redirect('select_coil_for_order', order_pk=order.pk)

        raw_weight = request.POST.get('weight_allocated')
        weight_value, weight_invalid = None, False
        if raw_weight:
            try:
                weight_value = Decimal(raw_weight)
            except InvalidOperation:
                weight_invalid = True

        if weight_invalid or not weight_value or weight_value <= 0:
            error = "Enter a valid weight to allocate."
        elif weight_value > coil_remaining:
            error = (
                f"Weight ({weight_value:.3f} kg) exceeds the remaining coil weight "
                f"({coil_remaining:.3f} kg)."
            )
        else:
            pt = order.product_type
            try:
                with transaction.atomic():
                    pick = OrderCoilPick.objects.create(
                        order=order, coil=coil, weight_allocated=weight_value,
                    )
                    # Re-check against the coil's actual total now that this
                    # pick is written (visible within this same transaction)
                    # — two tablets picking the same coil at nearly the same
                    # moment could both have passed the "remaining" check
                    # above against a stale read before either had written
                    # anything. If this overshoots, roll back rather than
                    # silently over-allocate the coil.
                    if coil.quantity is not None and coil.weight_used() > coil.quantity:
                        raise _CoilOverCommitted
                    job = ProductionJob.objects.create(
                        pick=pick, product_type=pt, order=order, job_no='PENDING',
                    )
                    job.job_no = f"JOB-{job.pk:04d}"
                    job.save(update_fields=['job_no'])
                    for step in pt.steps.all():
                        StepLog.objects.create(
                            job=job, step=step, status='pending',
                            updated_by=request.user if request.user.is_authenticated else None,
                        )
                    # Mark order as in production when its first coil is picked
                    if order.status == 'confirmed':
                        order.status = 'in_production'
                        order.save(update_fields=['status'])
            except _CoilOverCommitted:
                error = (
                    "This coil's remaining weight changed just now — likely someone else "
                    "picking it at the same time. Reload the page and try again."
                )
            except (InvalidOperation, ValidationError, ValueError):
                error = "Check the entered weight."
            else:
                return redirect('select_coil_for_order', order_pk=order.pk)

    return render(request, 'materials/pick_coil_for_order.html', {
        'order': order, 'coil': coil,
        'coil_total': coil_total, 'coil_remaining': coil_remaining,
        'suggested_weight': suggested_weight,
        'exhausted': exhausted, 'archived': archived,
        'matches_specs': matches_specs, 'fully_picked': fully_picked,
        'blocked': blocked, 'error': error,
    })


# ── Production jobs ──────────────────────────────────────────

def job_detail(request, pk):
    guard = _employee_required(request)
    if guard: return guard
    job = get_object_or_404(
        ProductionJob.objects.select_related('pick__coil', 'product_type')
                             .prefetch_related('step_logs', 'product_type__steps'),
        pk=pk,
    )
    steps = list(job.product_type.steps.all())

    # Build latest log per step from prefetched data
    logs_by_step = {}
    for log in sorted(job.step_logs.all(), key=lambda l: l.timestamp, reverse=True):
        logs_by_step.setdefault(log.step_id, log)

    # A step is unlocked only if all steps before it are completed
    unlocked_step_ids = set()
    for step in steps:
        prev_steps = [s for s in steps if s.order < step.order]
        if all(logs_by_step.get(s.id) and logs_by_step[s.id].status == 'completed'
               for s in prev_steps):
            unlocked_step_ids.add(step.id)

    if request.method == 'POST':
        step_id = request.POST.get('step_id')
        action = request.POST.get('action')
        if action not in ('start', 'complete'):
            return redirect('job_detail', pk=job.pk)
        new_status = 'completed' if action == 'complete' else 'in_progress'
        step = _safe_get(ProcessStep.objects, step_id)

        if step is None or step.id not in unlocked_step_ids:
            return redirect('job_detail', pk=job.pk)

        StepLog.objects.create(
            job=job, step=step, status=new_status,
            updated_by=request.user if request.user.is_authenticated else None,
        )
        job.recalculate_status()

        return redirect('job_detail', pk=job.pk)

    return render(request, 'materials/job_detail.html', {
        'job': job,
        'steps': steps,
        'logs_by_step': logs_by_step,
        'unlocked_step_ids': unlocked_step_ids,
    })


def select_job_for_coil(request):
    """Gate into job_detail — progress can only be updated by scanning or
    typing the coil's own number, the same way an order can only be picked
    by scanning the coil being picked. production_board is read-only status
    now; this is the only path into actually updating a job."""
    guard = _employee_required(request)
    if guard: return guard

    scan_error = None
    jobs = None
    if request.method == 'POST':
        coil_no = _parse_coil_no(request.POST.get('coil_no'))
        coil = _safe_get(Material.objects, coil_no) if coil_no is not None else None
        if coil is None:
            scan_error = "Coil not found. Check the number and try again."
        else:
            jobs = list(
                ProductionJob.objects
                .filter(pick__coil=coil)
                .select_related('order', 'product_type')
                .order_by('-created_at')
            )
            if not jobs:
                scan_error = f"{coil.formatted_coil()} hasn't been picked for any order yet — nothing to update."
            elif len(jobs) == 1:
                return redirect('job_detail', pk=jobs[0].pk)
            # else: multiple jobs on this coil (split across orders) — let
            # the employee pick which one, rendered below.

    return render(request, 'materials/select_job_for_coil.html', {
        'scan_error': scan_error,
        'jobs': jobs,
    })


def production_board(request):
    guard = _employee_required(request)
    if guard: return guard
    orders = (Order.objects
              .filter(status='in_production')
              .select_related('customer', 'product_type')
              .prefetch_related(
                  'jobs__pick__coil',
                  'jobs__product_type__steps',
                  'jobs__step_logs__step',
              )
              .order_by('delivery_date'))

    board = []
    for order in orders:
        jobs_data = []
        for job in order.jobs.all():
            steps = list(job.product_type.steps.all())
            total = len(steps)

            logs_by_step = {}
            for log in sorted(job.step_logs.all(), key=lambda l: l.timestamp, reverse=True):
                logs_by_step.setdefault(log.step_id, log)

            completed = sum(
                1 for s in steps
                if logs_by_step.get(s.id) and logs_by_step[s.id].status == 'completed'
            )

            current_step = None
            current_status = 'completed'
            for step in steps:
                log = logs_by_step.get(step.id)
                if not log or log.status != 'completed':
                    current_step = step
                    current_status = log.status if log else 'pending'
                    break

            jobs_data.append({
                'job': job,
                'total': total,
                'completed': completed,
                'pct': int(completed / total * 100) if total > 0 else 0,
                'current_step': current_step,
                'current_status': current_status,
            })

        weight_cut = sum(float(jd['job'].pick.weight_allocated or 0) for jd in jobs_data)
        weight_needed = float(order.quantity or 0)
        board.append({
            'order': order,
            'jobs': jobs_data,
            'weight_cut': weight_cut,
            'weight_fulfilled': weight_needed > 0 and weight_cut >= weight_needed,
        })

    return render(request, 'materials/production_board.html', {'board': board})


def employee_landing(request):
    guard = _employee_required(request)
    if guard: return guard
    return render(request, 'materials/employee_landing.html')


# ── Queries (pre-quote leads) ──────────────────────────────────

def query_dashboard(request):
    """Unified log of inbound sales inquiries — phone calls, referrals,
    IndiaMART, WhatsApp — regardless of source, before any of them become a
    formal quote/order. Logging a query immediately kicks off the WhatsApp
    intake sequence (see whatsapp_webhook below) — staff only ever type in
    a phone number here; everything else arrives via WhatsApp. Staff decide
    which ones to pursue via query_send_quote once that sequence completes."""
    if not request.user.is_authenticated or not request.user.is_staff:
        return redirect(f"{reverse('admin_login')}?next={reverse('query_dashboard')}")

    queries = Query.objects.select_related('product_type', 'customer').prefetch_related('quotations').all()
    error = None

    if request.method == 'POST':
        source = request.POST.get('source', '')
        contact_phone = _normalize_phone(request.POST.get('contact_phone', ''))

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
            query = Query.objects.create(source=source, contact_phone=contact_phone)
            try:
                _send_whatsapp_template_message(
                    contact_phone, WHATSAPP_QUERY_INTAKE_TEMPLATE, language=WHATSAPP_QUERY_INTAKE_TEMPLATE_LANGUAGE,
                )
            except WhatsAppSendError as e:
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


def query_send_quote(request, pk):
    """Create/reuse a Customer from the query's captured info, record the
    rate just finalized as an official Quotation, and send it — by email
    (PDF attached, ahead of the order-form link) if one's on file,
    otherwise staff fall back to Copy Link or the quotation PDF download
    (same as any other customer)."""
    if not request.user.is_staff:
        return redirect('home')
    if request.method != 'POST':
        return redirect('query_dashboard')
    query = get_object_or_404(Query, pk=pk)

    rate_per_kg = _parse_rate_per_kg(request.POST.get('rate_per_kg'))
    if rate_per_kg is None:
        messages.error(request, "Enter a valid rate per kg before sending the quote.")
        return redirect('query_dashboard')

    customer_name = query.company_name or query.contact_phone or f"Query #{query.pk}"
    customer, _ = Customer.objects.get_or_create(name=customer_name)
    if query.contact_email:
        customer.email = query.contact_email
    if query.contact_phone:
        customer.phone = query.contact_phone
    customer.save(update_fields=['email', 'phone'])

    query.customer = customer
    query.status = 'quote_sent'
    query.save(update_fields=['customer', 'status'])

    # The rate form prefills from the query but is editable — fall back to
    # the query's own values for anything left blank.
    product_type_id = request.POST.get('product_type') or query.product_type_id
    grade = request.POST.get('grade', '').strip() or query.grade
    raw_size = request.POST.get('size') or (str(query.size) if query.size is not None else None)
    try:
        quotation = Quotation.objects.create(
            customer=customer, source_query=query, rate_per_kg=rate_per_kg,
            product_type_id=product_type_id, grade=grade, size=raw_size,
        )
    except (InvalidOperation, ValueError, ValidationError):
        messages.error(request, "Check that size is a valid number.")
        return redirect('query_dashboard')

    if customer.email:
        _dispatch_quote_email(request, customer, quotation)
    else:
        messages.warning(
            request,
            f"No email on file for {customer.name} — use “Copy Link” to share the quote form, "
            f"or download the quotation PDF below to send manually.",
        )
    return redirect('query_dashboard')


def query_not_interested(request, pk):
    if not request.user.is_staff:
        return redirect('home')
    if request.method != 'POST':
        return redirect('query_dashboard')
    query = get_object_or_404(Query, pk=pk)
    query.status = 'not_interested'
    query.save(update_fields=['status'])
    return redirect('query_dashboard')


def query_edit(request, pk):
    """Fix a wrong/incomplete field on a query directly from the dashboard —
    a typo'd company name, a grade the WhatsApp bot mis-parsed, etc.
    Previously the only way to do this was Django admin. `source`, `status`,
    and `customer` are deliberately not editable here — status/customer
    changes go through query_send_quote/query_not_interested so they can't
    drift out of sync with what those actions actually did."""
    if not request.user.is_staff:
        return redirect('home')
    query = get_object_or_404(Query, pk=pk)
    product_types = ProductType.objects.order_by('item_code')
    error = None

    if request.method == 'POST':
        raw_size = request.POST.get('size') or None
        raw_quantity = request.POST.get('quantity') or None
        try:
            query.company_name = request.POST.get('company_name', '').strip()
            query.contact_phone = _normalize_phone(request.POST.get('contact_phone', ''))
            query.contact_email = request.POST.get('contact_email', '').strip()
            query.product_type_id = request.POST.get('product_type') or None
            query.grade = request.POST.get('grade', '').strip()
            query.size = raw_size
            query.quantity = raw_quantity
            query.notes = request.POST.get('notes', '').strip()
            query.full_clean(exclude=['source', 'status', 'customer'])
        except ValidationError as e:
            error = e.messages[0] if e.messages else "Check the values entered."
        except (InvalidOperation, ValueError):
            error = "Check that size and quantity are valid numbers."
        else:
            query.save(update_fields=[
                'company_name', 'contact_phone', 'contact_email',
                'product_type', 'grade', 'size', 'quantity', 'notes',
            ])
            return redirect('query_dashboard')

    return render(request, 'materials/query_edit.html', {
        'query': query,
        'product_types': product_types,
        'error': error,
    })


def order_dashboard(request):
    if not request.user.is_authenticated or not request.user.is_staff:
        return redirect(f"{reverse('admin_login')}?next={reverse('order_dashboard')}")

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
                if email: customer.email = email
                if phone: customer.phone = phone
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


def customer_autocomplete(request):
    if not request.user.is_authenticated or not request.user.is_staff:
        return JsonResponse([], safe=False)
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


def order_confirm(request, pk):
    if not request.user.is_staff:
        return redirect('home')
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


def order_dispatch(request, pk):
    if not request.user.is_staff:
        return redirect('home')
    if request.method != 'POST':
        return redirect('order_dashboard')
    order = get_object_or_404(Order, pk=pk)
    if order.status == 'in_production':
        order.status = 'completed'
        order.save(update_fields=['status'])
        messages.success(request, f"ORD-{order.order_no:04d} marked as dispatched.")
    return redirect('order_dashboard')


def order_reject(request, pk):
    if not request.user.is_staff:
        return redirect('home')
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


def send_quote_email(request, pk):
    if not request.user.is_staff:
        return redirect('home')
    if request.method != 'POST':
        return redirect('order_dashboard')

    customer = get_object_or_404(Customer, pk=pk)

    if not customer.email:
        messages.error(request, f"No email address on file for {customer.name}.")
        return redirect('order_dashboard')

    rate_per_kg = _parse_rate_per_kg(request.POST.get('rate_per_kg'))
    if rate_per_kg is None:
        messages.error(request, "Enter a valid rate per kg before sending the quote.")
        return redirect('order_dashboard')

    try:
        quotation = Quotation.objects.create(
            customer=customer, rate_per_kg=rate_per_kg,
            product_type_id=request.POST.get('product_type') or None,
            grade=request.POST.get('grade', '').strip(),
            size=request.POST.get('size') or None,
        )
    except (InvalidOperation, ValueError, ValidationError):
        messages.error(request, "Check that size is a valid number.")
        return redirect('order_dashboard')

    _dispatch_quote_email(request, customer, quotation)
    return redirect('order_dashboard')


def quick_send_quote(request):
    """Create/update a customer from name+email+phone and immediately send the quote form link."""
    if not request.user.is_staff:
        return redirect('home')
    if request.method != 'POST':
        return redirect('order_dashboard')

    name  = request.POST.get('name', '').strip()
    email = request.POST.get('email', '').strip()
    phone = request.POST.get('phone', '').strip()

    if not name:
        messages.error(request, "Company name is required.")
        return redirect('order_dashboard')
    if not email:
        messages.error(request, "Email address is required to send the form.")
        return redirect('order_dashboard')

    rate_per_kg = _parse_rate_per_kg(request.POST.get('rate_per_kg'))
    if rate_per_kg is None:
        messages.error(request, "Enter a valid rate per kg before sending the quote.")
        return redirect('order_dashboard')

    customer, _ = Customer.objects.get_or_create(name=name)
    customer.email = email
    if phone:
        customer.phone = phone
    customer.save(update_fields=['email', 'phone'])

    try:
        quotation = Quotation.objects.create(
            customer=customer, rate_per_kg=rate_per_kg,
            product_type_id=request.POST.get('product_type') or None,
            grade=request.POST.get('grade', '').strip(),
            size=request.POST.get('size') or None,
        )
    except (InvalidOperation, ValueError, ValidationError):
        messages.error(request, "Check that size is a valid number.")
        return redirect('order_dashboard')

    _dispatch_quote_email(request, customer, quotation)
    return redirect('order_dashboard')


def _public_quote_base_url(request):
    """The origin (scheme+host, no trailing slash) that any customer-facing
    quote link must be built from. Admins only ever reach this app over
    Tailscale — building a link from this request's own host would hand an
    external customer a private address they can never open. Used for both
    the emailed link (_dispatch_quote_email) and the dashboards' "Copy
    Link" fallback, which previously built its URL from request.get_host()
    directly and inherited that same bug."""
    return settings.PUBLIC_QUOTE_BASE_URL or request.build_absolute_uri('/').rstrip('/')


def _dispatch_quote_email(request, customer, quotation):
    """Emails the official quotation PDF, followed by the order-form link,
    to customer.email. Adds a Django message for success/failure."""
    if not settings.EMAIL_HOST_USER:
        messages.error(request, "Email is not configured — set EMAIL_HOST, EMAIL_HOST_USER, and EMAIL_HOST_PASSWORD in your .env file.")
        return

    quote_path = reverse('quote_form', kwargs={'token': customer.quote_token})
    quote_url = f"{_public_quote_base_url(request)}{quote_path}"
    try:
        pdf_bytes = generate_quotation_pdf(quotation)
        email = EmailMessage(
            subject=f"Quotation {quotation.formatted_no()} — {settings.COMPANY_NAME}",
            body=(
                f"Dear {customer.name},\n\n"
                f"Please find attached our official quotation {quotation.formatted_no()} "
                f"for your requirement.\n\n"
                f"If you wish to proceed, please log your order using the link below — "
                f"you're welcome to attach your own Purchase Order there too, if you have one:\n\n"
                f"{quote_url}\n\n"
                f"This link is unique to your company.\n\n"
                f"Regards,\n{settings.COMPANY_NAME}"
            ),
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=[customer.email],
        )
        email.attach(f"{quotation.formatted_no()}.pdf", pdf_bytes, "application/pdf")
        email.send()
        messages.success(request, f"Quotation {quotation.formatted_no()} sent to {customer.email}.")
    except Exception as e:
        messages.error(request, f"Failed to send email: {e}")


def quotation_pdf(request, pk):
    """Standalone download of a quotation's PDF — used for the Copy Link
    fallback (no email on file to attach it to) and for staff wanting to
    re-download/print one already sent."""
    if not request.user.is_staff:
        return redirect('home')
    quotation = get_object_or_404(Quotation, pk=pk)
    pdf_bytes = generate_quotation_pdf(quotation)
    response = HttpResponse(pdf_bytes, content_type='application/pdf')
    response['Content-Disposition'] = f'attachment; filename="{quotation.formatted_no()}.pdf"'
    return response


# ── WhatsApp webhook (public, unauthenticated) ─────────────────────

WHATSAPP_GRAPH_API_VERSION = "v21.0"
WHATSAPP_QUERY_INTAKE_TEMPLATE = "matta_drawing_query_intake"
# Meta templates are keyed by the exact language code they were approved
# under — picking "English" (not "English (US)") in WhatsApp Manager
# approves the template as "en", not "en_US". Sending with the wrong code
# fails with a "template does not exist" error even though the template
# itself exists and is approved.
WHATSAPP_QUERY_INTAKE_TEMPLATE_LANGUAGE = "en"
# Fixed intake order — the next question is whichever of these is still
# blank on the Query, so there's no separate "stage" field to drift out of
# sync with the actual data.
WHATSAPP_QUERY_FIELDS = ['company_name', 'contact_email', 'grade', 'size']
WHATSAPP_QUERY_QUESTIONS = {
    'contact_email': "Thanks! What's the best email address to send your quote to?",
    'grade': "Got it. Which grade do you need (e.g. EN8D, EN9)?",
    'size': "And what size do you need (in mm), e.g. 1.2?",
}
WHATSAPP_CLOSING_MESSAGE = (
    "Thanks - that's everything we need for now. Our team will get back to "
    "you shortly with your quote."
)


class WhatsAppSendError(Exception):
    """Raised by the send helpers below on any failure — missing config,
    network error, or a non-2xx response from the Graph API. Callers
    decide how to degrade; nothing here is allowed to propagate into a
    crash (a failed outbound send should never lose an already-saved
    answer or block a query from being created)."""


def _whatsapp_graph_request(payload):
    phone_number_id = settings.WHATSAPP_PHONE_NUMBER_ID
    access_token = settings.WHATSAPP_ACCESS_TOKEN
    if not phone_number_id or not access_token:
        raise WhatsAppSendError("WhatsApp sending is not configured (missing access token/phone number ID).")

    url = f"https://graph.facebook.com/{WHATSAPP_GRAPH_API_VERSION}/{phone_number_id}/messages"
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status >= 300:
                raise WhatsAppSendError(f"WhatsApp API returned HTTP {resp.status}")
    except urllib.error.HTTPError as e:
        raise WhatsAppSendError(f"WhatsApp API error {e.code}: {e.read().decode(errors='replace')}") from e
    except (urllib.error.URLError, OSError) as e:
        raise WhatsAppSendError(f"WhatsApp API request failed: {e}") from e


def _send_whatsapp_template_message(phone, template_name, language="en_US"):
    """The first message to a number that hasn't messaged us (or has gone
    quiet 24h+) must be a pre-approved template — Meta rejects free-form
    text otherwise. `template_name` must already be approved in Meta
    Business Manager."""
    _whatsapp_graph_request({
        "messaging_product": "whatsapp", "to": phone, "type": "template",
        "template": {"name": template_name, "language": {"code": language}},
    })


def _send_whatsapp_text_message(phone, text):
    """Free-form follow-up — only usable once the customer has replied at
    least once within the last 24 hours."""
    _whatsapp_graph_request({
        "messaging_product": "whatsapp", "to": phone, "type": "text", "text": {"body": text},
    })


def _send_whatsapp_text_message_background(phone, text):
    """Fire-and-forget a follow-up question from inside the webhook. Meta
    expects a fast ack on every delivery — waiting on the Graph API's own
    network round trip before responding risks a slow/timed-out ack, which
    is exactly what triggers Meta to redeliver the same message. The
    answer that triggered this send is already saved by the time this
    runs, so a failed send here only means a delayed follow-up question,
    never lost data — logged (not silently swallowed) so a persistent
    failure, e.g. an expired access token, is actually visible."""
    def _send():
        try:
            _send_whatsapp_text_message(phone, text)
        except WhatsAppSendError as e:
            logger.warning("WhatsApp follow-up send to %s failed: %s", phone, e)
    thread = threading.Thread(target=_send, daemon=True)
    thread.start()
    return thread


def _next_expected_query_field(query):
    """The next blank field in the fixed intake order, or None once
    company_name/contact_email/grade/size are all filled."""
    for field in WHATSAPP_QUERY_FIELDS:
        value = getattr(query, field)
        if field == 'size':
            if value is None:
                return field
        elif not value:
            return field
    return None


def _normalize_phone(phone):
    """Digits only — matches the format Meta's Cloud API uses for
    msg['from'] (no '+', spaces, hyphens, or parens), so a number staff
    type in any human format (+91 98765 43210, 98765-43210, ...) still
    matches the customer's later WhatsApp replies exactly."""
    return re.sub(r'\D', '', phone or '')


def _parse_whatsapp_size(text):
    """Lenient size parsing — "1.2", "1.2mm", "1.2 mm" all become
    Decimal('1.2'); anything with no usable digits, or a zero/negative
    result, returns None so the caller can re-ask instead of saving a
    coil size that could never be real."""
    cleaned = re.sub(r'[^0-9.\-]', '', text or '')
    if not cleaned:
        return None
    try:
        value = Decimal(cleaned)
    except InvalidOperation:
        return None
    return value if value > 0 else None


def _is_valid_whatsapp_email(text):
    """Reject an obviously-wrong reply at the email step (a typo, or an
    answer to the wrong question) rather than saving it and only finding
    out much later when query_send_quote tries to actually mail it."""
    try:
        validate_email(text)
        return True
    except ValidationError:
        return False


@csrf_exempt
@require_http_methods(["GET", "POST"])
def whatsapp_webhook(request):
    """Meta WhatsApp Cloud API webhook. GET is the one-time subscription
    handshake Meta performs when the webhook URL is registered; POST
    delivers inbound message events. csrf_exempt because Meta's servers
    never carry a Django session/CSRF cookie — HMAC verification of
    X-Hub-Signature-256 (POST) and the verify-token check (GET) are the
    real authentication here, not Django's CSRF protection."""
    if request.method == "GET":
        return _whatsapp_verify(request)
    return _whatsapp_receive(request)


def _whatsapp_verify(request):
    mode = request.GET.get("hub.mode")
    token = request.GET.get("hub.verify_token", "")
    challenge = request.GET.get("hub.challenge", "")
    # An unset WHATSAPP_VERIFY_TOKEN must never "verify" anything — without
    # this check, a blank token setting would match a request that also
    # omits hub.verify_token ('' == ''), passing a handshake that verified
    # nothing at all.
    if (
        settings.WHATSAPP_VERIFY_TOKEN
        and mode == "subscribe"
        and constant_time_compare(token, settings.WHATSAPP_VERIFY_TOKEN)
    ):
        return HttpResponse(challenge, content_type="text/plain")
    return HttpResponseForbidden("Verification failed")


def _whatsapp_receive(request):
    # Signature check happens before anything else touches the body — an
    # invalid/missing signature means nothing here is trusted, so nothing
    # gets parsed or written.
    signature = request.headers.get("X-Hub-Signature-256", "")
    if not _valid_whatsapp_signature(request.body, signature):
        return HttpResponseForbidden("Invalid signature")

    try:
        payload = json.loads(request.body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        # Malformed body from an already-authenticated sender — ack anyway
        # so Meta doesn't retry-storm; there's nothing usable to process.
        return HttpResponse(status=200)
    if not isinstance(payload, dict):
        # Syntactically valid JSON that isn't an object (e.g. "null", "5",
        # a bare array) — same "nothing usable, just ack" case as above.
        return HttpResponse(status=200)

    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            _process_whatsapp_change(change.get("value", {}))

    return HttpResponse(status=200)


def _valid_whatsapp_signature(raw_body, signature_header):
    # An unset WHATSAPP_APP_SECRET must never validate anything — HMAC
    # keyed with an empty string is a key anyone can also compute, which
    # would make the signature trivially forgeable rather than absent.
    if not settings.WHATSAPP_APP_SECRET or not signature_header.startswith("sha256="):
        return False
    provided = signature_header[len("sha256="):]
    expected = hmac.new(
        settings.WHATSAPP_APP_SECRET.encode("utf-8"), raw_body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(provided, expected)


def _process_whatsapp_change(value):
    """Meta posts both inbound messages and delivery-status receipts
    (`value['statuses']`) to this same webhook — only the former should
    ever touch a Query."""
    incoming_messages = value.get("messages")
    if not incoming_messages:
        return

    contacts = value.get("contacts", [])
    profile_name = contacts[0].get("profile", {}).get("name", "") if contacts else ""

    for msg in incoming_messages:
        phone = msg.get("from", "")
        if not phone:
            continue
        text = (msg.get("text") or {}).get("body", "").strip()
        if not text:
            continue  # non-text message types (image/audio/etc.) — not handled yet
        _route_whatsapp_message(phone, text, profile_name)


def _route_whatsapp_message(phone, text, profile_name):
    """Route an inbound message to whichever open Query is mid-intake for
    this phone number. A message from a number with no in-progress query —
    either a cold inbound message, or a reply after that query already
    converted/was marked not interested — still becomes a bare Query
    rather than being silently dropped."""
    phone = _normalize_phone(phone)
    query = (
        Query.objects
        .filter(contact_phone=phone)
        .exclude(status__in=['converted', 'not_interested'])
        .order_by('-created_at')
        .first()
    )
    if query:
        _process_whatsapp_answer(query.pk, text)
    else:
        Query.objects.create(source='whatsapp', company_name=profile_name, contact_phone=phone, notes=text)


def _process_whatsapp_answer(query_pk, text):
    """Save this message as the answer to whichever question is next in
    the intake sequence, then send the following question — or, once the
    sequence is complete, try to auto-match an existing ProductType and
    send the closing message. A message that arrives after the sequence
    is already done is just appended to notes, not mistaken for an answer.

    Re-fetches and locks the row inside a transaction (select_for_update)
    rather than trusting the caller's already-read Query instance — Meta
    can redeliver the same webhook, and two overlapping deliveries for the
    same phone must not both read the same "next field" and race each
    other into the wrong column. A no-op on SQLite (no row locking there),
    but real protection once/if this ever runs on Postgres."""
    with transaction.atomic():
        query = Query.objects.select_for_update().get(pk=query_pk)

        field = _next_expected_query_field(query)
        if field is None:
            stamp = timezone.now().strftime('%d %b %H:%M')
            query.notes = f"{query.notes}\n[{stamp}] {text}".strip()
            query.save(update_fields=['notes'])
            return

        if field == 'size':
            parsed = _parse_whatsapp_size(text)
            if parsed is None:
                _send_whatsapp_text_message_background(query.contact_phone, WHATSAPP_QUERY_QUESTIONS['size'])
                return
            query.size = parsed
        elif field == 'contact_email':
            candidate = text.strip()
            if not _is_valid_whatsapp_email(candidate):
                _send_whatsapp_text_message_background(query.contact_phone, WHATSAPP_QUERY_QUESTIONS['contact_email'])
                return
            query.contact_email = candidate
        else:
            setattr(query, field, text.strip())
        query.save(update_fields=[field])

        next_field = _next_expected_query_field(query)
        if next_field:
            _send_whatsapp_text_message_background(query.contact_phone, WHATSAPP_QUERY_QUESTIONS[next_field])
        else:
            if query.grade and query.size is not None:
                match = ProductType.objects.filter(grade__iexact=query.grade, size=query.size).first()
                if match:
                    query.product_type = match
                    query.save(update_fields=['product_type'])
            _send_whatsapp_text_message_background(query.contact_phone, WHATSAPP_CLOSING_MESSAGE)
