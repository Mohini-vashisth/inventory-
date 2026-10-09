"""Production jobs: the step-by-step progress screens and the read-only board."""

import base64
import io

import qrcode
from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse

from .. import parts as parts_service
from ..decorators import employee_or_staff_required, employee_required
from ..models import Material, Order, ProcessStep, ProductionJob, ProductionPart
from ..parts import PartError
from .common import _parse_coil_no, _safe_get


def _job_queryset():
    return (ProductionJob.objects.select_related('pick__coil', 'product_type', 'order__customer')
            .prefetch_related('product_type__steps'))


def _step_rows(job, part):
    """What the step list of one part shows: each step with its latest log, whether it can be worked on now,
    and, for an "all parts together" step, which other parts are holding it up."""
    steps = list(job.product_type.steps.all())
    latest = part.latest_logs_by_step()
    unlocked = parts_service.unlocked_step_ids(part, steps)
    rows = []
    for step in steps:
        log = latest.get(step.id)
        waiting = parts_service.joined_step_waiting_on(job, step) if step.joins_parts and step.id in unlocked else []
        if step.joins_parts and step.id in unlocked and waiting:
            ready = False   # this part is ready, but others have not got here yet
        else:
            ready = step.id in unlocked
        rows.append({'step': step, 'log': log, 'unlocked': ready, 'waiting': waiting,
                     'open': ready and not (log and log.status == 'completed')})
    return rows


def _render_part(request, job, part, error=None):
    children = list(part.children.all())
    return render(request, 'materials/part_detail.html', {
        'job': job,
        'part': part,
        'rows': _step_rows(job, part),
        'children': children,
        'is_split': bool(children),
        'is_whole_coil': part.parent_id is None and not children,
        'has_parts': job.parts.count() > 1,
        'error': error,
        'max_parts': parts_service.MAX_PARTS_PER_SPLIT,
    })


def _handle_part_post(request, job, part):
    """Apply a Start / Complete / Split sent from a part's page. Returns (response or None, error text)."""
    step = _safe_get(ProcessStep.objects, request.POST.get('step_id'))
    action = request.POST.get('action')
    user = request.user if request.user.is_authenticated else None
    if step is None or action not in ('start', 'complete', 'split'):
        return redirect(request.path), None   # a stale or malformed form: nothing to do, nothing to say
    try:
        if action == 'split':
            children = parts_service.split_part(part, step, parts_service.parse_weights(request.POST.getlist('weight')), user)
            ids = ','.join(str(child.pk) for child in children)
            return redirect(f"{reverse('part_tags', kwargs={'job_pk': job.pk})}?parts={ids}&from={part.pk}"), None
        parts_service.apply_step_action(part, step, action, user)
    except PartError as problem:
        return None, str(problem)
    return redirect(request.path), None


@employee_required
def job_detail(request, pk):
    """A job's page. While the whole coil is still one part it is that part's step list (so scanning a coil
    goes straight to the steps, as before); once material has been split it is the overview of every part."""
    job = get_object_or_404(_job_queryset(), pk=pk)
    parts = list(job.parts.all())
    if len(parts) <= 1:
        part = job.root_part()
        error = None
        if request.method == 'POST':
            response, error = _handle_part_post(request, job, part)
            if response:
                return response
        return _render_part(request, job, part, error)

    steps = list(job.product_type.steps.all())
    tree = []

    def walk(part, depth):
        latest = part.latest_logs_by_step()
        done = sum(1 for step in steps if latest.get(step.id) and latest[step.id].status == 'completed')
        current = next((step for step in steps if not (latest.get(step.id) and latest[step.id].status == 'completed')), None)
        tree.append({'part': part, 'depth': depth, 'indent': depth * 1.25, 'done': done, 'total': len(steps),
                     'current': current, 'split': part.is_split(),
                     'status': latest[current.id].status if current and latest.get(current.id) else 'pending'})
        for child in sorted(part.children.all(), key=lambda c: c.label):
            walk(child, depth + 1)

    for root in [p for p in parts if p.parent_id is None]:
        walk(root, 0)
    return render(request, 'materials/job_overview.html', {
        'job': job,
        'tree': tree,
        'finished_weight': job.finished_weight(),
        'scrap_weight': job.scrap_weight(),
        'active_count': len(job.active_parts()),
    })


@employee_required
def part_detail(request, pk):
    """One part's step list: start / complete steps, split the material at a step that divides it."""
    part = get_object_or_404(ProductionPart.objects.select_related('job__product_type', 'job__pick__coil', 'job__order'), pk=pk)
    job = part.job
    error = None
    if request.method == 'POST':
        response, error = _handle_part_post(request, job, part)
        if response:
            return response
    return _render_part(request, job, part, error)


@employee_or_staff_required
def part_tags(request, job_pk):
    """Printable tags (a QR code of the part's label plus what it is) — for the parts just made by a split
    (?parts=ids), or for every part still being worked on."""
    job = get_object_or_404(_job_queryset(), pk=job_pk)
    wanted = [int(x) for x in request.GET.get('parts', '').split(',') if x.strip().isdigit()]
    parts = [p for p in job.parts.all() if (p.pk in wanted if wanted else (p.parent_id is not None and not p.is_split()))]
    parts.sort(key=lambda p: p.label)
    tags = []
    for part in parts:
        qr = qrcode.QRCode(box_size=6, border=2)
        qr.add_data(part.label)
        qr.make(fit=True)
        buf = io.BytesIO()
        qr.make_image(fill_color="black", back_color="white").save(buf, format='PNG')
        tags.append({'part': part, 'qr_b64': base64.b64encode(buf.getvalue()).decode()})
    return render(request, 'materials/part_tags.html', {'job': job, 'tags': tags, 'from_part': request.GET.get('from', '')})


@employee_required
def select_job_for_coil(request):
    """Gate into job_detail — progress can only be updated by scanning or
    typing the coil's own number, the same way an order can only be picked
    by scanning the coil being picked. production_board is read-only status
    now; this is the only path into actually updating a job."""

    scan_error = None
    jobs = None
    if request.method == 'POST':
        typed = (request.POST.get('coil_no') or '').strip()
        part = ProductionPart.objects.filter(label__iexact=typed).first() if typed else None
        if part is not None:   # a part's own printed tag (JOB-0012-A1)
            return redirect('part_detail', pk=part.pk)
        coil_no = _parse_coil_no(request.POST.get('coil_no'))
        coil = _safe_get(Material.objects, coil_no) if coil_no is not None else None
        if coil is None:
            scan_error = "Coil not found. Check the number (or the part tag) and try again."
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


@employee_required
def production_board(request):
    orders = (Order.objects
              .filter(status='in_production')
              .select_related('customer', 'product_type')
              .prefetch_related(
                  'jobs__pick__coil',
                  'jobs__product_type__steps',
                  'jobs__parts__step_logs__step',
              )
              .order_by('delivery_date'))

    board = []
    for order in orders:
        jobs_data = []
        for job in order.jobs.all():
            steps = list(job.product_type.steps.all())
            total = len(steps)
            rows = []
            for part in (job.active_parts() or [job.root_part()]):
                latest = part.latest_logs_by_step()
                completed = sum(1 for s in steps if latest.get(s.id) and latest[s.id].status == 'completed')
                current_step, current_status = None, 'completed'
                for step in steps:
                    log = latest.get(step.id)
                    if not log or log.status != 'completed':
                        current_step, current_status = step, log.status if log else 'pending'
                        break
                rows.append({
                    'label': part.label if len(job.parts.all()) > 1 else job.pick.coil.formatted_coil(),
                    'weight': part.weight, 'total': total, 'completed': completed,
                    'pct': int(completed / total * 100) if total > 0 else 0,
                    'current_step': current_step, 'current_status': current_status,
                })
            jobs_data.append({'job': job, 'rows': rows, 'multiple': len(rows) > 1})

        weight_cut = sum(float(jd['job'].pick.weight_allocated or 0) for jd in jobs_data)
        weight_needed = float(order.quantity or 0)
        board.append({
            'order': order,
            'jobs': jobs_data,
            'weight_cut': weight_cut,
            'weight_fulfilled': weight_needed > 0 and weight_cut >= weight_needed,
        })

    return render(request, 'materials/production_board.html', {'board': board})
