"""WhatsApp Cloud API intake bot: the public webhook, sending, and the question sequence."""

import hashlib
import hmac
import json
import logging
import os
import re
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.db import transaction, connections
from django.core.files.base import ContentFile
from django.http import HttpResponse, HttpResponseForbidden
from django.utils.crypto import constant_time_compare
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
from django.core.exceptions import ValidationError
from django.core.validators import validate_email

from ..models import GSTIN_PATTERN, ProductCategory, Query, QueryItem, WhatsAppMessage

logger = logging.getLogger(__name__)


WHATSAPP_GRAPH_API_VERSION = "v21.0"


WHATSAPP_QUERY_INTAKE_TEMPLATE = "matta_drawing_query_intake"


# Meta templates are keyed by the exact language code they were approved
# under — picking "English" (not "English (US)") in WhatsApp Manager
# approves the template as "en", not "en_US". Sending with the wrong code
# fails with a "template does not exist" error even though the template
# itself exists and is approved.
WHATSAPP_QUERY_INTAKE_TEMPLATE_LANGUAGE = "en"


# Fixed intake order — the next question is whichever of these is still
# blank on the Query, so there's no separate "stage" field to drift out of
# sync with the actual data (and a field filled some other way, e.g. a staff
# edit, is simply skipped). The first reply, to the opening template, is the
# company name. gst_address is normally filled by the same reply as
# gst_number (see _parse_whatsapp_gst_details) and so is skipped; it only
# gets its own question when the customer sent the number without an address.
WHATSAPP_QUERY_FIELDS = [
    'company_name', 'contact_email', 'gst_number', 'gst_address', 'item_count', 'product_category',
    'drawing', 'grade', 'width', 'thickness', 'technical_requirements', 'end_use', 'delivery_form', 'quantity',
]

# Asked once per product: with several products the customer answers each of these with a comma-separated
# list, one value per product in the same order (the first question, item_count, says how many). Everything
# else is asked once for the whole query.
WHATSAPP_PER_ITEM_FIELDS = ('product_category', 'grade', 'width', 'thickness', 'delivery_form', 'quantity')
WHATSAPP_MAX_ITEMS = Query.MAX_ITEMS


WHATSAPP_QUERY_QUESTIONS = {
    'contact_email': "Thanks! What's the best email address to send your quote to?",
    'gst_number': "Please share your GST details: your GST number (GSTIN) and the address registered under it.",
    'gst_address': "Thanks! And the address registered under your GST?",
    'item_count': "How many different products do you need a quote for? Tap a number below.",
    'product_category': "Which product type do you need? Tap the button below and choose one.",
    'drawing': "Please attach a drawing with detailed dimensions, or a photo of a sample. You can send an image or PDF here, or reply 'no' if you don't have one.",
    'grade': "Which grade of material do you require?",
    'width': "What width do you need, in mm? Please reply with the number, for example 50 or 12.5.",
    'thickness': "And the thickness, in mm? Please reply with the number, for example 6 or 1.2.",
    'technical_requirements': "Any particular make, mechanical properties or processes to be carried out? Reply 'no' if none.",
    'end_use': "What is the end use of the material?",
    'delivery_form': "In what form do you need the material delivered?",
    'quantity': "Please enter the quantity in kgs.",
}

# With more than one product the same questions ask for a list. {n} is the number of products and {example}
# a made-up answer with that many values.
_LIST_SUFFIX = " Send {n} values separated by commas, one for each product, in the same order."
_LIST_QUESTIONS = {
    'product_category': "Which product types do you need?" + _LIST_SUFFIX + " Choose from: {types}.",
    'grade': "Which grades of material do you require?" + _LIST_SUFFIX + " Example: {example}",
    'width': "What widths do you need, in mm?" + _LIST_SUFFIX + " Use a dot for decimals. Example: {example}",
    'thickness': "And the thicknesses, in mm?" + _LIST_SUFFIX + " Use a dot for decimals. Example: {example}",
    'delivery_form': "In what form do you need each one delivered, Coil or Bar?" + _LIST_SUFFIX + " Example: {example}",
    'quantity': "Please enter the quantity in kgs for each product." + _LIST_SUFFIX + " Example: {example}",
}
_LIST_EXAMPLES = {
    'grade': ['EN8D', 'SAE1010', 'SS304', 'EN9', 'SAE1008'],
    'width': ['50', '12.5', '30', '45', '60'],
    'thickness': ['6', '1.2', '3', '4', '8'],
    'delivery_form': ['Coil', 'Bar', 'Coil', 'Bar', 'Coil'],
    'quantity': ['500', '2000', '800', '1500', '3000'],
}


def _question_text(field, count=1):
    """The question as sent: the plain one for a single product, the comma-list version for several."""
    if count <= 1 or field not in _LIST_QUESTIONS:
        return WHATSAPP_QUERY_QUESTIONS[field]
    types = ', '.join(ProductCategory.objects.values_list('name', flat=True))
    example = ', '.join(_LIST_EXAMPLES.get(field, [])[:count])
    return _LIST_QUESTIONS[field].format(n=count, example=example, types=types)


# Questions with a fixed set of answers are sent as tappable reply buttons
# instead of plain text (Meta allows at most 3 buttons, each title up to 20
# characters). A tap arrives as an "interactive" message carrying the button's
# title, which is handled like a typed answer; a typed answer is still
# accepted (see _parse_whatsapp_delivery_form).
WHATSAPP_QUERY_CHOICES = {
    'delivery_form': Query.DELIVERY_FORM_CHOICES,
}


WHATSAPP_QUANTITY_INVALID_MESSAGE = "Please enter the quantity as a number in kgs, for example 8000."

# Width and thickness are both asked, one per message, and both must be a number of mm.
WHATSAPP_DIMENSION_INVALID_MESSAGES = {
    'width': "I couldn't read a width in that. Please reply with the width in mm as a number, for example 50 or 12.5.",
    'thickness': "I couldn't read a thickness in that. Please reply with the thickness in mm as a number, for example 6 or 1.2.",
}

WHATSAPP_GST_INVALID_MESSAGE = (
    "I couldn't find a valid 15-character GST number in that (for example 22AAAAA0000A1Z5). "
    "Please send your GST number along with your registered address."
)


# The same GSTIN shape as the model's validator, found inside a longer message
# and bounded so it can't be carved out of the middle of some other
# alphanumeric string.
_GSTIN_ONLY = re.compile(rf'^{GSTIN_PATTERN}$', re.IGNORECASE)
_GSTIN_IN_TEXT = re.compile(rf'(?<![A-Za-z0-9]){GSTIN_PATTERN}(?![A-Za-z0-9])', re.IGNORECASE)
_GST_LABEL_AT_END = re.compile(r'(?i)(?:gstin|gst\s*(?:no\.?|number)?)\s*[:\-\u2013]?\s*$')


WHATSAPP_CLOSING_MESSAGE = (
    "Thanks! We'll reach out to you shortly. If you have any other questions in "
    "the meantime, feel free to message us here and we'll get back to you."
)


class WhatsAppSendError(Exception):
    """Raised by the send helpers below on any failure — missing config,
    network error, or a non-2xx response from the Graph API. Callers
    decide how to degrade; nothing here is allowed to propagate into a
    crash (a failed outbound send should never lose an already-saved
    answer or block a query from being created)."""


def _whatsapp_graph_request(payload):
    phone_number_id = settings.WHATSAPP_PHONE_NUMBER_ID
    access_token = settings.WHATSAPP_ACCESS_TOKEN
    if not phone_number_id or not access_token:
        raise WhatsAppSendError("WhatsApp sending is not configured (missing access token/phone number ID).")

    url = f"https://graph.facebook.com/{WHATSAPP_GRAPH_API_VERSION}/{phone_number_id}/messages"
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status >= 300:
                raise WhatsAppSendError(f"WhatsApp API returned HTTP {resp.status}")
    except urllib.error.HTTPError as e:
        raise WhatsAppSendError(f"WhatsApp API error {e.code}: {e.read().decode(errors='replace')}") from e
    except (urllib.error.URLError, OSError) as e:
        raise WhatsAppSendError(f"WhatsApp API request failed: {e}") from e


def _send_whatsapp_template_message(phone, template_name, language="en_US"):
    """The first message to a number that hasn't messaged us (or has gone
    quiet 24h+) must be a pre-approved template — Meta rejects free-form
    text otherwise. `template_name` must already be approved in Meta
    Business Manager."""
    _whatsapp_graph_request({
        "messaging_product": "whatsapp", "to": phone, "type": "template",
        "template": {"name": template_name, "language": {"code": language}},
    })


def _send_whatsapp_text_message(phone, text):
    """Free-form follow-up — only usable once the customer has replied at
    least once within the last 24 hours."""
    _whatsapp_graph_request({
        "messaging_product": "whatsapp", "to": phone, "type": "text", "text": {"body": text},
    })


def _send_whatsapp_buttons_message(phone, text, choices):
    """A question with tappable reply buttons. Like free-form text, only usable
    inside the 24-hour window the customer's own reply opened. `choices` is
    [(value, label), ...]; the label is both the button text and what comes
    back when it's tapped."""
    _whatsapp_graph_request({
        "messaging_product": "whatsapp", "to": phone, "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": text},
            "action": {"buttons": [
                {"type": "reply", "reply": {"id": value, "title": label}} for value, label in choices
            ]},
        },
    })


# Meta allows at most 10 rows in a list message, a row title up to 24 characters
# and the opening button up to 20.
WHATSAPP_LIST_MAX_ROWS = 10
WHATSAPP_LIST_TITLE_MAX = 24
WHATSAPP_CATEGORY_ROW_PREFIX = 'category:'


def _send_whatsapp_list_message(phone, text, button_label, rows):
    """A question with a tappable list (for more answers than the 3 reply buttons
    allow). `rows` is [(id, title, description), ...]. Like free-form text, only
    usable inside the 24-hour window the customer's own reply opened. A tap comes
    back as an interactive list_reply carrying the row's id and title."""
    _whatsapp_graph_request({
        "messaging_product": "whatsapp", "to": phone, "type": "interactive",
        "interactive": {
            "type": "list",
            "body": {"text": text},
            "action": {"button": button_label, "sections": [{
                "title": "Product types",
                "rows": [{"id": row_id, "title": title, "description": description} for row_id, title, description in rows],
            }]},
        },
    })


def _product_category_rows():
    """The product types as list rows — the title is cut to fit Meta's limit, the
    description carries the full name. None when there are too many for one list
    (the customer is then asked to type the name instead)."""
    categories = list(ProductCategory.objects.all())
    if not categories or len(categories) > WHATSAPP_LIST_MAX_ROWS:
        return None
    return [
        (f"{WHATSAPP_CATEGORY_ROW_PREFIX}{c.pk}",
         c.name if len(c.name) <= WHATSAPP_LIST_TITLE_MAX else c.name[:WHATSAPP_LIST_TITLE_MAX - 1] + '…',
         c.name)
        for c in categories
    ]


def _send_whatsapp_text_message_background(phone, text):
    """Fire-and-forget a follow-up question from inside the webhook. Meta
    expects a fast ack on every delivery — waiting on the Graph API's own
    network round trip before responding risks a slow/timed-out ack, which
    is exactly what triggers Meta to redeliver the same message. The
    answer that triggered this send is already saved by the time this
    runs, so a failed send here only means a delayed follow-up question,
    never lost data — logged (not silently swallowed) so a persistent
    failure, e.g. an expired access token, is actually visible."""
    def _send():
        try:
            _send_whatsapp_text_message(phone, text)
        except WhatsAppSendError as e:
            logger.warning("WhatsApp follow-up send to %s failed: %s", phone, e)
    thread = threading.Thread(target=_send, daemon=True)
    thread.start()
    return thread


def _send_whatsapp_buttons_message_background(phone, text, choices):
    """Same fire-and-forget contract as _send_whatsapp_text_message_background."""
    def _send():
        try:
            _send_whatsapp_buttons_message(phone, text, choices)
        except WhatsAppSendError as e:
            logger.warning("WhatsApp follow-up send to %s failed: %s", phone, e)
    thread = threading.Thread(target=_send, daemon=True)
    thread.start()
    return thread


def _send_whatsapp_steps_background(phone, steps):
    """Send several messages in order, from one background thread (separate threads could arrive out of
    order). Each step is ('text', text), ('buttons', text, choices) or ('list', text, button, rows). If
    one fails the rest are not sent — a "Confirm" button must never arrive without its summary."""
    def _send():
        for step in steps:
            try:
                if step[0] == 'text':
                    _send_whatsapp_text_message(phone, step[1])
                elif step[0] == 'buttons':
                    _send_whatsapp_buttons_message(phone, step[1], step[2])
                elif step[0] == 'list':
                    _send_whatsapp_list_message(phone, step[1], step[2], step[3])
            except WhatsAppSendError as e:
                logger.warning("WhatsApp send to %s failed: %s", phone, e)
                return
    thread = threading.Thread(target=_send, daemon=True)
    thread.start()
    return thread


def _send_whatsapp_list_message_background(phone, text, button_label, rows):
    """Same fire-and-forget contract as _send_whatsapp_text_message_background."""
    def _send():
        try:
            _send_whatsapp_list_message(phone, text, button_label, rows)
        except WhatsAppSendError as e:
            logger.warning("WhatsApp follow-up send to %s failed: %s", phone, e)
    thread = threading.Thread(target=_send, daemon=True)
    thread.start()
    return thread


def _item_count_rows():
    return [(f"count:{n}", f"{n} product" + ("s" if n > 1 else ''), '') for n in range(1, WHATSAPP_MAX_ITEMS + 1)]


def _send_whatsapp_question(phone, field, count=1):
    """Send `field`'s question — as a tappable list for the number of products (and the product type
    when there is just one), as buttons if it has a few fixed answers, as plain text otherwise. With
    several products the per-product questions ask for a comma-separated list instead."""
    text = _question_text(field, count)
    if field == 'item_count':
        return _send_whatsapp_list_message_background(phone, text, "Choose number", _item_count_rows())
    if field == 'product_category' and count <= 1:
        rows = _product_category_rows()
        if rows:
            return _send_whatsapp_list_message_background(phone, text, "Choose type", rows)
        return _send_whatsapp_text_message_background(phone, "Which product type do you need? Please type its name.")
    choices = WHATSAPP_QUERY_CHOICES.get(field)
    if choices and count <= 1:
        return _send_whatsapp_buttons_message_background(phone, text, choices)
    return _send_whatsapp_text_message_background(phone, text)


def _item_value_missing(item, field):
    if field == 'product_category':
        return not item.product_category_id
    if field in ('width', 'thickness', 'quantity'):
        return getattr(item, field) is None
    return not getattr(item, field)


def _next_expected_query_field(query):
    """The next unanswered field in the fixed intake order, or None once every field is filled. The
    per-product fields are unanswered while any of the query's products still lacks the value;
    'item_count' is unanswered until the products exist. 'drawing' is special: a FileField alone can't
    tell "not asked yet" apart from "asked, customer had none" — drawing_notes (set either way) is what
    actually marks that question answered, whether or not a file came with it."""
    items = query.item_list()
    for field in WHATSAPP_QUERY_FIELDS:
        if field == 'item_count':
            if not items:
                return field
        elif field == 'drawing':
            if not query.drawing and not query.drawing_notes:
                return field
        elif field in WHATSAPP_PER_ITEM_FIELDS:
            if any(_item_value_missing(item, field) for item in items):
                return field
        elif not getattr(query, field):
            return field
    return None


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


def _detect_product_category(text):
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

    found = [c for c in ProductCategory.objects.all() if any(p in lowered for p in phrases(c.name))]
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


@csrf_exempt
@require_http_methods(["GET", "POST"])
def whatsapp_webhook(request):
    """Meta WhatsApp Cloud API webhook. GET is the one-time subscription
    handshake Meta performs when the webhook URL is registered; POST
    delivers inbound message events. csrf_exempt because Meta's servers
    never carry a Django session/CSRF cookie — HMAC verification of
    X-Hub-Signature-256 (POST) and the verify-token check (GET) are the
    real authentication here, not Django's CSRF protection."""
    if request.method == "GET":
        return _whatsapp_verify(request)
    return _whatsapp_receive(request)


def _whatsapp_verify(request):
    mode = request.GET.get("hub.mode")
    token = request.GET.get("hub.verify_token", "")
    challenge = request.GET.get("hub.challenge", "")
    # An unset WHATSAPP_VERIFY_TOKEN must never "verify" anything — without
    # this check, a blank token setting would match a request that also
    # omits hub.verify_token ('' == ''), passing a handshake that verified
    # nothing at all.
    if (
        settings.WHATSAPP_VERIFY_TOKEN
        and mode == "subscribe"
        and constant_time_compare(token, settings.WHATSAPP_VERIFY_TOKEN)
    ):
        return HttpResponse(challenge, content_type="text/plain")
    return HttpResponseForbidden("Verification failed")


def _whatsapp_receive(request):
    # Signature check happens before anything else touches the body — an
    # invalid/missing signature means nothing here is trusted, so nothing
    # gets parsed or written.
    signature = request.headers.get("X-Hub-Signature-256", "")
    if not _valid_whatsapp_signature(request.body, signature):
        return HttpResponseForbidden("Invalid signature")

    try:
        payload = json.loads(request.body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        # Malformed body from an already-authenticated sender — ack anyway
        # so Meta doesn't retry-storm; there's nothing usable to process.
        return HttpResponse(status=200)
    if not isinstance(payload, dict):
        # Syntactically valid JSON that isn't an object (e.g. "null", "5",
        # a bare array) — same "nothing usable, just ack" case as above.
        return HttpResponse(status=200)

    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            _process_whatsapp_change(change.get("value", {}))

    return HttpResponse(status=200)


def _valid_whatsapp_signature(raw_body, signature_header):
    # An unset WHATSAPP_APP_SECRET must never validate anything — HMAC
    # keyed with an empty string is a key anyone can also compute, which
    # would make the signature trivially forgeable rather than absent.
    if not settings.WHATSAPP_APP_SECRET or not signature_header.startswith("sha256="):
        return False
    provided = signature_header[len("sha256="):]
    expected = hmac.new(
        settings.WHATSAPP_APP_SECRET.encode("utf-8"), raw_body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(provided, expected)


def _process_whatsapp_change(value):
    """Meta posts both inbound messages and delivery-status receipts
    (`value['statuses']`) to this same webhook — only the former should
    ever touch a Query. Text messages are handled inline; an image/document
    reply is only meaningful when 'drawing' is actually the field being
    asked for right now, and downloading one is slow enough to need
    backgrounding (see _route_whatsapp_media) — a tap on a reply button is treated like a typed answer; every other
    message type (audio/video/location/etc.) is still not handled."""
    incoming_messages = value.get("messages")
    if not incoming_messages:
        return

    contacts = value.get("contacts", [])
    profile_name = contacts[0].get("profile", {}).get("name", "") if contacts else ""

    # Meta redelivers anything it didn't get an acknowledgement for, and replays a backlog when
    # the app comes back after being offline — in any order. So: (1) a message id seen before is
    # skipped, (2) a batch is handled oldest-first, and (3) when one batch holds several messages
    # from the same customer only the last one sends the next question, otherwise they would be
    # asked a string of questions they have already answered.
    WhatsAppMessage.objects.filter(received_at__lt=timezone.now() - WHATSAPP_MESSAGE_ID_KEEP).delete()   # old ids can't recur
    ordered = sorted((m for m in incoming_messages if m.get("from")), key=_message_timestamp)
    fresh = [m for m in ordered if _claim_whatsapp_message(m.get("id"))]
    last_for_phone = {_normalize_phone(m["from"]): index for index, m in enumerate(fresh)}

    for index, msg in enumerate(fresh):
        phone = msg["from"]
        send_next = last_for_phone[_normalize_phone(phone)] == index
        sent_at = _message_sent_at(msg)
        try:
            _handle_whatsapp_message(msg, phone, profile_name, sent_at, send_next)
        except Exception:
            for unhandled in fresh[index:]:   # let Meta's retry process these properly
                _release_whatsapp_message(unhandled.get("id"))
            raise


def _message_timestamp(msg):
    try:
        return int(msg.get("timestamp") or 0)
    except (TypeError, ValueError):
        return 0


def _message_sent_at(msg):
    """When the customer sent this message, from Meta's own timestamp (None if absent)."""
    seconds = _message_timestamp(msg)
    return datetime.fromtimestamp(seconds, tz=dt_timezone.utc) if seconds else None


def _claim_whatsapp_message(message_id):
    """True the first time a message id is seen (and records it); False for a repeat.
    Messages with no id (never the case from Meta) are always handled."""
    if not message_id:
        return True
    _, created = WhatsAppMessage.objects.get_or_create(message_id=message_id)
    return created


def _release_whatsapp_message(message_id):
    if message_id:
        WhatsAppMessage.objects.filter(message_id=message_id).delete()


def _handle_whatsapp_message(msg, phone, profile_name, sent_at, send_next):
    msg_type = msg.get("type")
    if msg_type == "text":
        text = (msg.get("text") or {}).get("body", "").strip()
        if text:
            _route_whatsapp_message(phone, text, profile_name, sent_at, send_next)
    elif msg_type == "interactive":
        # A tap on a reply button (or list row): its title is the answer.
        interactive = msg.get("interactive") or {}
        reply = interactive.get("button_reply") or interactive.get("list_reply") or {}
        title = (reply.get("title") or "").strip()
        reply_id = reply.get("id") or ""
        if reply_id.startswith(WHATSAPP_CATEGORY_ROW_PREFIX):   # a product type row: its title may be cut short
            category = ProductCategory.objects.filter(pk=reply_id[len(WHATSAPP_CATEGORY_ROW_PREFIX):] or 0).first() \
                if reply_id[len(WHATSAPP_CATEGORY_ROW_PREFIX):].isdigit() else None
            title = category.name if category else title
        if reply_id.startswith(WHATSAPP_FIELD_ROW_PREFIX) or reply_id.startswith(WHATSAPP_ITEM_ROW_PREFIX) or reply_id in ('more', 'back'):
            title = f"@{reply_id}"   # a row of the "what would you like to change?" lists
        elif reply_id.startswith('count:'):
            title = reply_id[len('count:'):]   # the number of products, tapped from a list
        if title:
            _route_whatsapp_message(phone, title, profile_name, sent_at, send_next)
    elif msg_type in ("image", "document"):
        media = msg.get(msg_type) or {}
        media_id = media.get("id")
        if media_id:
            _route_whatsapp_media(phone, media_id, media.get("mime_type", ""), sent_at)


def _route_whatsapp_message(phone, text, profile_name, sent_at=None, send_next=True):
    """Route an inbound message to whichever open Query is mid-intake for
    this phone number. A message from a number with no in-progress query —
    either a cold inbound message, or a reply after that query already
    converted/was marked not interested — still becomes a bare Query
    rather than being silently dropped."""
    phone = _normalize_phone(phone)
    query = (
        Query.objects
        .filter(contact_phone=phone)
        .exclude(status__in=['converted', 'not_interested'])
        .order_by('-created_at')
        .first()
    )
    if query:
        _process_whatsapp_answer(query.pk, text, sent_at, send_next)
    else:
        Query.objects.create(source='whatsapp', company_name=profile_name, contact_phone=phone, notes=text,
                             last_inbound_at=sent_at or timezone.now())


def _route_whatsapp_media(phone, media_id, mime_type, sent_at=None):
    """Same lookup as _route_whatsapp_message, but for an image/document
    reply — only meaningful when 'drawing' is actually the field being
    asked for right now; otherwise there's no in-progress query expecting
    a file, so it's dropped, the same as any other message type this flow
    doesn't interpret. Unlike a cold text message, a cold image isn't
    turned into a bare Query either — there's no caption text to put in
    notes, so there'd be nothing useful to record."""
    phone = _normalize_phone(phone)
    query = (
        Query.objects
        .filter(contact_phone=phone)
        .exclude(status__in=['converted', 'not_interested'])
        .order_by('-created_at')
        .first()
    )
    if not query:
        return
    newest = sent_at or timezone.now()   # a file is the customer's newest message too: it re-opens the window
    if not query.last_inbound_at or newest > query.last_inbound_at:
        query.last_inbound_at = newest
        query.save(update_fields=['last_inbound_at'])
    editing_drawing = query.bot_stage == 'editing' and query.edit_field == 'drawing'
    if not editing_drawing and (query.bot_stage not in ('', ) or _next_expected_query_field(query) != 'drawing'):
        return
    _process_whatsapp_drawing_media_background(query.pk, media_id, mime_type)


_WHATSAPP_MIME_EXTENSIONS = {
    'image/jpeg': '.jpg', 'image/png': '.png', 'image/webp': '.webp',
    'application/pdf': '.pdf',
}


def _extension_for_mime_type(mime_type):
    return _WHATSAPP_MIME_EXTENSIONS.get((mime_type or '').split(';')[0].strip(), '')


def _download_whatsapp_media(media_id):
    """Two-step Graph API fetch: resolve the media id to a short-lived
    signed URL + mime type, then download the actual bytes from that URL —
    both steps need the access token, per Meta's documented media-download
    flow. Raises WhatsAppSendError on any failure — reused rather than a
    separate exception class, since it's the same "something about talking
    to the Graph API failed" shape every other helper here already uses."""
    access_token = settings.WHATSAPP_ACCESS_TOKEN
    if not access_token:
        raise WhatsAppSendError("WhatsApp sending is not configured (missing access token).")

    meta_req = urllib.request.Request(
        f"https://graph.facebook.com/{WHATSAPP_GRAPH_API_VERSION}/{media_id}",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    try:
        with urllib.request.urlopen(meta_req, timeout=10) as resp:
            meta = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise WhatsAppSendError(f"Could not resolve WhatsApp media {media_id}: {e}") from e

    media_url = meta.get("url")
    mime_type = meta.get("mime_type", "")
    if not media_url:
        raise WhatsAppSendError(f"WhatsApp media {media_id} had no download URL")

    data_req = urllib.request.Request(media_url, headers={"Authorization": f"Bearer {access_token}"})
    try:
        with urllib.request.urlopen(data_req, timeout=20) as resp:
            content = resp.read()
    except (urllib.error.URLError, OSError) as e:
        raise WhatsAppSendError(f"Could not download WhatsApp media {media_id}: {e}") from e

    return content, mime_type


def _process_whatsapp_drawing_media_background(query_pk, media_id, mime_type):
    """Downloading a WhatsApp media file takes two external HTTP round
    trips (resolve id -> signed URL, then download the bytes) — backgrounded
    for the same reason outbound follow-up sends are: Meta expects a fast
    ack on the webhook itself, well before either of those calls would
    finish. The whole thing (download, save, advance the sequence) runs in
    the thread; the webhook has already returned 200 by the time this does
    anything. connections.close_all() at the end avoids leaking a DB
    connection this thread opened on its own — Django only cleans those up
    automatically at the end of a request/response cycle, which this isn't."""
    def _work():
        try:
            content, resolved_mime = _download_whatsapp_media(media_id)
        except WhatsAppSendError as e:
            logger.warning("WhatsApp media download for query %s failed: %s", query_pk, e)
            return
        try:
            with transaction.atomic():
                try:
                    query = Query.objects.select_for_update().get(pk=query_pk)
                except Query.DoesNotExist:
                    return
                filename = f"drawing{_extension_for_mime_type(resolved_mime or mime_type)}"
                if query.bot_stage == 'editing' and query.edit_field == 'drawing':
                    # A replacement file: hold it until the customer confirms the change.
                    query.pending_drawing.save(filename, ContentFile(content), save=False)
                    query.pending_value = {'drawing_notes': "Drawing attached via WhatsApp", '_pending_file': True}
                    query.bot_stage = 'confirm_change'
                    query.save(update_fields=['pending_drawing', 'pending_value', 'bot_stage'])
                    if _whatsapp_window_open(query):
                        _send_whatsapp_steps_background(query.contact_phone, _confirm_change_steps(query))
                    return
                if query.bot_stage != '' or _next_expected_query_field(query) != 'drawing':
                    return  # already answered some other way (e.g. a race with a text reply)
                query.drawing.save(filename, ContentFile(content), save=False)
                query.drawing_notes = "Drawing attached via WhatsApp"
                query.save(update_fields=['drawing', 'drawing_notes'])
                _advance_whatsapp_query(query)
        finally:
            connections.close_all()
    thread = threading.Thread(target=_work, daemon=True)
    thread.start()
    return thread


# WhatsApp only lets the bot send free-form text for 24 hours after the customer's last message;
# a margin is kept so a send never lands right on the edge.
WHATSAPP_WINDOW = timedelta(hours=23)
# How long handled message ids are remembered; Meta retries for days, not months.
WHATSAPP_MESSAGE_ID_KEEP = timedelta(days=30)
WHATSAPP_WINDOW_CLOSED_NOTE = (
    "WhatsApp's 24-hour reply window has closed, so the bot can't send the next question — "
    "message the customer on WhatsApp yourself; the bot carries on when they reply."
)
WHATSAPP_OUT_OF_ORDER_NOTE = (
    "A reply reached the bot out of order (the app was probably offline) — check the answers below "
    "are in the right fields; the late message is in the notes."
)


def _whatsapp_window_open(query, now=None):
    """Whether the bot may still send the customer free-form messages."""
    if query.last_inbound_at is None:
        return True
    return (now or timezone.now()) - query.last_inbound_at < WHATSAPP_WINDOW


def _flag_for_review(query, note):
    """Mark a conversation for staff (shown on the dashboard and detail page)."""
    if note not in query.review_note:
        query.review_note = f"{query.review_note} {note}".strip()[:255]
    query.needs_review = True
    query.save(update_fields=['needs_review', 'review_note'])


def _process_whatsapp_answer(query_pk, text, sent_at=None, send_next=True):
    """Save this message as the answer to whichever question is next in
    the intake sequence, then send the following question — or, once the
    sequence is complete, the closing message. A message that arrives after
    the sequence is already done is just appended to notes, not mistaken
    for an answer.

    Re-fetches and locks the row inside a transaction (select_for_update)
    rather than trusting the caller's already-read Query instance — Meta
    can redeliver the same webhook, and two overlapping deliveries for the
    same phone must not both read the same "next field" and race each
    other into the wrong column. A no-op on SQLite (no row locking there),
    but real protection once/if this ever runs on Postgres.

    `sent_at` is when the customer sent it (Meta's timestamp). A message older than one already
    handled arrived out of order — it is not used as an answer, only noted and flagged for staff.
    If the 24-hour window since the customer's newest message has closed, answers are still saved
    but nothing is sent (it would be refused) and staff are told. `send_next=False` is used for all
    but the last of several messages from one customer in a single delivery."""
    with transaction.atomic():
        query = Query.objects.select_for_update().get(pk=query_pk)

        if sent_at is not None and query.last_inbound_at and sent_at < query.last_inbound_at - timedelta(seconds=1):
            stamp = timezone.localtime(sent_at).strftime('%d %b %H:%M')
            query.notes = f"{query.notes}\n[late reply, sent {stamp}] {text}".strip()
            query.save(update_fields=['notes'])
            _flag_for_review(query, WHATSAPP_OUT_OF_ORDER_NOTE)
            return

        newest = sent_at or timezone.now()
        query.last_inbound_at = max(query.last_inbound_at, newest) if query.last_inbound_at else newest
        query.save(update_fields=['last_inbound_at'])
        window_open = _whatsapp_window_open(query)
        if not window_open:
            _flag_for_review(query, WHATSAPP_WINDOW_CLOSED_NOTE)

        def say(message):
            if window_open:
                _send_whatsapp_text_message_background(query.contact_phone, message)

        def ask(field_name):
            if window_open:
                _send_whatsapp_question(query.contact_phone, field_name, query.items.count())

        if query.bot_stage in WHATSAPP_REVIEW_STAGES:
            _review_reply(query, text, window_open)
            return

        field = _next_expected_query_field(query)
        if field is None:
            stamp = timezone.now().strftime('%d %b %H:%M')
            query.notes = f"{query.notes}\n[{stamp}] {text}".strip()
            query.save(update_fields=['notes'])
            return

        try:
            updates = _parse_answer(field, text.strip(), query.items.count())
        except _InvalidAnswer as invalid:
            if invalid.say:
                say(invalid.say)
            if invalid.ask:
                ask(invalid.ask)
            return
        query.save(update_fields=_apply_updates(query, updates))

        if send_next:
            _advance_whatsapp_query(query)


def _advance_whatsapp_query(query):
    """Sends the next question in the intake sequence — or, once every question is answered, a summary
    of the answers with Confirm / Change buttons (the review, see _review_reply). Shared by the
    text-answer path above and the drawing-media path (_process_whatsapp_drawing_media_background),
    since both need to advance the same way once their field is saved. Never re-asks a question that
    was already sent and is still waiting for its answer (so a replayed or duplicated message can't
    double-ask), and sends nothing once the 24-hour window has closed — staff are told instead."""
    if not _whatsapp_window_open(query):
        _flag_for_review(query, WHATSAPP_WINDOW_CLOSED_NOTE)
        return
    next_field = _next_expected_query_field(query)
    if next_field is None:
        if query.bot_stage == '':   # answers are complete: show them back to the customer once
            query.bot_stage = 'summary'
            query.save(update_fields=['bot_stage'])
            _send_whatsapp_steps_background(query.contact_phone, _summary_steps(query))
        return
    if query.last_asked_field == next_field:
        return
    query.last_asked_field = next_field
    query.save(update_fields=['last_asked_field'])
    _send_whatsapp_question(query.contact_phone, next_field, query.items.count())


# ── Answer parsing ────────────────────────────────────────────────────────────

class _InvalidAnswer(Exception):
    """A reply that can't be used as the answer: `say` is the message to send back, `ask` a
    question to send again (buttons or a list)."""
    def __init__(self, say=None, ask=None):
        super().__init__(say or ask)
        self.say, self.ask = say, ask


WHATSAPP_ITEM_COUNT_WORDS = {'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5}


def _parse_item_count(text):
    """How many products (1 to 5) from "3", "three", "3 products" — or None."""
    lowered = text.lower()
    for word, number in WHATSAPP_ITEM_COUNT_WORDS.items():
        if re.search(rf'\b{word}\b', lowered):
            return number
    found = re.search(r'\d+', lowered)
    if found and 1 <= int(found.group()) <= WHATSAPP_MAX_ITEMS:
        return int(found.group())
    return None


def _product_type_names():
    return ', '.join(ProductCategory.objects.values_list('name', flat=True))


def _parse_single_value(field, part, position, count):
    """One product's value for a per-product field, JSON-safe. `position` (0-based) and `count` only
    shape the error message; raises _InvalidAnswer for an unusable value."""
    where = f"Product {position + 1}: " if count > 1 else ''
    if field in WHATSAPP_DIMENSION_INVALID_MESSAGES:   # width, thickness
        parsed = _parse_whatsapp_dimension(part)
        if parsed is None:
            raise _InvalidAnswer(say=where + WHATSAPP_DIMENSION_INVALID_MESSAGES[field])
        return str(parsed)
    if field == 'quantity':
        kg = _parse_whatsapp_quantity_kg(part)
        if kg is None:
            raise _InvalidAnswer(say=where + WHATSAPP_QUANTITY_INVALID_MESSAGE)
        return str(kg)
    if field == 'delivery_form':
        parsed = _parse_whatsapp_delivery_form(part)
        if parsed is None:
            raise _InvalidAnswer(say=f"{where}please say Coil or Bar." if count > 1 else None, ask=None if count > 1 else 'delivery_form')
        return parsed
    if field == 'product_category':
        detected = _detect_product_category(part)
        if detected is None:
            if count > 1:
                raise _InvalidAnswer(say=f"{where}I couldn't match '{part}' to a product type. Choose from: {_product_type_names()}.")
            raise _InvalidAnswer(ask='product_category')
        return detected.pk
    if not part.strip():   # grade
        raise _InvalidAnswer(say=f"{where}please give the grade.")
    return part.strip()


def _parse_item_values(field, text, count):
    """The values for every product from a reply: the whole reply for one product, a comma-separated
    list with exactly one value per product for several."""
    if count <= 1:
        return [_parse_single_value(field, text, 0, 1)]
    parts = [part.strip() for part in text.split(',')]
    while parts and not parts[-1]:
        parts.pop()   # a trailing comma is harmless
    if len(parts) != count:
        raise _InvalidAnswer(say=(f"I received {len(parts)} value{'s' if len(parts) != 1 else ''} but need {count}, one for each "
                                  f"product, separated by commas.\n\n{_question_text(field, count)}"))
    return [_parse_single_value(field, part, position, count) for position, part in enumerate(parts)]


def _parse_answer(field, text, count=1):
    """The model updates a reply to `field`'s question stands for, as JSON-safe values (so a change
    can be held as pending until the customer confirms it). `count` is the number of products: the
    per-product questions take a comma-separated list when it is more than one. Raises _InvalidAnswer
    for an unusable reply."""
    if field == 'item_count':
        number = _parse_item_count(text)
        if number is None:
            raise _InvalidAnswer(ask='item_count')
        return {'item_count': number}
    if field == 'gst_number':
        parsed = _parse_whatsapp_gst_details(text)
        if parsed is None:
            raise _InvalidAnswer(say=WHATSAPP_GST_INVALID_MESSAGE)
        number, address = parsed
        return {'gst_number': number, **({'gst_address': address} if address else {})}
    if field in WHATSAPP_PER_ITEM_FIELDS:
        updates = {'_items': {field: _parse_item_values(field, text, count)}}
        if count <= 1 and field in WHATSAPP_DIMENSION_INVALID_MESSAGES and len(_DIMENSION_NUMBER.findall(text)) > 1:
            updates['_note'] = f"{field.capitalize()} reply: {text}"   # e.g. "10 and 12 mm": keep the first, show staff the rest
        return updates
    if field == 'contact_email':
        if not _is_valid_whatsapp_email(text):
            raise _InvalidAnswer(say=WHATSAPP_QUERY_QUESTIONS['contact_email'])
        return {'contact_email': text}
    if field == 'drawing':
        # A text reply here means no attachment came with it — "no", or a short description instead of a
        # photo/PDF. An actual image/document reply is handled separately, in the background, by
        # _process_whatsapp_drawing_media_background.
        return {'drawing_notes': text}
    return {field: text}


def _apply_item_updates(query, values_by_field, only=None, editing=False):
    """Write per-product values onto the query's items: every item in order, or just item `only`
    (a customer changing one product's detail). Re-links each touched item's product code to its type +
    grade (dropping a stale link when editing)."""
    items = query.item_list()
    targets = [(only, 0)] if only is not None else list(enumerate(range(len(items))))
    for index, value_index in targets:
        if index >= len(items):
            continue
        item, touched = items[index], []
        for field, values in values_by_field.items():
            value = values[value_index]
            if field == 'product_category':
                value = ProductCategory.objects.get(pk=value)
                item.product_category = value
            elif field in ('width', 'thickness', 'quantity'):
                setattr(item, field, Decimal(value))
            else:
                setattr(item, field, value)
            touched.append(field)
        if 'grade' in touched or 'product_category' in touched:
            code = item.matching_product_code()   # link the catalogue code as soon as type + grade are known
            if editing or (code and not item.product_type_id):
                item.product_type = code
                touched.append('product_type')
        item.save(update_fields=list(dict.fromkeys(touched)))


def _apply_updates(query, updates, editing=False):
    """Set `updates` (from _parse_answer) on the query and its items, and return the query field names
    to save. A customer's confirmed change overrides what was there."""
    changed = []
    for key, value in updates.items():
        if key == '_note':
            stamp = timezone.now().strftime('%d %b %H:%M')
            query.notes = f"{query.notes}\n[{stamp}] {value}".strip()
            changed.append('notes')
        elif key == 'item_count':
            if not query.items.exists():
                QueryItem.objects.bulk_create([QueryItem(query=query, position=n) for n in range(1, value + 1)])
        elif key in ('_items', '_item_only', '_pending_file'):
            continue
        else:
            setattr(query, key, value)
            changed.append(key)
    if '_items' in updates:
        _apply_item_updates(query, updates['_items'], updates.get('_item_only'), editing)
    if updates.get('_pending_file') and query.pending_drawing:
        content = query.pending_drawing.read()
        query.drawing.save(os.path.basename(query.pending_drawing.name), ContentFile(content), save=False)
        query.pending_drawing.delete(save=False)
        query.pending_drawing = None
        changed += ['drawing', 'pending_drawing']
    elif editing and 'drawing_notes' in updates and query.drawing:
        query.drawing = ''   # replaced by a text answer: the old file no longer stands
        changed.append('drawing')
    return list(dict.fromkeys(changed))


# ── The end-of-conversation review ────────────────────────────────────────────
# Once every question is answered the bot sends a summary and asks "Is everything correct?" with
# Confirm / Change something buttons. Change → a tappable list of what to change (a product opens a
# second list of its details) → the question again → "Change X to Y? Save it / Keep old" → back to
# the summary. Nothing a customer changes is stored until they confirm it. Stages (Query.bot_stage):
#   ''  collecting answers          'summary'  waiting for Confirm / Change
#   'pick_field' / 'pick_more'  choosing what to change (two lists: Meta allows 10 rows per list)
#   'pick_item_field'  choosing which detail of the product picked (edit_field = 'item:<index>')
#   'editing'  waiting for the new answer (edit_field = 'gst_number', or 'item:<index>:<field>')
#   'confirm_change'  waiting for Save / Keep old          'done'  confirmed
WHATSAPP_REVIEW_STAGES = ('summary', 'pick_field', 'pick_more', 'pick_item_field', 'editing', 'confirm_change')
WHATSAPP_FIELD_ROW_PREFIX = 'field:'
WHATSAPP_ITEM_ROW_PREFIX = 'item:'

# Answers about the whole query, and the details of one product, with the labels used on screen.
WHATSAPP_QUERY_REVIEW_FIELDS = [
    ('company_name', 'Company name'), ('contact_email', 'Email'), ('gst_number', 'GST number & address'),
    ('drawing', 'Drawing / sample'), ('technical_requirements', 'Make/properties/process'), ('end_use', 'End use'),
]
WHATSAPP_ITEM_REVIEW_FIELDS = [
    ('product_category', 'Product type'), ('grade', 'Grade'), ('width', 'Width'), ('thickness', 'Thickness'),
    ('delivery_form', 'Delivery form'), ('quantity', 'Quantity'),
]
_QUERY_LABELS = dict(WHATSAPP_QUERY_REVIEW_FIELDS)
_ITEM_LABELS = dict(WHATSAPP_ITEM_REVIEW_FIELDS)
_REVIEW_ALIASES = {
    'company_name': ('company', 'name'), 'contact_email': ('email', 'mail'), 'gst_number': ('gst', 'address'),
    'drawing': ('drawing', 'sample'), 'technical_requirements': ('make', 'propert', 'process'), 'end_use': ('end use', 'use'),
}
_ITEM_ALIASES = {
    'product_category': ('type', 'category'), 'grade': ('grade',), 'width': ('width',), 'thickness': ('thickness',),
    'delivery_form': ('delivery', 'coil', 'bar'), 'quantity': ('quantity', 'kg'),
}
WHATSAPP_SUMMARY_INTRO = "Here is what we have noted from you:"
WHATSAPP_SUMMARY_QUESTION = "Is everything correct?"
WHATSAPP_SUMMARY_CHOICES = [('confirm', 'Confirm'), ('change', 'Change something')]
WHATSAPP_CHANGE_CHOICES = [('yes', 'Yes, save it'), ('no', 'No, keep old')]


def _trim_number(value):
    return format(value.normalize(), 'f')


def _review_value(query, key):
    """What the summary shows for a query-level answer ('—' when empty)."""
    if key == 'gst_number':
        value = ', '.join(part for part in (query.gst_number, query.gst_address) if part)
    elif key == 'drawing':
        value = "File attached" if query.drawing else query.drawing_notes
    else:
        value = getattr(query, key)
    return str(value).strip() or '—'


def _item_field_value(item, field):
    """What the summary shows for one detail of one product ('—' when empty)."""
    if field == 'product_category':
        value = item.product_category.name if item.product_category_id else ''
    elif field in ('width', 'thickness'):
        number = getattr(item, field)
        value = f"{_trim_number(number)} mm" if number is not None else ''
    elif field == 'quantity':
        value = f"{_trim_number(item.quantity)} kg" if item.quantity is not None else ''
    else:
        value = getattr(item, field)
    return str(value).strip() or '—'


def _product_label(position, count):
    return f"Product {position + 1}" if count > 1 else "Product"


def _summary_text(query):
    items = query.item_list()
    lines = [f"• {_QUERY_LABELS[key]}: {_review_value(query, key)}" for key in ('company_name', 'contact_email', 'gst_number')]
    for position, item in enumerate(items):
        lines.append(f"• {_product_label(position, len(items))}: {item.summary_text() or '—'}")
    lines += [f"• {_QUERY_LABELS[key]}: {_review_value(query, key)}" for key in ('drawing', 'technical_requirements', 'end_use')]
    return WHATSAPP_SUMMARY_INTRO + "\n" + "\n".join(lines)


def _summary_steps(query, lead=''):
    """The summary, then the Confirm / Change buttons — two messages because a buttons message is limited
    to 1024 characters and a summary with an address and several products can be longer."""
    return [('text', (lead + "\n\n" if lead else '') + _summary_text(query)),
            ('buttons', WHATSAPP_SUMMARY_QUESTION, WHATSAPP_SUMMARY_CHOICES)]


def _change_list_step(query, page_two=False):
    """What to change: the first list has company, email, GST and each product (+ More…), the second the
    drawing, make/process and end use (+ Back). Meta allows 10 rows per list; 3 + 5 products + More = 9."""
    items = query.item_list()
    if page_two:
        rows = [(f"{WHATSAPP_FIELD_ROW_PREFIX}{key}", _QUERY_LABELS[key][:24], _review_value(query, key)[:72])
                for key in ('drawing', 'technical_requirements', 'end_use')]
        rows.append(('back', "← Back", "The first options"))
    else:
        rows = [(f"{WHATSAPP_FIELD_ROW_PREFIX}{key}", _QUERY_LABELS[key][:24], _review_value(query, key)[:72])
                for key in ('company_name', 'contact_email', 'gst_number')]
        rows += [(f"{WHATSAPP_ITEM_ROW_PREFIX}{position}", _product_label(position, len(items)) if len(items) > 1 else "Product details",
                  (item.summary_text() or '—')[:72]) for position, item in enumerate(items)]
        rows.append(('more', "More…", "Drawing, make/process, end use"))
    return ('list', "What would you like to change?", "Choose", rows)


def _item_field_list_step(query, index):
    items = query.item_list()
    item = items[index]
    heading = f"{_product_label(index, len(items))}: which detail would you like to change?" if len(items) > 1 else "Which detail would you like to change?"
    rows = [(f"{WHATSAPP_FIELD_ROW_PREFIX}{field}", label[:24], _item_field_value(item, field)[:72])
            for field, label in WHATSAPP_ITEM_REVIEW_FIELDS]
    rows.append(('back', "← Back", "The first options"))
    return ('list', heading, "Choose", rows)


def _edit_target(query):
    """('item', index, field) or ('query', None, field) for what Query.edit_field points at."""
    if query.edit_field.startswith(WHATSAPP_ITEM_ROW_PREFIX):
        parts = query.edit_field.split(':')
        return 'item', int(parts[1]), parts[2] if len(parts) > 2 else ''
    return 'query', None, query.edit_field


def _target_label(query):
    kind, index, field = _edit_target(query)
    if kind == 'query':
        return _QUERY_LABELS.get(field, field).lower()
    label = _ITEM_LABELS.get(field, field).lower()
    return f"product {index + 1} {label}" if query.items.count() > 1 else label


def _pending_display(query, updates):
    """How a held change reads in "Change X to …?"."""
    if updates.get('_pending_file'):
        return "the file you just sent"
    if '_items' in updates:
        (field, values), = updates['_items'].items()
        value = values[0]
        if field == 'product_category':
            return ProductCategory.objects.get(pk=value).name
        if field in ('width', 'thickness'):
            return f"{_trim_number(Decimal(value))} mm"
        if field == 'quantity':
            return f"{_trim_number(Decimal(value))} kg"
        return str(value)
    if 'gst_number' in updates:
        return ', '.join(part for part in (updates.get('gst_number'), updates.get('gst_address', query.gst_address)) if part)
    if 'drawing_notes' in updates:
        return updates['drawing_notes']
    (key, value), = [(k, v) for k, v in updates.items() if not k.startswith('_')][:1] or [('', '')]
    return str(value)


def _confirm_change_steps(query):
    shown = _pending_display(query, query.pending_value)[:700]
    return [('buttons', f"Change {_target_label(query)} to:\n{shown}\n\nSave this change?", WHATSAPP_CHANGE_CHOICES)]


def _says_yes(text):
    return bool(re.match(r"\s*(yes|y\b|yep|ok|okay|sure|confirm|correct|save|right|all good)", text.lower()))


def _says_no(text):
    return bool(re.match(r"\s*(no\b|nope|keep|cancel|don't|do not)", text.lower()))


def _says_change(text):
    return _says_no(text) or text.lower().strip().startswith(('change', 'edit', 'wrong', 'incorrect', 'update'))


def _query_key_from(text):
    """The query-level answer a customer means by a tapped row ('@field:end_use') or by typing."""
    lowered = text.lower().strip()
    if lowered.startswith('@' + WHATSAPP_FIELD_ROW_PREFIX):
        key = lowered[len('@' + WHATSAPP_FIELD_ROW_PREFIX):]
        return key if key in _QUERY_LABELS else None
    for key, label in WHATSAPP_QUERY_REVIEW_FIELDS:
        if key.replace('_', ' ') in lowered or label.lower() in lowered:
            return key
    for key, aliases in _REVIEW_ALIASES.items():
        if any(alias in lowered for alias in aliases):
            return key
    return None


def _item_key_from(text):
    """The detail of a product a customer means by a tapped row or by typing ('the grade')."""
    lowered = text.lower().strip()
    if lowered.startswith('@' + WHATSAPP_FIELD_ROW_PREFIX):
        key = lowered[len('@' + WHATSAPP_FIELD_ROW_PREFIX):]
        return key if key in _ITEM_LABELS else None
    for key, label in WHATSAPP_ITEM_REVIEW_FIELDS:
        if label.lower() in lowered:
            return key
    for key, aliases in _ITEM_ALIASES.items():
        if any(alias in lowered for alias in aliases):
            return key
    return None


def _item_index_from(text, count):
    """Which product ('@item:1', 'product 2', 'second') a customer means; None if unclear."""
    lowered = text.lower().strip()
    if lowered.startswith('@' + WHATSAPP_ITEM_ROW_PREFIX):
        index = lowered[len('@' + WHATSAPP_ITEM_ROW_PREFIX):]
        return int(index) if index.isdigit() and int(index) < count else None
    found = re.search(r'(?:product|item)\s*(\d+)', lowered)
    if found and 1 <= int(found.group(1)) <= count:
        return int(found.group(1)) - 1
    return 0 if count == 1 and re.search(r'product', lowered) else None


def _review_reply(query, text, window_open):
    """Handle a customer's message while they are reviewing the summary. Everything sent is skipped (but
    state still moves on) once the 24-hour window has closed — staff were already told."""
    def send(steps):
        if window_open:
            _send_whatsapp_steps_background(query.contact_phone, steps)

    def save(*fields):
        query.save(update_fields=list(fields))

    def back_to_summary(lead=''):
        query.bot_stage, query.edit_field, query.pending_value = 'summary', '', {}
        if query.pending_drawing:
            query.pending_drawing.delete(save=False)
            query.pending_drawing = None
        save('bot_stage', 'edit_field', 'pending_value', 'pending_drawing')
        send(_summary_steps(query, lead))

    def ask_for(field, count=1, lead=''):
        if field == 'company_name':
            return [('text', lead + "What is your company name?")]
        step = _question_step(field, count)
        return [(step[0], lead + step[1], *step[2:])] if lead and step[0] == 'text' else [step]

    stage, lowered = query.bot_stage, text.lower().strip()
    items = query.item_list()

    if stage == 'summary':
        if _says_yes(lowered) and not lowered.startswith('change'):
            query.bot_stage = 'done'
            query.intake_confirmed_at = timezone.now()
            save('bot_stage', 'intake_confirmed_at')
            send([('text', WHATSAPP_CLOSING_MESSAGE)])
        elif _says_change(lowered):
            query.bot_stage = 'pick_field'
            save('bot_stage')
            send([_change_list_step(query)])
        else:
            send(_summary_steps(query)[1:])   # not understood: just the buttons again
        return

    if stage in ('pick_field', 'pick_more'):
        if lowered == '@more':
            query.bot_stage = 'pick_more'
            save('bot_stage')
            send([_change_list_step(query, page_two=True)])
            return
        if lowered == '@back':
            query.bot_stage = 'pick_field'
            save('bot_stage')
            send([_change_list_step(query)])
            return
        if lowered.startswith('cancel'):
            back_to_summary()
            return
        index = _item_index_from(text, len(items))
        if index is not None:   # a product: next, which of its details
            item_key = _item_key_from(text) if not lowered.startswith('@') else None
            if item_key and len(items) == 1:   # "the width" with a single product: straight to it
                query.bot_stage, query.edit_field = 'editing', f"item:{index}:{item_key}"
                save('bot_stage', 'edit_field')
                send(ask_for(item_key))
                return
            query.bot_stage, query.edit_field = 'pick_item_field', f"{WHATSAPP_ITEM_ROW_PREFIX}{index}"
            save('bot_stage', 'edit_field')
            send([_item_field_list_step(query, index)])
            return
        key = _query_key_from(text)
        if key is None and len(items) == 1:   # typed a product detail with one product: go straight to it
            item_key = _item_key_from(text)
            if item_key:
                query.bot_stage, query.edit_field = 'editing', f"item:0:{item_key}"
                save('bot_stage', 'edit_field')
                send(ask_for(item_key))
                return
        if key is None:
            send([_change_list_step(query, page_two=(stage == 'pick_more'))])
            return
        query.bot_stage, query.edit_field = 'editing', key
        save('bot_stage', 'edit_field')
        send(ask_for(key))
        return

    if stage == 'pick_item_field':
        index = _edit_target(query)[1]
        if lowered.startswith('cancel'):
            back_to_summary()
            return
        if lowered == '@back':
            query.bot_stage, query.edit_field = 'pick_field', ''
            save('bot_stage', 'edit_field')
            send([_change_list_step(query)])
            return
        key = _item_key_from(text)
        if key is None:
            send([_item_field_list_step(query, index)])
            return
        query.bot_stage, query.edit_field = 'editing', f"item:{index}:{key}"
        save('bot_stage', 'edit_field')
        lead = f"{_product_label(index, len(items))}: " if len(items) > 1 else ''
        send(ask_for(key, lead=lead))
        return

    if stage == 'editing':
        if lowered in ('cancel', 'back'):
            back_to_summary()
            return
        kind, index, field = _edit_target(query)
        try:
            updates = _parse_answer(field, text.strip(), 1)   # one value: it is one product's detail
        except _InvalidAnswer as invalid:
            if invalid.say:
                send([('text', invalid.say)])
            if invalid.ask:
                send([_question_step(invalid.ask)])
            return
        if kind == 'item':
            updates['_item_only'] = index
        query.pending_value, query.bot_stage = updates, 'confirm_change'
        save('pending_value', 'bot_stage')
        send(_confirm_change_steps(query))
        return

    if stage == 'confirm_change':
        if _says_yes(lowered):
            changed = _apply_updates(query, query.pending_value, editing=True)
            query.pending_value, query.bot_stage, query.edit_field = {}, 'summary', ''
            query.save(update_fields=changed + ['pending_value', 'bot_stage', 'edit_field'])
            send(_summary_steps(query, "Done, I've updated that."))
        elif _says_no(lowered):
            back_to_summary("No problem, I've kept it as it was.")
        else:
            send(_confirm_change_steps(query))


def _question_step(field, count=1):
    """The step that asks `field`'s question (a list or buttons for the tappable ones)."""
    text = _question_text(field, count)
    if field == 'item_count':
        return ('list', text, "Choose number", _item_count_rows())
    if field == 'product_category' and count <= 1:
        rows = _product_category_rows()
        if rows:
            return ('list', text, "Choose type", rows)
        return ('text', "Which product type do you need? Please type its name.")
    if field in WHATSAPP_QUERY_CHOICES and count <= 1:
        return ('buttons', text, WHATSAPP_QUERY_CHOICES[field])
    return ('text', text)
