"""Employee PIN login/logout, the admin login page, and the public home page."""

from django.shortcuts import render, redirect
from django.contrib.auth import authenticate, login
from django.conf import settings
from django.core.cache import cache
from django.urls import reverse
from django.utils.crypto import constant_time_compare

from ..decorators import employee_required
from .common import _safe_next


def _check_shared_code(request, *, flag, code, key_prefix, wrong_message="Incorrect PIN."):
    """The shared-code login used by the employee PIN and the finance code: throttled per client IP (a wrong
    code counts; too many locks that IP out, even for the right code), and a right code sets the session flag.
    Returns an error message, or None when the code was right."""
    # REMOTE_ADDR only: X-Forwarded-For is client-supplied, so keying on it
    # would let an attacker dodge the lockout by rotating the header.
    key = f"{key_prefix}:{request.META.get('REMOTE_ADDR', '')}"
    if cache.get(key, 0) >= settings.EMPLOYEE_LOGIN_MAX_FAILURES:
        minutes = settings.EMPLOYEE_LOGIN_LOCKOUT_SECONDS // 60
        return f"Too many incorrect attempts. Try again in {minutes} minutes."
    if constant_time_compare(request.POST.get('pin', ''), code):
        cache.delete(key)
        request.session[flag] = True
        return None
    cache.add(key, 0, settings.EMPLOYEE_LOGIN_LOCKOUT_SECONDS)
    try:
        cache.incr(key)
    except ValueError:
        cache.set(key, 1, settings.EMPLOYEE_LOGIN_LOCKOUT_SECONDS)
    return wrong_message


def employee_login(request):
    next_url = _safe_next(request, request.GET.get('next') or request.POST.get('next'), reverse('employee'))
    if request.session.get('employee_auth'):
        return redirect(next_url)
    error = None
    if request.method == 'POST':
        error = _check_shared_code(request, flag='employee_auth', code=settings.EMPLOYEE_PIN,
                                   key_prefix='employee-login-failures')
        if error is None:
            return redirect(next_url)
    return render(request, 'materials/employee_login.html', {'error': error, 'next': next_url})


def finance_login(request):
    """The finance team's page is behind its own shared code, separate from the employee PIN."""
    next_url = _safe_next(request, request.GET.get('next') or request.POST.get('next'), reverse('finance'))
    if request.session.get('finance_auth'):
        return redirect(next_url)
    error = None
    if request.method == 'POST':
        error = _check_shared_code(request, flag='finance_auth', code=settings.FINANCE_CODE,
                                   key_prefix='finance-login-failures', wrong_message="Incorrect code.")
        if error is None:
            return redirect(next_url)
    return render(request, 'materials/finance_login.html', {'error': error, 'next': next_url})


def finance_logout(request):
    if request.method == 'POST':
        request.session.pop('finance_auth', None)   # only the finance flag; a staff login is untouched
    return redirect('finance_login')


def employee_logout(request):
    if request.method == 'POST':
        request.session.flush()
    return redirect('employee_login')


def welcome(request):
    """The first screen of the installed tablet app (the manifest's start_url): a welcome, then on to the PIN page.
    Opening it signs the employee session out, so the PIN is asked every time the app is opened (only the
    employee PIN flag goes; a staff admin login in the same browser is untouched)."""
    request.session.pop('employee_auth', None)
    return render(request, 'materials/welcome.html')


def home(request):
    return render(request, "home.html")


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


@employee_required
def employee_landing(request):
    return render(request, 'materials/employee_landing.html')
