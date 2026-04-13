from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth import authenticate, login
from django.db.models import Sum
from datetime import datetime
import qrcode
import io
import base64
from .models import Material, CoilPart, ProductType, ProcessStep, ProductionJob, StepLog
from django.contrib.auth.decorators import login_required
from django.utils import timezone

from .models import Material


def admin_dashboard(request):
    total_materials = Material.objects.count()
    total_quantity = Material.objects.aggregate(Sum("quantity"))["quantity__sum"] or 0
    current_month = datetime.now().month
    materials_this_month = Material.objects.filter(date__month=current_month).count()

    context = {
        "total_materials": total_materials,
        "total_quantity": total_quantity,
        "materials_this_month": materials_this_month,
    }
    return render(request, "materials/admin_dashboard.html", context)


def material_table(request):
    materials = Material.objects.all()
    return render(request, "materials/material_table.html", {"materials": materials})


def home(request):
    return render(request, "home.html")


def material_form(request):
    if request.method == "POST":
        coil = Material.objects.create(       # ← capture the created object
            date=request.POST['date'],
            grade=request.POST['grade'],
            size=request.POST['size'],
            company=request.POST['company'],
            vendor=request.POST['vendor'],
            quantity=request.POST['quantity'],
            heat_no=request.POST['heat_no']
        )
        # Redirect to the tag page immediately after saving
        return redirect('coil_tag', pk=coil.coil_no)

    last_material = Material.objects.order_by('-coil_no').first()
    next_coil = (last_material.coil_no + 1) if last_material else 1
    formatted_coil = f"COIL{next_coil:04d}"

    return render(request, "index.html", {"coil_no": formatted_coil})


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
            return render(request, "admin_login.html", {"error": "Invalid credentials"})

    return render(request, "admin_login.html")


# ── Coil parts ──────────────────────────────────────────────

def coil_parts(request, coil_pk):
    """List all parts cut from a coil + form to add a new part."""
    coil = get_object_or_404(Material, pk=coil_pk)

    if request.method == 'POST':
        suffix = request.POST.get('suffix', '').strip().upper()
        CoilPart.objects.create(
            coil=coil,
            part_no=f"{coil.formatted_coil()}-{suffix}",
            weight=request.POST.get('weight') or None,
            length=request.POST.get('length') or None,
            cut_date=request.POST.get('cut_date') or None,
            notes=request.POST.get('notes', ''),
        )
        return redirect('coil_parts', coil_pk=coil.pk)

    parts = coil.parts.all().order_by('created_at')
    return render(request, 'materials/coil_parts.html', {
        'coil': coil,
        'parts': parts,
    })


# ── Production jobs ──────────────────────────────────────────

def create_job(request, part_pk):
    """Create a production job for a coil part."""
    part = get_object_or_404(CoilPart, pk=part_pk)
    product_types = ProductType.objects.all()

    if request.method == 'POST':
        pt = get_object_or_404(ProductType, pk=request.POST['product_type'])
        last = ProductionJob.objects.order_by('-id').first()
        job_no = f"JOB-{(last.id + 1 if last else 1):04d}"

        job = ProductionJob.objects.create(
            part=part,
            product_type=pt,
            job_no=job_no,
            notes=request.POST.get('notes', ''),
        )

        # Auto-create a StepLog row (pending) for every step in this product type
        for step in pt.steps.all():
            StepLog.objects.create(
                job=job,
                step=step,
                status='pending',
                updated_by=request.user if request.user.is_authenticated else None,
            )

        return redirect('job_detail', pk=job.pk)

    return render(request, 'materials/create_job.html', {
        'part': part,
        'product_types': product_types,
    })


def job_detail(request, pk):
    """Show all steps for a job and allow status updates."""
    job = get_object_or_404(ProductionJob, pk=pk)

    # Get latest log per step (one row per step)
    steps = job.product_type.steps.all()
    step_status = {}
    for step in steps:
        latest = job.step_logs.filter(step=step).order_by('-timestamp').first()
        step_status[step.id] = latest

    if request.method == 'POST':
        step_id = request.POST.get('step_id')
        new_status = request.POST.get('status')
        notes = request.POST.get('notes', '')
        step = get_object_or_404(ProcessStep, pk=step_id)

        StepLog.objects.create(
            job=job,
            step=step,
            status=new_status,
            updated_by=request.user if request.user.is_authenticated else None,
            notes=notes,
        )

        # Update overall job status automatically
        all_logs = {s.id: job.step_logs.filter(step=s).order_by('-timestamp').first()
                    for s in steps}
        statuses = [l.status for l in all_logs.values() if l]
        if all(s == 'completed' for s in statuses):
            job.status = 'completed'
        elif any(s == 'in_progress' for s in statuses):
            job.status = 'in_progress'
        elif any(s == 'failed' for s in statuses):
            job.status = 'on_hold'
        job.save()

        return redirect('job_detail', pk=job.pk)

    return render(request, 'materials/job_detail.html', {
        'job': job,
        'steps': steps,
        'step_status': step_status,
    })


def tracking_dashboard(request):
    """Overview of all active jobs."""
    jobs = ProductionJob.objects.select_related(
        'part__coil', 'product_type'
    ).order_by('-created_at')

    return render(request, 'materials/tracking_dashboard.html', {'jobs': jobs})
def employee_landing(request):
    coils = Material.objects.order_by('-coil_no')
    active_jobs = ProductionJob.objects.exclude(
        status='completed'
    ).select_related('part__coil', 'product_type').order_by('-created_at')

    return render(request, 'materials/employee_landing.html', {
        'coils': coils,
        'active_jobs': active_jobs,
    })