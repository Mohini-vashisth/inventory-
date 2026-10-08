"""Order-first coil picking: choose an order, then scan/pick coils against it."""

from django.shortcuts import render, redirect, get_object_or_404
from django.db import transaction
from django.db.models import DecimalField, F, Sum, Value
from django.db.models.functions import Coalesce
from decimal import Decimal, InvalidOperation
from django.core.exceptions import ValidationError

from ..models import Material, OrderCoilPick, ProductionJob, StepLog, Order
from ..decorators import employee_required
from .common import _CoilOverCommitted, _parse_coil_no, _safe_get


@employee_required
def select_order(request):
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
    if not order.product_type.allowed_specs.exists():
        return True
    for spec in order.applicable_specs():
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
        for spec in (specs if specs is not None else order.applicable_specs()):
            grade_matches = not spec.grade or spec.grade.lower() == (coil.grade or '').lower()
            size_matches = not spec.size or spec.size == coil.size
            if grade_matches and size_matches:
                return spec.raw_material_ratio
    return Decimal('1')


@employee_required
def select_coil_for_order(request, order_pk):
    """The order's picking hub: shows how much raw material has been picked
    so far against how much the order needs, lists eligible coils to browse,
    and accepts a scanned/typed coil number to jump straight into picking it."""
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
        specs = order.applicable_specs()
        if order.product_type.allowed_specs.exists():
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
            coils_qs = coils_qs.filter(q) if specs else coils_qs.none()   # specs exist, but none is for this size

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


@employee_required
def pick_coil_for_order(request, order_pk, coil_pk):
    """Confirm-and-allocate screen for one coil against one order — mirrors
    material_form's single-entity-confirm pattern. Product type comes from
    the order, never resubmitted, so a tampered/stale form can't override it."""
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
