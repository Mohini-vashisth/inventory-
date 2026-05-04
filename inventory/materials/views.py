from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth import authenticate, login
from django.db.models import Sum
import qrcode
import io
import base64
from .models import Material, CoilPart, ProductType, ProcessStep, ProductionJob, StepLog
from .forms import MaterialForm


def home(request):
    return render(request, "home.html")


def material_form(request):
    if request.method == "POST":
        form = MaterialForm(request.POST)
        if form.is_valid():
            coil = form.save()
            return redirect('coil_tag', pk=coil.coil_no)
    else:
        form = MaterialForm()

    last_material = Material.objects.order_by('-coil_no').first()
    next_coil = (last_material.coil_no + 1) if last_material else 1
    formatted_coil = f"COIL{next_coil:04d}"

    return render(request, "materials/material_form.html", {"coil_no": formatted_coil, "form": form})


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
    })


def admin_login(request):
    if request.method == "POST":
        username = request.POST.get("username")
        password = request.POST.get("password")
        user = authenticate(request, username=username, password=password)

        if user is not None:
            login(request, user)
            return redirect("/admin/")
        else:
            return render(request, "materials/admin_login.html", {"error": "Invalid credentials"})

    return render(request, "materials/admin_login.html")


# ── Coil parts ──────────────────────────────────────────────

def coil_parts(request, coil_pk):
    """List all parts cut from a coil + form to add a new part."""
    coil = get_object_or_404(Material, pk=coil_pk)
    parts = coil.parts.prefetch_related('jobs__product_type').order_by('created_at')
    product_types = ProductType.objects.all()

    total_used = float(parts.aggregate(total=Sum('weight'))['total'] or 0)
    coil_weight = float(coil.quantity or 0)
    remaining = coil_weight - total_used
    exhausted = coil_weight > 0 and remaining <= 0

    if request.method == 'POST':
        if exhausted:
            return redirect('coil_parts', coil_pk=coil.pk)

        suffix = request.POST.get('suffix', '').strip().upper()
        new_weight = request.POST.get('weight') or None
        product_type_id = request.POST.get('product_type')
        error = None

        if not suffix:
            error = "Part suffix cannot be empty."
        elif CoilPart.objects.filter(part_no=f"{coil.formatted_coil()}-{suffix}").exists():
            error = f"A part with suffix '{suffix}' already exists for this coil."
        elif new_weight and float(new_weight) > remaining:
            error = (
                f"Part weight ({float(new_weight):.3f} kg) exceeds the remaining coil weight "
                f"({remaining:.3f} kg). Reduce the weight or split into smaller parts."
            )
        else:
            part = CoilPart.objects.create(
                coil=coil,
                part_no=f"{coil.formatted_coil()}-{suffix}",
                weight=new_weight,
                length=request.POST.get('length') or None,
                cut_date=request.POST.get('cut_date') or None,
                notes=request.POST.get('notes', ''),
            )
            pt = get_object_or_404(ProductType, pk=product_type_id)
            job = ProductionJob.objects.create(
                part=part, product_type=pt, job_no='PENDING',
            )
            job.job_no = f"JOB-{job.pk:04d}"
            job.save(update_fields=['job_no'])
            for step in pt.steps.all():
                StepLog.objects.create(
                    job=job, step=step, status='pending',
                    updated_by=request.user if request.user.is_authenticated else None,
                )
            return redirect('coil_parts', coil_pk=coil.pk)

        return render(request, 'materials/coil_parts.html', {
            'coil': coil, 'parts': parts, 'product_types': product_types,
            'total_used': total_used, 'remaining': remaining,
            'exhausted': exhausted, 'error': error,
        })

    return render(request, 'materials/coil_parts.html', {
        'coil': coil, 'parts': parts, 'product_types': product_types,
        'total_used': total_used, 'remaining': remaining, 'exhausted': exhausted,
    })


# ── Production jobs ──────────────────────────────────────────

def job_detail(request, pk):
    """Step-ticking view for a production job."""
    job = get_object_or_404(
        ProductionJob.objects.select_related('part__coil', 'product_type')
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
        new_status = 'completed' if action == 'complete' else 'in_progress'
        step = get_object_or_404(ProcessStep, pk=step_id)

        if step.id not in unlocked_step_ids:
            return redirect('job_detail', pk=job.pk)

        StepLog.objects.create(
            job=job, step=step, status=new_status,
            updated_by=request.user if request.user.is_authenticated else None,
        )

        # Refresh logs to recalculate job status
        all_latest = {s.id: job.step_logs.filter(step=s).order_by('-timestamp').first()
                      for s in steps}
        statuses = [l.status for l in all_latest.values() if l]
        if all(s == 'completed' for s in statuses):
            job.status = 'completed'
        elif any(s == 'in_progress' for s in statuses):
            job.status = 'in_progress'
        job.save()

        return redirect('job_detail', pk=job.pk)

    return render(request, 'materials/job_detail.html', {
        'job': job,
        'steps': steps,
        'logs_by_step': logs_by_step,
        'unlocked_step_ids': unlocked_step_ids,
    })


def employee_landing(request):
    coils = Material.objects.order_by('-coil_no')
    all_jobs = ProductionJob.objects.select_related(
        'part__coil', 'product_type'
    ).order_by('-created_at')

    return render(request, 'materials/employee_landing.html', {
        'coils': coils,
        'active_jobs': all_jobs,
    })
