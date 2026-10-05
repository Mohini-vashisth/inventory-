from functools import wraps

from django.shortcuts import redirect
from django.urls import reverse


def _tag(wrapper, access):
    # Read by MaterialsRouteGuardTests, which fails for any routed view that
    # is neither tagged nor on its explicit public allowlist.
    wrapper.access = access
    return wrapper


def staff_required(view_func=None, *, on_denied=None):
    """Only `is_staff` users may reach the view. By default anyone else is
    sent home; pass `on_denied` (request -> response) for a different
    refusal, e.g. bouncing to the admin login or an empty JSON body."""
    def decorator(func):
        @wraps(func)
        def wrapper(request, *args, **kwargs):
            if not request.user.is_staff:
                return on_denied(request) if on_denied else redirect('home')
            return func(request, *args, **kwargs)
        return _tag(wrapper, 'staff')
    return decorator(view_func) if view_func else decorator


def redirect_to_admin_login(request):
    return redirect(f"{reverse('admin_login')}?next={request.path}")


def employee_required(view_func):
    """Only a browser that has entered the shared employee PIN may reach the
    view (session flag set by `employee_login`)."""
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if not request.session.get('employee_auth'):
            return redirect(f"{reverse('employee_login')}?next={request.path}")
        return view_func(request, *args, **kwargs)
    return _tag(wrapper, 'employee')
