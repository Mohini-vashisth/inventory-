"""WhatsApp Cloud API intake bot: the public webhook, sending, and the question sequence."""

import hashlib
import hmac
import json
import logging
import re
import threading
import urllib.error
import urllib.request

from django.conf import settings
from django.db import transaction, connections
from django.core.files.base import ContentFile
from django.http import HttpResponse, HttpResponseForbidden
from django.utils.crypto import constant_time_compare
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
from decimal import Decimal, InvalidOperation
from django.core.exceptions import ValidationError
from django.core.validators import validate_email

from ..models import ProductType, Query

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
# sync with the actual data.
WHATSAPP_QUERY_FIELDS = ['company_name', 'contact_email', 'grade', 'size', 'drawing', 'notes']


WHATSAPP_QUERY_QUESTIONS = {
    'contact_email': "Thanks! What's the best email address to send your quote to?",
    'grade': "Got it. Which grade do you need (e.g. EN8D, EN9)?",
    'size': "And what size do you need (in mm), e.g. 1.2?",
    'drawing': "Do you have a drawing for the final product? You can send a photo or PDF here, or just reply 'no' if you don't have one.",
    'notes': "Got it. Any other special requirements we should know about? Reply 'no' if none.",
}


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


def _next_expected_query_field(query):
    """The next blank field in the fixed intake order, or None once every
    field is filled. 'drawing' is special: a FileField alone can't tell
    "not asked yet" apart from "asked, customer had none" — drawing_notes
    (set either way, see _process_whatsapp_answer/
    _process_whatsapp_drawing_media_background) is what actually marks
    that question answered, whether or not a file came with it."""
    for field in WHATSAPP_QUERY_FIELDS:
        if field == 'size':
            if query.size is None:
                return field
        elif field == 'drawing':
            if not query.drawing and not query.drawing_notes:
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


def _parse_whatsapp_size(text):
    """Lenient size parsing — "1.2", "1.2mm", "1.2 mm" all become
    Decimal('1.2'); anything with no usable digits, or a zero/negative
    result, returns None so the caller can re-ask instead of saving a
    coil size that could never be real."""
    cleaned = re.sub(r'[^0-9.\-]', '', text or '')
    if not cleaned:
        return None
    try:
        value = Decimal(cleaned)
    except InvalidOperation:
        return None
    return value if value > 0 else None


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
    backgrounding (see _route_whatsapp_media) — every other message type
    (audio/video/location/etc.) is still not handled."""
    incoming_messages = value.get("messages")
    if not incoming_messages:
        return

    contacts = value.get("contacts", [])
    profile_name = contacts[0].get("profile", {}).get("name", "") if contacts else ""

    for msg in incoming_messages:
        phone = msg.get("from", "")
        if not phone:
            continue
        msg_type = msg.get("type")
        if msg_type == "text":
            text = (msg.get("text") or {}).get("body", "").strip()
            if text:
                _route_whatsapp_message(phone, text, profile_name)
        elif msg_type in ("image", "document"):
            media = msg.get(msg_type) or {}
            media_id = media.get("id")
            if media_id:
                _route_whatsapp_media(phone, media_id, media.get("mime_type", ""))


def _route_whatsapp_message(phone, text, profile_name):
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
        _process_whatsapp_answer(query.pk, text)
    else:
        Query.objects.create(source='whatsapp', company_name=profile_name, contact_phone=phone, notes=text)


def _route_whatsapp_media(phone, media_id, mime_type):
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
    if not query or _next_expected_query_field(query) != 'drawing':
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
                if _next_expected_query_field(query) != 'drawing':
                    return  # already answered some other way (e.g. a race with a text reply)
                filename = f"drawing{_extension_for_mime_type(resolved_mime or mime_type)}"
                query.drawing.save(filename, ContentFile(content), save=False)
                query.drawing_notes = "Drawing attached via WhatsApp"
                query.save(update_fields=['drawing', 'drawing_notes'])
                _advance_whatsapp_query(query)
        finally:
            connections.close_all()
    thread = threading.Thread(target=_work, daemon=True)
    thread.start()
    return thread


def _process_whatsapp_answer(query_pk, text):
    """Save this message as the answer to whichever question is next in
    the intake sequence, then send the following question — or, once the
    sequence is complete, try to auto-match an existing ProductType and
    send the closing message. A message that arrives after the sequence
    is already done is just appended to notes, not mistaken for an answer.

    Re-fetches and locks the row inside a transaction (select_for_update)
    rather than trusting the caller's already-read Query instance — Meta
    can redeliver the same webhook, and two overlapping deliveries for the
    same phone must not both read the same "next field" and race each
    other into the wrong column. A no-op on SQLite (no row locking there),
    but real protection once/if this ever runs on Postgres."""
    with transaction.atomic():
        query = Query.objects.select_for_update().get(pk=query_pk)

        field = _next_expected_query_field(query)
        if field is None:
            stamp = timezone.now().strftime('%d %b %H:%M')
            query.notes = f"{query.notes}\n[{stamp}] {text}".strip()
            query.save(update_fields=['notes'])
            return

        if field == 'size':
            parsed = _parse_whatsapp_size(text)
            if parsed is None:
                _send_whatsapp_text_message_background(query.contact_phone, WHATSAPP_QUERY_QUESTIONS['size'])
                return
            query.size = parsed
            changed = ['size']
        elif field == 'contact_email':
            candidate = text.strip()
            if not _is_valid_whatsapp_email(candidate):
                _send_whatsapp_text_message_background(query.contact_phone, WHATSAPP_QUERY_QUESTIONS['contact_email'])
                return
            query.contact_email = candidate
            changed = ['contact_email']
        elif field == 'drawing':
            # A text reply here means no attachment came with it — "no",
            # or a short description instead of a photo/PDF. An actual
            # image/document reply is handled separately, in the
            # background, by _process_whatsapp_drawing_media_background.
            query.drawing_notes = text.strip()
            changed = ['drawing_notes']
        else:
            setattr(query, field, text.strip())
            changed = [field]
        query.save(update_fields=changed)

        _advance_whatsapp_query(query)


def _advance_whatsapp_query(query):
    """Sends the next question in the intake sequence, or the closing
    message once it's complete. Tries to auto-match an existing
    ProductType as soon as grade+size are both known — not only once the
    whole sequence finishes, since drawing/notes come after size now and
    waiting for those too would just delay a match that's already
    possible. Shared by the text-answer path above and the drawing-media
    path (_process_whatsapp_drawing_media_background), since both need to
    advance the same way once their field is saved."""
    if query.grade and query.size is not None and not query.product_type_id:
        match = ProductType.objects.filter(grade__iexact=query.grade, size=query.size).first()
        if match:
            query.product_type = match
            query.save(update_fields=['product_type'])

    next_field = _next_expected_query_field(query)
    if next_field:
        _send_whatsapp_text_message_background(query.contact_phone, WHATSAPP_QUERY_QUESTIONS[next_field])
    else:
        _send_whatsapp_text_message_background(query.contact_phone, WHATSAPP_CLOSING_MESSAGE)
