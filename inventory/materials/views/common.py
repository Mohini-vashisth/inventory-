"""Small helpers shared by several view modules."""

from django.utils.http import url_has_allowed_host_and_scheme

from ..models import ProductType
from ..product_codes import grade_key


def _match_product_type(grade, category=None):
    """The catalogue product code (ProductType) for this product type + grade, or
    None. A code depends on both: with a `category` (product type) given it must
    match that too; without one, the grade alone only counts when exactly one code
    has it — the same grade under two product types is ambiguous, so it matches
    nothing rather than guessing. Grade ignores case, spaces and punctuation (a
    typed "EN8D" is "EN-8D"). Size plays no part: width and thickness vary per order."""
    if not grade or not grade.strip():
        return None
    candidates = ProductType.objects.all()
    if category is not None:
        candidates = candidates.filter(category=category)
    key = grade_key(grade)
    matches = [c for c in candidates if grade_key(c.grade) == key]
    return matches[0] if len(matches) == 1 else None


class _CoilOverCommitted(Exception):
    """Raised inside pick_coil_for_order's atomic block when, after actually
    writing a new pick, the coil's total used weight now exceeds its
    quantity — catches two concurrent picks on the same coil that both
    passed the earlier read-based check against a stale "remaining" value
    before either had written anything."""


class _GateEntryOverCommitted(Exception):
    """Raised inside material_form's atomic block when, after actually
    writing a new coil, the gate entry now has more registered coils than
    invoiced — catches two concurrent registrations against the same gate
    entry's last remaining slot that both passed the earlier read-based
    check before either had written anything."""


def _safe_next(request, next_url, default):
    """Only follow `next` if it points back at this host — blocks open-redirect via a spoofed link."""
    if next_url and url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        return next_url
    return default


def _first_form_error(form):
    for errors in form.errors.values():
        return errors[0]
    return "Invalid order details."


def _safe_get(queryset, pk):
    """Look up a row by a pk that may be missing, empty, or malformed (e.g. raw POST data)
    without raising ValueError — Model.objects.filter(pk=...) still raises on a non-numeric pk."""
    if pk in (None, ''):
        return None
    try:
        return queryset.filter(pk=pk).first()
    except (ValueError, TypeError):
        return None


def _first_formset_error(formset, label=None):
    """The first error to show. With `label` ("Item") and more than one form, says which one: "Item 2: ..."."""
    if formset.non_form_errors():
        return formset.non_form_errors()[0]
    for number, form in enumerate(formset, start=1):
        for errors in form.errors.values():
            return f"{label} {number}: {errors[0]}" if label and len(formset.forms) > 1 else errors[0]
    return "Check the lot details below."


def _parse_coil_no(raw):
    """Accepts either the formatted tag text ("COIL0007") or a bare number,
    tolerant of surrounding whitespace/case — matches whatever a barcode
    scanner (which just types the QR's text into the input) sends."""
    raw = (raw or '').strip().upper()
    if raw.startswith('COIL'):
        raw = raw[4:]
    try:
        return int(raw)
    except ValueError:
        return None
