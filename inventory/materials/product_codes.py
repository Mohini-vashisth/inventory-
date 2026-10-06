"""Product codes: product type + grade, written as e.g. FBB009.

    FBB      the product type's 3 letters (Flat Bright Bar)
    009      the grade's number (EN-8D)

Size is not part of a code: width and thickness vary per order, so they are
recorded on the query, quote line and order instead.

This is the only place that knows the format. Codes are created by the admin in
the Django admin (the Add product code form fills Item Code from these two and a
script keeps it live); quotes only ever *look codes up* and refuse to be sent
until the code for each item exists.
"""
import re

from django.db.models import Max

from .models import GradeOption, ProductType

MAX_GRADE_NUMBER = 999


def grade_key(grade):
    """What makes two spellings the same grade: case, spaces and punctuation are
    ignored, so "EN8D", "en-8d" and "EN 8D" are one grade ("EN-8D CR" stays
    different from "EN-8D"). The browser scripts apply the same rule."""
    return re.sub(r'[^a-z0-9]', '', (grade or '').lower())


def find_grade_option(grade):
    """The listed GradeOption this grade is a spelling of, or None."""
    key = grade_key(grade)
    if not key:
        return None
    return next((o for o in GradeOption.objects.order_by('pk') if grade_key(o.name) == key), None)


def find_product_code(category, grade, exclude_pk=None):
    """The existing code for this type + grade, whichever way the grade is spelled."""
    key = grade_key(grade)
    candidates = ProductType.objects.exclude(pk=exclude_pk).filter(category=category)
    return next((c for c in candidates if grade_key(c.grade) == key), None)


def canonical_grade(grade):
    """The grade as it's spelled in the grade list when it is a spelling of a listed
    one ("en8d" or "EN8D" -> "EN-8D"), otherwise the text as typed (trimmed). Keeps
    one grade from being written several ways, without ever refusing a new one."""
    text = (grade or '').strip()
    option = find_grade_option(text)
    return option.name if option else text


def _next_grade_number():
    return (GradeOption.objects.aggregate(Max('number'))['number__max'] or 0) + 1


def _why_not(category, grade):
    """Why a code can't be generated for this combination, or '' if it can."""
    if not category.code:
        return f"The product type {category.name} has no 3-letter code yet - set it in the admin."
    if len(grade.strip()) > 20:
        return "The grade is longer than 20 characters."
    existing = find_grade_option(grade)
    if (existing is None or existing.number is None) and _next_grade_number() > MAX_GRADE_NUMBER:
        return "All 999 grade numbers are in use."
    return ""


def _build(category, grade_number):
    return f"{category.code}{grade_number:03d}"


def describe_product_code(category, grade):
    """What the quote form shows once type and grade are filled in:
    {'exists', 'item_code', 'reason'} - the existing code, or the code that
    sending the quote would create, or why none can be."""
    if not (category and grade and grade.strip()):
        return {'exists': False, 'item_code': None, 'reason': ''}
    existing = find_product_code(category, grade)
    if existing:
        return {'exists': True, 'item_code': existing.item_code, 'reason': ''}
    reason = _why_not(category, grade)
    if reason:
        return {'exists': False, 'item_code': None, 'reason': reason}
    option = find_grade_option(grade)
    number = option.number if option and option.number is not None else _next_grade_number()
    return {'exists': False, 'item_code': _build(category, number), 'reason': ''}


def _grade_number(grade):
    """The grade's number, adding the grade to the list and numbering it the
    first time it is used. Returns (GradeOption, number)."""
    name = grade.strip()
    option = find_grade_option(name) or GradeOption(name=name)
    if option.number is None:
        option.number = _next_grade_number()
        option.save()
    return option, option.number


def _unique_item_code(base, exclude_pk=None):
    """`base`, or `base-2`, `base-3`... if a hand-made code already uses that text."""
    item_code, suffix = base, 1
    while ProductType.objects.exclude(pk=exclude_pk).filter(item_code=item_code).exists():
        suffix += 1
        item_code = f"{base}-{suffix}"
    return item_code


def item_code_for(category, grade, exclude_pk=None):
    """For the admin's Add/Change product code form when Item Code is left blank:
    (item_code, '') for the code to use, or (None, reason). A combination that
    already has a code is refused rather than duplicated (`exclude_pk` is the code
    being edited, which doesn't count as a duplicate of itself)."""
    if not (category and grade and grade.strip()):
        return None, "Pick a product type and fill in the grade, or type an item code yourself."
    existing = find_product_code(category, grade, exclude_pk)
    if existing:
        return None, f"A product code for this type and grade already exists: {existing.item_code}."
    reason = _why_not(category, grade)
    if reason:
        return None, reason
    option = find_grade_option(grade)
    number = option.number if option and option.number is not None else _next_grade_number()
    return _unique_item_code(_build(category, number), exclude_pk), ""


def reserve_grade_number(grade):
    """Add the grade to the list and number it if it isn't yet — called after a code
    using it is saved, so the number in that code is now taken and the next new
    grade gets the one after."""
    _grade_number(grade)
