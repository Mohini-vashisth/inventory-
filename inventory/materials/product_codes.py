"""Product codes: product type + grade + size, written as e.g. FBB00100120.

    FBB      the product type's 3 letters (Flat Bright Bar)
    001      the grade's number (EN8D)
    00120    the size in hundredths of a mm (1.2 mm)

This is the only place that knows the format. A code is created when a quote
using a new type + grade + size is sent; the quote form previews it first.
"""
from decimal import Decimal, InvalidOperation

from django.db import IntegrityError, transaction
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


def get_or_create_product_code(category, grade, size):
    """The product code for this type + grade + size, created if it's new.
    Returns (ProductType or None, created); None when a code can't be generated
    (see describe_product_code for the reason)."""
    if not (category and grade and grade.strip() and size is not None):
        return None, False
    existing = _find_code(category, grade, size)
    if existing:
        return existing, False
    if _why_not(category, grade, size):
        return None, False
    try:
        with transaction.atomic():
            option, number = _grade_number(grade)
            item_code = _build(category, number, size)
            suffix = 1
            while ProductType.objects.filter(item_code=item_code).exists():   # a hand-made code already uses it
                suffix += 1
                item_code = f"{_build(category, number, size)}-{suffix}"
            return ProductType.objects.create(category=category, item_code=item_code, grade=option.name, size=size), True
    except IntegrityError:   # someone else created it a moment ago
        return _find_code(category, grade, size), False
