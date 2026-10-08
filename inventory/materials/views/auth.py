"""Employee PIN login/logout, the admin login page, and the public home page."""

from django.shortcuts import render, redirect
from django.contrib.auth import authenticate, login
from django.conf import settings
from django.core.cache import cache
from django.urls import reverse
from django.utils.crypto import constant_time_compare

from ..decorators import employee_required
from .common import _safe_next


def _employee_login_cache_key(request):
    # REMOTE_ADDR only: X-Forwarded-For is client-supplied, so keying on it
    # would let an attacker dodge the lockout by rotating the header.
    return f"employee-login-failures:{request.META.get('REMOTE_ADDR', '')}"


def employee_login(request):
    next_url = _safe_next(request, request.GET.get('next') or request.POST.get('next'), reverse('employee'))
    if request.session.get('employee_auth'):
        return redirect(next_url)
    error = None
    if request.method == 'POST':
        key = _employee_login_cache_key(request)
        if cache.get(key, 0) >= settings.EMPLOYEE_LOGIN_MAX_FAILURES:
            minutes = settings.EMPLOYEE_LOGIN_LOCKOUT_SECONDS // 60
            error = f"Too many incorrect attempts. Try again in {minutes} minutes."
        elif constant_time_compare(request.POST.get('pin', ''), settings.EMPLOYEE_PIN):
            cache.delete(key)
            request.session['employee_auth'] = True
            return redirect(next_url)
        else:
            cache.add(key, 0, settings.EMPLOYEE_LOGIN_LOCKOUT_SECONDS)
            try:
                cache.incr(key)
            except ValueError:
                cache.set(key, 1, settings.EMPLOYEE_LOGIN_LOCKOUT_SECONDS)
            error = "Incorrect PIN."
    return render(request, 'materials/employee_login.html', {'error': error, 'next': next_url})


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
