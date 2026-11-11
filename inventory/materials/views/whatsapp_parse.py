"""Pure reading of the customer's WhatsApp answers: phone, GST number and address, product type, delivery form,
width/thickness, quantity and email. Nothing here sends or stores anything, so it can be read and tested on its own.
whatsapp.py imports every name, so `whatsapp._parse_...` keeps working for callers and tests."""

import re
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.core.validators import validate_email

from ..models import GSTIN_PATTERN, ProductType

# The same GSTIN shape as the model's validator, found inside a longer message
# and bounded so it can't be carved out of the middle of some other
# alphanumeric string.
_GSTIN_ONLY = re.compile(rf'^{GSTIN_PATTERN}$', re.IGNORECASE)
_GSTIN_IN_TEXT = re.compile(rf'(?<![A-Za-z0-9]){GSTIN_PATTERN}(?![A-Za-z0-9])', re.IGNORECASE)
_GST_LABEL_AT_END = re.compile(r'(?i)(?:gstin|gst\s*(?:no\.?|number)?)\s*[:\-\u2013]?\s*$')


def _normalize_phone(phone):
    """Digits only — matches the format Meta's Cloud API uses for
    msg['from'] (no '+', spaces, hyphens, or parens), so a number staff
    type in any human format (+91 98765 43210, 98765-43210, ...) still
    matches the customer's later WhatsApp replies exactly."""
    return re.sub(r'\D', '', phone or '')


def _clean_address(text):
    return re.sub(r'[ \t]+', ' ', text).strip(' ,;:-\u2013\n\r\t')


def _parse_whatsapp_gst_details(text):
    """Splits a reply to the GST question into (gst_number, address), or None
    if there's no valid GSTIN in it — so the caller can re-ask rather than
    save a typo. A GST number is compulsory for every company, so there is no
    "NA". gst_number comes back normalised (upper-case, no spaces); address is
    whatever else the customer wrote ("" if nothing, in which case the
    sequence asks for it separately). Accepts the number alone (any case,
    spaces ignored), or a GSTIN anywhere in a longer message — before or after
    the address, with or without a "GSTIN:" label."""
    text = (text or '').strip()
    compact = re.sub(r'\s+', '', text).upper()
    if _GSTIN_ONLY.match(compact):
        return compact, ''
    found = _GSTIN_IN_TEXT.search(text)
    if found:
        before = _GST_LABEL_AT_END.sub('', text[:found.start()])
        return found.group(0).upper(), _clean_address(f"{before} {text[found.end():]}")
    return None


def _detect_product_type(text):
    """The product type (Flat Bright Bar, Cold Rolled Strip, ...) a customer's answer
    names, or None. A type counts when its full name appears in the text (a name
    written "Profile/Shaped Bright Bar" is also matched as "profile bright bar" or
    "shaped bright bar"); a shorter name inside a longer match is dropped, and if
    what's left is more than one type the answer is ambiguous and matches nothing —
    staff pick it instead."""
    lowered = re.sub(r'\s+', ' ', (text or '').lower())

    def phrases(name):
        name = name.lower()
        return {name, re.sub(r'(\w+)/(\w+)', r'\1', name), re.sub(r'(\w+)/(\w+)', r'\2', name)}

    found = [c for c in ProductType.objects.all() if any(p in lowered for p in phrases(c.name))]
    names = [c.name.lower() for c in found]
    kept = [c for c in found if not any(c.name.lower() != other and c.name.lower() in other for other in names)]
    return kept[0] if len(kept) == 1 else None


def _parse_whatsapp_delivery_form(text):
    """"Coil" or "Bar" — what a button tap sends back, but also tolerates a
    typed "coils", "bar please", ... Returns None for anything else, and for
    an answer naming both, so the caller can re-ask with the buttons."""
    lowered = (text or '').lower()
    has_coil = bool(re.search(r'\bcoils?\b', lowered))
    has_bar = bool(re.search(r'\bbars?\b', lowered))
    if has_coil == has_bar:
        return None
    return 'Coil' if has_coil else 'Bar'


_DIMENSION_NUMBER = re.compile(r'\d+(?:[.,]\d+)?')


def _parse_whatsapp_dimension(text):
    """A width or thickness in mm from a reply like "12", "1.2 mm" or "12,5 mm round" (the first
    number; a comma is read as a decimal point), or None if there isn't a usable one:
    it must be above 0, have at most 3 decimals, and fit the Query width/thickness fields."""
    match = _DIMENSION_NUMBER.search(text or '')
    if not match:
        return None
    try:
        value = Decimal(match.group().replace(',', '.'))
    except InvalidOperation:
        return None
    if not (0 < value < 10_000_000) or value != value.quantize(Decimal('0.001')):
        return None
    return value


_KG_NUMBER = re.compile(r'(\d[\d,]*(?:\.\d+)?)\s*(?:kgs?|kilos?|kilograms?)\b', re.I)
_ANY_NUMBER = re.compile(r'\d[\d,]*(?:\.\d+)?')
_OTHER_UNITS = re.compile(r'\b(?:tons?|tonnes?|mts?|quintals?|qtl|lbs?|pounds?)\b', re.I)


def _parse_whatsapp_quantity_kg(text):
    """The quantity in kg from a reply like "8000 kgs monthly", or None when it can't be
    read safely: another unit (tons, quintals) is named, or there's no number, or several
    numbers and none is marked kg ("3 months 8000" could be either). The customer's own
    words are always kept in quantity_text; this only decides whether the numeric
    Query.quantity (which the quote form pre-fills) can be filled in too."""
    if _OTHER_UNITS.search(text or ''):
        return None
    marked = _KG_NUMBER.findall(text or '')
    if len(marked) == 1:
        candidates = marked
    elif not marked:
        candidates = _ANY_NUMBER.findall(text or '')
        if len(candidates) != 1:
            return None
    else:
        return None
    try:
        value = Decimal(candidates[0].replace(',', ''))
    except InvalidOperation:
        return None
    return value if 0 < value < 10_000_000 and value == value.quantize(Decimal('0.001')) else None


def _is_valid_whatsapp_email(text):
    """Reject an obviously-wrong reply at the email step (a typo, or an
    answer to the wrong question) rather than saving it and only finding
    out much later when query_send_quote tries to actually mail it."""
    try:
        validate_email(text)
        return True
    except ValidationError:
        return False
