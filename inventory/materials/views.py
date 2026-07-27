from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth import authenticate, login
from django.contrib import messages
from django.core.mail import send_mail
from django.conf import settings
from django.urls import reverse
from django.db import transaction
from django.db.models import Sum
from django.http import JsonResponse
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.crypto import constant_time_compare
import uuid
import qrcode
import io
import base64
from .models import Material, CoilPart, GradeOption, SizeOption, ProductType, AllowedCoilSpec, ProcessStep, ProductionJob, StepLog, Customer, Order
from .forms import MaterialForm, OrderForm


def _safe_next(request, next_url, default):
    """Only follow `next` if it points back at this host — blocks open-redirect via a spoofed link."""
    if next_url and url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        return next_url
    return default


def _first_form_error(form):
    for errors in form.errors.values():
        return errors[0]
    return "Invalid order details."


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


def material_form(request):
    guard = _employee_required(request)
    if guard: return guard
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

    return render(request, "materials/material_form.html", {
        "coil_no": formatted_coil,
        "form": form,
        "grades": GradeOption.objects.all(),
        "sizes": SizeOption.objects.all(),
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


# ── Coil parts ──────────────────────────────────────────────

def coil_parts(request, coil_pk):
    guard = _employee_required(request)
    if guard: return guard
    coil = get_object_or_404(Material, pk=coil_pk)
    parts = coil.parts.prefetch_related('jobs__product_type').order_by('created_at')
    product_types = ProductType.objects.all()

    # Optional: coming from order-first flow
    from_order_pk = request.GET.get('from_order') or request.POST.get('from_order')
    from_order = Order.objects.select_related('product_type', 'customer').filter(pk=from_order_pk).first() if from_order_pk else None

    total_used = float(parts.aggregate(total=Sum('weight'))['total'] or 0)
    coil_weight = float(coil.quantity or 0)
    remaining = coil_weight - total_used
    exhausted = coil_weight > 0 and remaining <= 0

    if request.method == 'POST':
        if exhausted:
            return redirect('coil_parts', coil_pk=coil.pk)

        suffix = request.POST.get('suffix', '').strip().upper()
        new_weight = request.POST.get('weight') or None
        # If coming from order flow, product type is fixed; otherwise use form selection
        product_type_id = (
            from_order.product_type.pk if from_order and from_order.product_type
            else request.POST.get('product_type')
        )
        pt = ProductType.objects.filter(pk=product_type_id).first()
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
        elif pt is None:
            error = "Please select a valid product type."
        else:
            with transaction.atomic():
                part = CoilPart.objects.create(
                    coil=coil,
                    part_no=f"{coil.formatted_coil()}-{suffix}",
                    weight=new_weight,
                    length=request.POST.get('length') or None,
                    cut_date=request.POST.get('cut_date') or None,
                    notes=request.POST.get('notes', ''),
                )
                job = ProductionJob.objects.create(
                    part=part, product_type=pt, job_no='PENDING',
                    order=from_order,
                )
                job.job_no = f"JOB-{job.pk:04d}"
                job.save(update_fields=['job_no'])
                for step in pt.steps.all():
                    StepLog.objects.create(
                        job=job, step=step, status='pending',
                        updated_by=request.user if request.user.is_authenticated else None,
                    )
                # Mark order as in production when first part is cut for it
                if from_order and from_order.status == 'confirmed':
                    from_order.status = 'in_production'
                    from_order.save(update_fields=['status'])
            return redirect('coil_parts', coil_pk=coil.pk)

        return render(request, 'materials/coil_parts.html', {
            'coil': coil, 'parts': parts, 'product_types': product_types,
            'total_used': total_used, 'remaining': remaining,
            'exhausted': exhausted, 'error': error,
            'from_order': from_order,
        })

    return render(request, 'materials/coil_parts.html', {
        'coil': coil, 'parts': parts, 'product_types': product_types,
        'total_used': total_used, 'remaining': remaining, 'exhausted': exhausted,
        'from_order': from_order,
    })


# ── Order-first part creation flow ───────────────────────────

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


def select_coil_for_order(request, order_pk):
    guard = _employee_required(request)
    if guard: return guard
    order = get_object_or_404(
        Order.objects.select_related('customer', 'product_type'),
        pk=order_pk,
    )

    coils_qs = Material.objects.annotate(weight_used=Sum('parts__weight'))

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

    # Only coils with remaining weight
    coils = []
    for coil in coils_qs.order_by('-coil_no'):
        used      = float(coil.weight_used or 0)
        total     = float(coil.quantity or 0)
        remaining = total - used
        if remaining > 0:
            coils.append({
                'coil': coil,
                'remaining': remaining,
                'total': total,
                'pct_used': int((used / total * 100)) if total > 0 else 0,
            })

    return render(request, 'materials/select_coil_for_order.html', {
        'order': order,
        'coils': coils,
    })


# ── Production jobs ──────────────────────────────────────────

def job_detail(request, pk):
    guard = _employee_required(request)
    if guard: return guard
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
        if action not in ('start', 'complete'):
            return redirect('job_detail', pk=job.pk)
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


def production_board(request):
    guard = _employee_required(request)
    if guard: return guard
    orders = (Order.objects
              .filter(status='in_production')
              .select_related('customer', 'product_type')
              .prefetch_related(
                  'jobs__part__coil',
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

        weight_cut = sum(float(jd['job'].part.weight or 0) for jd in jobs_data)
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
              .annotate(weight_cut=Sum('jobs__part__weight'))
              .order_by('-created_at'))
    customers = Customer.objects.order_by('name')
    product_types = ProductType.objects.order_by('name')
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
    order = get_object_or_404(Order, pk=pk)
    if not order.product_type_id:
        messages.error(request, f"ORD-{order.pk:04d} cannot be confirmed without a product type. Edit the order to assign one.")
        return redirect('order_dashboard')
    order.status = 'confirmed'
    order.save(update_fields=['status'])
    messages.success(request, f"ORD-{order.pk:04d} confirmed.")
    return redirect('order_dashboard')


def order_dispatch(request, pk):
    if not request.user.is_staff:
        return redirect('home')
    order = get_object_or_404(Order, pk=pk)
    if order.status == 'in_production':
        order.status = 'completed'
        order.save(update_fields=['status'])
        messages.success(request, f"ORD-{order.pk:04d} marked as dispatched.")
    return redirect('order_dashboard')


def order_reject(request, pk):
    if not request.user.is_staff:
        return redirect('home')
    order = get_object_or_404(Order, pk=pk)
    order.status = 'cancelled'
    order.save(update_fields=['status'])
    return redirect('order_dashboard')


def quote_form(request, token):
    customer = get_object_or_404(Customer, quote_token=token)
    product_types = ProductType.objects.order_by('name')
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

    quote_url = request.build_absolute_uri(
        reverse('quote_form', kwargs={'token': customer.quote_token})
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
