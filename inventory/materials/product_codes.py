"""Product codes: product type + grade + size, written as e.g. FBB00100120.

    FBB      the product type's 3 letters (Flat Bright Bar)
    001      the grade's number (EN8D)
    00120    the size in hundredths of a mm (1.2 mm)

This is the only place that knows the format. Codes are created by the admin in
the Django admin (the Add product code form fills Item Code from these three
and a script keeps it live); quotes only ever *look codes up* and refuse to be
sent until the code for each item exists.
"""
from decimal import Decimal, InvalidOperation

from django.db.models import Max

from .models import GradeOption, ProductType

MAX_GRADE_NUMBER = 999
MAX_SIZE_HUNDREDTHS = 99999   # 999.99 mm


def size_digits(size):
    """The 5-digit size part (1.2 mm -> '00120'), or None when the size can't be
    written exactly: not positive, over 999.99 mm, or finer than 0.01 mm (two
    different sizes would otherwise share a code)."""
    if size is None:
        return None
    try:
        hundredths = Decimal(size) * 100
    except (InvalidOperation, TypeError, ValueError):
        return None
    if hundredths != hundredths.to_integral_value() or not (0 < hundredths <= MAX_SIZE_HUNDREDTHS):
        return None
    return f"{int(hundredths):05d}"


def canonical_grade(grade):
    """The grade as it's spelled in the grade list when it matches one regardless of
    case ("en8d" -> "EN8D"), otherwise the text as typed (trimmed). Keeps one
    grade from being written several ways, without ever refusing a new one."""
    text = (grade or '').strip()
    option = GradeOption.objects.filter(name__iexact=text).first() if text else None
    return option.name if option else text


def _next_grade_number():
    return (GradeOption.objects.aggregate(Max('number'))['number__max'] or 0) + 1


def _find_code(category, grade, size):
    return ProductType.objects.filter(category=category, grade__iexact=grade.strip(), size=size).first()


def _why_not(category, grade, size):
    """Why a code can't be generated for this combination, or '' if it can."""
    if not category.code:
        return f"The product type {category.name} has no 3-letter code yet - set it in the admin."
    if len(grade.strip()) > 20:
        return "The grade is longer than 20 characters."
    if size_digits(size) is None:
        return "An automatic code needs a size above 0 up to 999.99 mm, with at most 2 decimals."
    existing = GradeOption.objects.filter(name__iexact=grade.strip()).first()
    if (existing is None or existing.number is None) and _next_grade_number() > MAX_GRADE_NUMBER:
        return "All 999 grade numbers are in use."
    return ""


def _build(category, grade_number, size):
    return f"{category.code}{grade_number:03d}{size_digits(size)}"


def describe_product_code(category, grade, size):
    """What the quote form shows once type, grade and size are filled in:
    {'exists', 'item_code', 'reason'} - the existing code, or the code that
    sending the quote would create, or why none can be."""
    if not (category and grade and grade.strip() and size is not None):
        return {'exists': False, 'item_code': None, 'reason': ''}
    existing = _find_code(category, grade, size)
    if existing:
        return {'exists': True, 'item_code': existing.item_code, 'reason': ''}
    reason = _why_not(category, grade, size)
    if reason:
        return {'exists': False, 'item_code': None, 'reason': reason}
    option = GradeOption.objects.filter(name__iexact=grade.strip()).first()
    number = option.number if option and option.number is not None else _next_grade_number()
    return {'exists': False, 'item_code': _build(category, number, size), 'reason': ''}


def _grade_number(grade):
    """The grade's number, adding the grade to the list and numbering it the
    first time it is used. Returns (GradeOption, number)."""
    name = grade.strip()
    option = GradeOption.objects.filter(name__iexact=name).first() or GradeOption(name=name)
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


def item_code_for(category, grade, size, exclude_pk=None):
    """For the admin's Add/Change product code form when Item Code is left blank:
    (item_code, '') for the code to use, or (None, reason). A combination that
    already has a code is refused rather than duplicated (`exclude_pk` is the code
    being edited, which doesn't count as a duplicate of itself)."""
    if not (category and grade and grade.strip() and size is not None):
        return None, "Pick a product type and fill in grade and size, or type an item code yourself."
    existing = ProductType.objects.exclude(pk=exclude_pk).filter(
        category=category, grade__iexact=grade.strip(), size=size).first()
    if existing:
        return None, f"A product code for this type, grade and size already exists: {existing.item_code}."
    reason = _why_not(category, grade, size)
    if reason:
        return None, reason
    option = GradeOption.objects.filter(name__iexact=grade.strip()).first()
    number = option.number if option and option.number is not None else _next_grade_number()
    return _unique_item_code(_build(category, number, size), exclude_pk), ""


def reserve_grade_number(grade):
    """Add the grade to the list and number it if it isn't yet — called after a code
    using it is saved, so the number in that code is now taken and the next new
    grade gets the one after."""
    _grade_number(grade)
