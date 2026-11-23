"""Dispatch: a tile and a page for the owners, the plant (employee portal) and finance. The pages are empty shells for
now (2026-11-23); the details are added one by one, each in the module of its area."""

from django.shortcuts import render
from django.urls import reverse

from ..decorators import employee_or_staff_required, finance_required, redirect_to_admin_login, staff_required


@staff_required(on_denied=redirect_to_admin_login)
def dispatch_planning(request):
    return render(request, 'materials/dispatch_page.html', {
        'title': 'Dispatch planning', 'back_url': reverse('staff_home'), 'back_label': '← Back to dashboard'})


@finance_required
def finance_dispatch(request):
    return render(request, 'materials/dispatch_page.html', {
        'title': 'Dispatch', 'back_url': reverse('finance'), 'back_label': '← Back to finance'})


@employee_or_staff_required
def employee_dispatch(request):
    return render(request, 'materials/employee_dispatch.html')
