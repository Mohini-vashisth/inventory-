from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth import authenticate, login
from django.contrib import messages
from django.core.mail import send_mail
from django.conf import settings
from django.urls import reverse
from django.db import transaction
from django.db.models import Count, DecimalField, F, Sum, Value
from django.db.models.functions import Coalesce
from django.http import JsonResponse
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.crypto import constant_time_compare
import uuid
import qrcode
import io
import base64
from decimal import Decimal, InvalidOperation
from django.core.exceptions import ValidationError
from .models import GateEntry, GateEntryLot, Material, OrderCoilPick, GradeOption, SizeOption, ProductType, AllowedCoilSpec, ProcessStep, ProductionJob, StepLog, Customer, Order
from .forms import GateEntryForm, GateEntryLotForm, GateEntryLotFormSet, MaterialForm, OrderForm


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


def _ratio_for_coil(order, coil):
    """The AllowedCoilSpec.raw_material_ratio for whichever spec matches this
    coil's grade/size under the order's product type — same matching rule
    OrderCoilPick.output_equivalent() uses, so the "how much raw material
    does the order still need" figure stays consistent everywhere it's
    computed. Falls back to 1:1 if no spec matches."""
    if order.product_type:
        for spec in order.product_type.allowed_specs.all():
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

    # Filter by allowed specs if the order has a product type configured
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
            order_needs = float(order_remaining_output * _ratio_for_coil(order, coil))
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


# ── Orders ───────────────────────────────────────────────────

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
    for order in orders:
        order.low_stock = (
            order.status in ('confirmed', 'in_production')
            and order.has_sufficient_raw_material() is False
        )

    return render(request, 'materials/order_dashboard.html', {
        'orders': orders,
        'customers': customers,
        'product_types': product_types,
        'product_type_data': product_type_data,
        'error': error,
        'post': request.POST if error else {},
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
    error = None

    if request.method == 'POST':
        form = OrderForm(request.POST)
        if not form.is_valid():
            error = _first_form_error(form)
        else:
            order = form.save(commit=False)
            order.customer = customer
            order.status = 'pending'
            order.save()
            # Invalidate this link — regenerate token so the URL becomes a 404
            customer.quote_token = uuid.uuid4()
            customer.save(update_fields=['quote_token'])
            return render(request, 'materials/quote_submitted.html', {'customer': customer})

    return render(request, 'materials/quote_form.html', {
        'customer': customer,
        'product_types': product_types,
        'product_type_data': product_type_data,
        'error': error,
        'post': request.POST if error else {},
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

    _dispatch_quote_email(request, customer)
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

    customer, _ = Customer.objects.get_or_create(name=name)
    customer.email = email
    if phone:
        customer.phone = phone
    customer.save(update_fields=['email', 'phone'])

    _dispatch_quote_email(request, customer)
    return redirect('order_dashboard')


def _dispatch_quote_email(request, customer):
    """Send the quote form link to customer.email. Adds a Django message for success/failure."""
    if not settings.EMAIL_HOST_USER:
        messages.error(request, "Email is not configured — set EMAIL_HOST, EMAIL_HOST_USER, and EMAIL_HOST_PASSWORD in your .env file.")
        return

    quote_path = reverse('quote_form', kwargs={'token': customer.quote_token})
    # Admins only ever reach this app over Tailscale — building the link from
    # this request's own host would put that private address in a customer's
    # email. Use the configured public origin when set (see PUBLIC_QUOTE_BASE_URL).
    quote_url = (
        f"{settings.PUBLIC_QUOTE_BASE_URL}{quote_path}" if settings.PUBLIC_QUOTE_BASE_URL
        else request.build_absolute_uri(quote_path)
    )
    try:
        send_mail(
            subject="Quotation Request Form",
            message=(
                f"Dear {customer.name},\n\n"
                f"Please fill in your quotation requirements using the link below:\n\n"
                f"{quote_url}\n\n"
                f"This link is unique to your company and can be used for future requests as well.\n\n"
                f"Regards"
            ),
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[customer.email],
        )
        messages.success(request, f"Quote form sent to {customer.email}.")
    except Exception as e:
        messages.error(request, f"Failed to send email: {e}")
