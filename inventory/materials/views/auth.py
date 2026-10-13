"""Employee PIN login/logout, the admin login page, and the public home page."""

from django.shortcuts import render, redirect
from django.contrib.auth import authenticate, login
from django.conf import settings
from django.core.cache import cache
from django.urls import Resolver404, resolve, reverse
from urllib.parse import urlsplit
from django.utils.crypto import constant_time_compare

from ..decorators import employee_required, redirect_to_admin_login, staff_required
from .common import _safe_next


# Where each shared code lands, and the access tag its pages carry (see decorators.py).
ROLE_SESSION_FLAGS = {'employee': 'employee_auth', 'finance': 'finance_auth'}
ROLE_HOME_URLS = {'employee': 'employee', 'finance': 'finance'}


def _landing_for(request, role, requested):
    """Where a freshly signed-in `role` goes: the page they were trying to reach if their role may open it,
    otherwise their own dashboard. Whoever typed the code decides, not the `next` in the address."""
    next_url = _safe_next(request, requested, '')
    if next_url:
        try:
            match = resolve(urlsplit(next_url).path)
        except Resolver404:
            match = None
        if match is not None and getattr(match.func, 'access', None) == role:
            return next_url
    return reverse(ROLE_HOME_URLS[role])


def _check_code(request):
    """The one login for the shared codes. The typed code is compared with both the employee PIN and the finance
    code, and whichever it matches sets that role's session flag. Throttled per client IP (a wrong code counts;
    too many locks that IP out, even for a right code). Returns (role, error): a role when it matched, otherwise
    an error message. A wrong code gets one message whichever role it was meant for."""
    # REMOTE_ADDR only: X-Forwarded-For is client-supplied, so keying on it
    # would let an attacker dodge the lockout by rotating the header.
    key = f"login-failures:{request.META.get('REMOTE_ADDR', '')}"
    if cache.get(key, 0) >= settings.EMPLOYEE_LOGIN_MAX_FAILURES:
        minutes = settings.EMPLOYEE_LOGIN_LOCKOUT_SECONDS // 60
        return None, f"Too many incorrect attempts. Try again in {minutes} minutes."
    typed = request.POST.get('pin', '')
    # Both comparisons always run, so the time taken doesn't hint at which code is closer.
    is_employee = constant_time_compare(typed, settings.EMPLOYEE_PIN)
    is_finance = constant_time_compare(typed, settings.FINANCE_CODE)
    if is_employee or is_finance:
        role = 'employee' if is_employee else 'finance'
        cache.delete(key)
        request.session[ROLE_SESSION_FLAGS[role]] = True
        return role, None
    cache.add(key, 0, settings.EMPLOYEE_LOGIN_LOCKOUT_SECONDS)
    try:
        cache.incr(key)
    except ValueError:
        cache.set(key, 1, settings.EMPLOYEE_LOGIN_LOCKOUT_SECONDS)
    return None, "Incorrect code."


def code_login(request):
    """The login page for ground staff and the finance team: one code box, and the code decides the dashboard.
    (Owners use the separate username and password page, linked below the box.)"""
    requested = request.GET.get('next') or request.POST.get('next')
    for role, flag in ROLE_SESSION_FLAGS.items():
        if request.session.get(flag):
            return redirect(_landing_for(request, role, requested))
    error = None
    if request.method == 'POST':
        role, error = _check_code(request)
        if role:
            return redirect(_landing_for(request, role, requested))
    return render(request, 'materials/login.html', {'error': error, 'next': _safe_next(request, requested, '')})


def finance_logout(request):
    if request.method == 'POST':
        request.session.pop('finance_auth', None)   # only the finance flag; a staff login is untouched
    return redirect('login')


def employee_logout(request):
    if request.method == 'POST':
        request.session.flush()
    return redirect('login')


def welcome(request):
    """The first screen for everyone (the site's main address and the tablet app's start page): a welcome, then on
    to the code page. Opening it signs the employee and finance sessions out, so the code is asked every time
    (only those two flags go; a staff admin login in the same browser is untouched)."""
    request.session.pop('employee_auth', None)
    request.session.pop('finance_auth', None)
    return render(request, 'materials/welcome.html')


@staff_required(on_denied=redirect_to_admin_login)
def staff_home(request):
    """Where an owner lands after the admin login: Orders, Queries and the Django admin. The admin login is the
    one sign-in for all three, so none of them asks again."""
    return render(request, 'materials/staff_home.html')


def admin_login(request):
    next_url = _safe_next(request, request.GET.get('next') or request.POST.get('next'), reverse('staff_home'))
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
