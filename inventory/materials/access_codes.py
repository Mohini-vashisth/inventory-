"""The shared codes for the employee portal (the PIN) and the finance page, and who is signed in with them.

Until an owner changes a code from the owners' dashboard it is the value in the `.env` file (`EMPLOYEE_PIN`,
`FINANCE_CODE`). The first change stores the new code, scrambled, in an `AccessCode` row, and from then on that row
wins. Every change adds 1 to the row's `version`. A browser signed in with a code keeps a token naming the version it
was given, so changing the code signs everybody out at once: their token no longer matches.
"""

import re

from django.conf import settings
from django.contrib.auth.hashers import check_password, make_password
from django.db import transaction
from django.utils import timezone
from django.utils.crypto import constant_time_compare

from .models import AccessCode

ROLES = ('employee', 'finance')
LABELS = {'employee': 'Employee PIN', 'finance': 'Finance code'}
NOUNS = {'employee': 'employee PIN', 'finance': 'finance code'}   # for the middle of a sentence
SESSION_FLAGS = {'employee': 'employee_auth', 'finance': 'finance_auth'}
CODE_PATTERN = re.compile(r'^[0-9]{4}$')


def _env_code(role):
    return settings.EMPLOYEE_PIN if role == 'employee' else settings.FINANCE_CODE


def current_row(role):
    return AccessCode.objects.filter(role=role).first()


def code_matches(role, typed):
    """Is `typed` the code now in force for `role` (the saved one, else the `.env` value)?"""
    row = current_row(role)
    if row is not None:
        return check_password(typed, row.code_hash)
    return constant_time_compare(typed, _env_code(role))


def session_token(role):
    """What a browser signing in with this role's code is given: it names the code's current version."""
    row = current_row(role)
    return f"v{row.version}" if row else 'v0'


def session_valid(request, role):
    """Is this browser signed in with the code that is in force now? A sign-in from before the code was last changed
    holds an older version and is refused. (Sign-ins that predate this feature hold a plain True, which counts as
    version 0, so they stay valid until the first change.)"""
    value = request.session.get(SESSION_FLAGS[role])
    token = session_token(role)
    return value == token or (value is True and token == 'v0')


def validate_new_code(role, new_code):
    """An error message if `new_code` can't be the code for `role`, otherwise None: exactly 4 digits, and not the
    code the other role has (the login page tells the roles apart by the code typed)."""
    if not CODE_PATTERN.match(new_code or ''):
        return "The code must be exactly 4 digits."
    other = next(r for r in ROLES if r != role)
    if code_matches(other, new_code):
        return f"That code is already the {LABELS[other]}. The two codes must be different."
    return None


def set_code(role, new_code, user=None):
    """Save `new_code` as the code for `role` and sign everybody out who is signed in with the old one. Returns
    False (and changes nothing) when it is already the code, so nobody is signed out for no reason."""
    if code_matches(role, new_code):
        return False
    with transaction.atomic():
        row = current_row(role)
        version = row.version + 1 if row else 1
        AccessCode.objects.update_or_create(role=role, defaults={
            'code_hash': make_password(new_code), 'version': version,
            'changed_at': timezone.now(), 'changed_by': user})
    return True
