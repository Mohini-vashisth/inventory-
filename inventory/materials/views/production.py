"""Production jobs: the step-by-step progress screens and the read-only board."""

from django.shortcuts import render, redirect, get_object_or_404

from ..models import Material, ProcessStep, ProductionJob, StepLog, Order
from ..decorators import employee_required
from .common import _parse_coil_no, _safe_get


@employee_required
def job_detail(request, pk):
    job = get_object_or_404(
        ProductionJob.objects.select_related('pick__coil', 'product_type')
                             .prefetch_related('step_logs', 'product_type__steps'),
        pk=pk,
    )
    steps = list(job.product_type.steps.all())

    # Build latest log per step from prefetched data
    logs_by_step = {}
    for log in sorted(job.step_logs.all(), key=lambda entry: entry.timestamp, reverse=True):
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


@employee_required
def select_job_for_coil(request):
    """Gate into job_detail — progress can only be updated by scanning or
    typing the coil's own number, the same way an order can only be picked
    by scanning the coil being picked. production_board is read-only status
    now; this is the only path into actually updating a job."""

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


@employee_required
def production_board(request):
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
            for log in sorted(job.step_logs.all(), key=lambda entry: entry.timestamp, reverse=True):
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
