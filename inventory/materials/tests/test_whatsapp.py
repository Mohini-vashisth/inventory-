"""The WhatsApp intake bot: webhook, question sequence, drawing media download."""

import hashlib
import hmac
import json
import tempfile

from decimal import Decimal
from django.contrib.auth.models import User
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from unittest.mock import patch, MagicMock

from ..models import ProductType, Query
from ..views import whatsapp
from ..views.whatsapp import WhatsAppSendError, WHATSAPP_QUERY_QUESTIONS, WHATSAPP_CLOSING_MESSAGE


def _sign_whatsapp_payload(body_bytes, secret):
    digest = hmac.new(secret.encode('utf-8'), body_bytes, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


@override_settings(WHATSAPP_VERIFY_TOKEN='test-verify-token', WHATSAPP_APP_SECRET='test-app-secret')
class WhatsAppWebhookTests(TestCase):
    """Meta's Cloud API is the only caller of this endpoint — GET performs
    the one-time subscription handshake, POST delivers inbound messages.
    Both are unauthenticated by Django's usual means, so signature/token
    verification IS the security boundary being tested here."""

    def _post_payload(self, payload, secret='test-app-secret'):
        body = json.dumps(payload).encode('utf-8')
        return self.client.post(
            reverse('whatsapp_webhook'), data=body, content_type='application/json',
            HTTP_X_HUB_SIGNATURE_256=_sign_whatsapp_payload(body, secret),
        )

    def _message_payload(self, phone, text, profile_name=None):
        value = {'messages': [{'from': phone, 'type': 'text', 'text': {'body': text}}]}
        if profile_name is not None:
            value['contacts'] = [{'profile': {'name': profile_name}}]
        return {'entry': [{'changes': [{'value': value}]}]}

    def _media_payload(self, phone, media_type, media_id, mime_type='image/jpeg'):
        value = {'messages': [{'from': phone, 'type': media_type, media_type: {'id': media_id, 'mime_type': mime_type}}]}
        return {'entry': [{'changes': [{'value': value}]}]}

    def test_get_handshake_succeeds_with_correct_verify_token(self):
        response = self.client.get(reverse('whatsapp_webhook'), {
            'hub.mode': 'subscribe', 'hub.verify_token': 'test-verify-token', 'hub.challenge': '12345',
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content.decode(), '12345')

    def test_get_handshake_rejects_wrong_verify_token(self):
        response = self.client.get(reverse('whatsapp_webhook'), {
            'hub.mode': 'subscribe', 'hub.verify_token': 'wrong-token', 'hub.challenge': '12345',
        })
        self.assertEqual(response.status_code, 403)

    @override_settings(WHATSAPP_VERIFY_TOKEN='')
    def test_get_handshake_rejects_when_verify_token_unset(self):
        # An unset WHATSAPP_VERIFY_TOKEN must never "verify" anything, even
        # a request that also omits hub.verify_token ('' == '' would
        # otherwise pass).
        response = self.client.get(reverse('whatsapp_webhook'), {
            'hub.mode': 'subscribe', 'hub.challenge': '12345',
        })
        self.assertEqual(response.status_code, 403)

    def test_post_with_no_matching_query_creates_bare_query(self):
        payload = self._message_payload('919876543210', 'Need 500kg EN8D', profile_name='Ramesh Traders')
        response = self._post_payload(payload)

        self.assertEqual(response.status_code, 200)
        query = Query.objects.get(contact_phone='919876543210')
        self.assertEqual(query.source, 'whatsapp')
        self.assertEqual(query.company_name, 'Ramesh Traders')
        self.assertIn('Need 500kg EN8D', query.notes)

    def test_post_does_not_reuse_converted_query(self):
        Query.objects.create(
            source='whatsapp', contact_phone='919876543210', notes='old conversation', status='converted',
        )
        payload = self._message_payload('919876543210', 'new inquiry')
        self._post_payload(payload)

        self.assertEqual(Query.objects.filter(contact_phone='919876543210').count(), 2)
        new_query = Query.objects.filter(contact_phone='919876543210', status='new').get()
        self.assertEqual(new_query.notes, 'new inquiry')

    @patch('materials.views.whatsapp._send_whatsapp_text_message_background')
    def test_inbound_answer_captures_company_name_then_asks_email(self, mock_send):
        Query.objects.create(source='call', contact_phone='919876543210')
        payload = self._message_payload('919876543210', 'Ramesh Traders')
        self._post_payload(payload)

        query = Query.objects.get(contact_phone='919876543210')
        self.assertEqual(query.company_name, 'Ramesh Traders')
        mock_send.assert_called_once_with('919876543210', WHATSAPP_QUERY_QUESTIONS['contact_email'])

    @patch('materials.views.whatsapp._send_whatsapp_text_message_background')
    def test_inbound_answer_captures_email_then_asks_grade(self, mock_send):
        Query.objects.create(source='call', contact_phone='919876543210', company_name='Ramesh Traders')
        payload = self._message_payload('919876543210', 'ramesh@example.com')
        self._post_payload(payload)

        query = Query.objects.get(contact_phone='919876543210')
        self.assertEqual(query.contact_email, 'ramesh@example.com')
        mock_send.assert_called_once_with('919876543210', WHATSAPP_QUERY_QUESTIONS['grade'])

    @patch('materials.views.whatsapp._send_whatsapp_text_message_background')
    def test_inbound_answer_captures_grade_then_asks_size(self, mock_send):
        Query.objects.create(
            source='call', contact_phone='919876543210',
            company_name='Ramesh Traders', contact_email='ramesh@example.com',
        )
        payload = self._message_payload('919876543210', 'EN8D')
        self._post_payload(payload)

        query = Query.objects.get(contact_phone='919876543210')
        self.assertEqual(query.grade, 'EN8D')
        mock_send.assert_called_once_with('919876543210', WHATSAPP_QUERY_QUESTIONS['size'])

    @patch('materials.views.whatsapp._send_whatsapp_text_message_background')
    def test_inbound_size_answer_asks_for_drawing_next(self, mock_send):
        Query.objects.create(
            source='call', contact_phone='919876543210', company_name='Ramesh Traders',
            contact_email='ramesh@example.com', grade='EN8D',
        )
        payload = self._message_payload('919876543210', '1.2')
        self._post_payload(payload)

        query = Query.objects.get(contact_phone='919876543210')
        self.assertEqual(query.size, Decimal('1.2'))
        mock_send.assert_called_once_with('919876543210', WHATSAPP_QUERY_QUESTIONS['drawing'])

    @patch('materials.views.whatsapp._send_whatsapp_text_message_background')
    def test_text_reply_to_drawing_question_saves_as_drawing_notes_and_asks_for_notes_next(self, mock_send):
        Query.objects.create(
            source='call', contact_phone='919876543210', company_name='Ramesh Traders',
            contact_email='ramesh@example.com', grade='EN8D', size=Decimal('1.2'),
        )
        self._post_payload(self._message_payload('919876543210', 'no'))

        query = Query.objects.get(contact_phone='919876543210')
        self.assertEqual(query.drawing_notes, 'no')
        self.assertFalse(query.drawing)
        mock_send.assert_called_once_with('919876543210', WHATSAPP_QUERY_QUESTIONS['notes'])

    @patch('materials.views.whatsapp._send_whatsapp_text_message_background')
    def test_notes_answer_completes_sequence_and_sends_closing(self, mock_send):
        Query.objects.create(
            source='call', contact_phone='919876543210', company_name='Ramesh Traders',
            contact_email='ramesh@example.com', grade='EN8D', size=Decimal('1.2'), drawing_notes='no',
        )
        self._post_payload(self._message_payload('919876543210', 'needs to be corrosion resistant'))

        query = Query.objects.get(contact_phone='919876543210')
        self.assertEqual(query.notes, 'needs to be corrosion resistant')
        mock_send.assert_called_once_with('919876543210', WHATSAPP_CLOSING_MESSAGE)

    @patch('materials.views.whatsapp._send_whatsapp_text_message_background')
    def test_image_reply_to_drawing_question_is_routed_for_download(self, mock_send):
        """The actual download happens in a background thread — this just
        confirms the webhook recognizes an image reply as answering the
        drawing question and hands it off, without touching drawing_notes
        (a text reply would) or advancing past 'drawing' synchronously."""
        Query.objects.create(
            source='call', contact_phone='919876543210', company_name='Ramesh Traders',
            contact_email='ramesh@example.com', grade='EN8D', size=Decimal('1.2'),
        )
        with patch('materials.views.whatsapp._process_whatsapp_drawing_media_background') as mock_bg:
            self._post_payload(self._media_payload('919876543210', 'image', 'media-id-123', 'image/jpeg'))
            mock_bg.assert_called_once()
            args = mock_bg.call_args[0]
            self.assertEqual(args[1], 'media-id-123')
            self.assertEqual(args[2], 'image/jpeg')
        query = Query.objects.get(contact_phone='919876543210')
        self.assertEqual(query.drawing_notes, '')
        mock_send.assert_not_called()

    @patch('materials.views.whatsapp._send_whatsapp_text_message_background')
    def test_image_reply_outside_drawing_question_is_ignored(self, mock_send):
        Query.objects.create(source='call', contact_phone='919876543210', company_name='Ramesh Traders')
        with patch('materials.views.whatsapp._process_whatsapp_drawing_media_background') as mock_bg:
            self._post_payload(self._media_payload('919876543210', 'image', 'media-id-456'))
            mock_bg.assert_not_called()

    @patch('materials.views.whatsapp._send_whatsapp_text_message_background')
    def test_inbound_size_with_units_is_parsed(self, mock_send):
        Query.objects.create(
            source='call', contact_phone='919876543210', company_name='Ramesh Traders',
            contact_email='ramesh@example.com', grade='EN8D',
        )
        self._post_payload(self._message_payload('919876543210', '1.2mm'))

        query = Query.objects.get(contact_phone='919876543210')
        self.assertEqual(query.size, Decimal('1.2'))

    @patch('materials.views.whatsapp._send_whatsapp_text_message_background')
    def test_inbound_unparseable_size_reasks_without_saving(self, mock_send):
        Query.objects.create(
            source='call', contact_phone='919876543210', company_name='Ramesh Traders',
            contact_email='ramesh@example.com', grade='EN8D',
        )
        self._post_payload(self._message_payload('919876543210', 'not sure'))

        query = Query.objects.get(contact_phone='919876543210')
        self.assertIsNone(query.size)
        mock_send.assert_called_once_with('919876543210', WHATSAPP_QUERY_QUESTIONS['size'])

    @patch('materials.views.whatsapp._send_whatsapp_text_message_background')
    def test_inbound_negative_size_reasks_without_saving(self, mock_send):
        Query.objects.create(
            source='call', contact_phone='919876543210', company_name='Ramesh Traders',
            contact_email='ramesh@example.com', grade='EN8D',
        )
        self._post_payload(self._message_payload('919876543210', '-1.2'))

        query = Query.objects.get(contact_phone='919876543210')
        self.assertIsNone(query.size)
        mock_send.assert_called_once_with('919876543210', WHATSAPP_QUERY_QUESTIONS['size'])

    @patch('materials.views.whatsapp._send_whatsapp_text_message_background')
    def test_inbound_invalid_email_reasks_without_saving(self, mock_send):
        Query.objects.create(source='call', contact_phone='919876543210', company_name='Ramesh Traders')
        self._post_payload(self._message_payload('919876543210', 'not an email'))

        query = Query.objects.get(contact_phone='919876543210')
        self.assertEqual(query.contact_email, '')
        mock_send.assert_called_once_with('919876543210', WHATSAPP_QUERY_QUESTIONS['contact_email'])

    @patch('materials.views.whatsapp._send_whatsapp_text_message_background')
    def test_size_completion_matches_existing_product_type(self, mock_send):
        product_type = ProductType.objects.create(item_code='Matched Bar', grade='EN8D', size='1.200')
        Query.objects.create(
            source='call', contact_phone='919876543210', company_name='Ramesh Traders',
            contact_email='ramesh@example.com', grade='en8d',
        )
        self._post_payload(self._message_payload('919876543210', '1.2'))

        query = Query.objects.get(contact_phone='919876543210')
        self.assertEqual(query.product_type, product_type)

    @patch('materials.views.whatsapp._send_whatsapp_text_message_background')
    def test_size_completion_leaves_product_type_null_when_no_match(self, mock_send):
        Query.objects.create(
            source='call', contact_phone='919876543210', company_name='Ramesh Traders',
            contact_email='ramesh@example.com', grade='EN8D',
        )
        self._post_payload(self._message_payload('919876543210', '9.9'))

        query = Query.objects.get(contact_phone='919876543210')
        self.assertIsNone(query.product_type)

    @patch('materials.views.whatsapp._send_whatsapp_text_message_background')
    def test_size_completion_requires_both_grade_and_size_to_match(self, mock_send):
        # Same grade, different size — and same size, different grade —
        # must NOT match; only a ProductType agreeing on both should link.
        ProductType.objects.create(item_code='Wrong Size Bar', grade='EN8D', size='2.500')
        ProductType.objects.create(item_code='Wrong Grade Bar', grade='SS304', size='1.200')
        Query.objects.create(
            source='call', contact_phone='919876543210', company_name='Ramesh Traders',
            contact_email='ramesh@example.com', grade='EN8D',
        )
        self._post_payload(self._message_payload('919876543210', '1.2'))

        query = Query.objects.get(contact_phone='919876543210')
        self.assertIsNone(query.product_type)

    @patch('materials.views.whatsapp._send_whatsapp_text_message_background')
    def test_message_after_sequence_complete_is_appended_to_notes(self, mock_send):
        Query.objects.create(
            source='call', contact_phone='919876543210', company_name='Ramesh Traders',
            contact_email='ramesh@example.com', grade='EN8D', size=Decimal('1.2'),
            drawing_notes='no', notes='standard requirement',
        )
        self._post_payload(self._message_payload('919876543210', 'also need it urgently'))

        query = Query.objects.get(contact_phone='919876543210')
        self.assertIn('also need it urgently', query.notes)
        self.assertIn('standard requirement', query.notes)
        mock_send.assert_not_called()

    @patch('materials.views.whatsapp._send_whatsapp_text_message')
    def test_answer_capture_survives_send_failure(self, mock_send):
        mock_send.side_effect = WhatsAppSendError("boom")
        Query.objects.create(source='call', contact_phone='919876543210')
        self._post_payload(self._message_payload('919876543210', 'Ramesh Traders'))

        query = Query.objects.get(contact_phone='919876543210')
        self.assertEqual(query.company_name, 'Ramesh Traders')

    @patch('materials.views.whatsapp._send_whatsapp_text_message')
    def test_background_send_logs_and_swallows_failure(self, mock_send):
        # _send_whatsapp_text_message_background is what _process_whatsapp_answer
        # actually calls — it must never let a send failure propagate (the
        # webhook has already acked by the time this thread runs), but a
        # persistent failure (e.g. an expired access token) should still be
        # visible somewhere, via a log line.
        mock_send.side_effect = WhatsAppSendError("boom")
        with self.assertLogs('materials.views', level='WARNING') as logs:
            thread = whatsapp._send_whatsapp_text_message_background('919876543210', 'hello')
            thread.join(timeout=2)
        self.assertTrue(any('919876543210' in line and 'boom' in line for line in logs.output))

    def test_post_with_invalid_signature_is_rejected(self):
        payload = self._message_payload('919876543210', 'Need 500kg EN8D')
        response = self._post_payload(payload, secret='wrong-secret')

        self.assertEqual(response.status_code, 403)
        self.assertEqual(Query.objects.count(), 0)

    def test_post_with_missing_signature_header_is_rejected(self):
        body = json.dumps(self._message_payload('919876543210', 'Need 500kg EN8D')).encode('utf-8')
        response = self.client.post(reverse('whatsapp_webhook'), data=body, content_type='application/json')

        self.assertEqual(response.status_code, 403)
        self.assertEqual(Query.objects.count(), 0)

    @override_settings(WHATSAPP_APP_SECRET='')
    def test_post_is_rejected_when_app_secret_unset(self):
        # An unset WHATSAPP_APP_SECRET must never validate anything — HMAC
        # keyed with an empty string is a key anyone can also compute, so
        # this must reject even a signature computed the "correct" way
        # against that same empty secret.
        payload = self._message_payload('919876543210', 'Need 500kg EN8D')
        response = self._post_payload(payload, secret='')

        self.assertEqual(response.status_code, 403)
        self.assertEqual(Query.objects.count(), 0)

    @patch('materials.views.whatsapp._send_whatsapp_text_message_background')
    @patch('materials.views.whatsapp._send_whatsapp_template_message')
    def test_phone_with_symbols_still_routes_to_open_query(self, mock_template_send, mock_text_send):
        # Staff may log a number formatted like the UI's own placeholder
        # ("+91 98765 43210") — Meta's inbound "from" is always digits-only,
        # so the stored value must be normalized the same way at write time
        # for the two to ever match.
        staff = User.objects.create_user('symbol_phone_staff', password='pw', is_staff=True)
        self.client.force_login(staff)
        self.client.post(reverse('query_dashboard'), {
            'source': 'call', 'contact_phone': '+91 98765 43210',
        })

        payload = self._message_payload('919876543210', 'Ramesh Traders')
        self._post_payload(payload)

        self.assertEqual(Query.objects.filter(contact_phone='919876543210').count(), 1)
        query = Query.objects.get(contact_phone='919876543210')
        self.assertEqual(query.company_name, 'Ramesh Traders')

    def test_post_ignores_status_payloads(self):
        payload = {'entry': [{'changes': [{'value': {'statuses': [{'id': 'abc', 'status': 'delivered'}]}}]}]}
        response = self._post_payload(payload)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(Query.objects.count(), 0)


class WhatsAppDrawingMediaTests(TransactionTestCase):
    """The background thread that actually downloads a drawing image/PDF
    and saves it to Query.drawing — run synchronously in these tests via
    threading.Thread.join(), since the thread itself is real (only the
    network calls inside it are mocked). TransactionTestCase, not TestCase:
    a real background thread opens its own DB connection and does a real
    write, which deadlocks against TestCase's outer held-open transaction
    on SQLite ("database table is locked") — TransactionTestCase commits
    per-operation instead, so the thread's own connection can actually see
    and write the row."""

    def setUp(self):
        # Real writes land on disk under TransactionTestCase (no rollback
        # to undo them) — isolate to a temp dir instead of the project's
        # own local media/ folder.
        self._media_tmp = tempfile.TemporaryDirectory()
        self._media_override = override_settings(MEDIA_ROOT=self._media_tmp.name)
        self._media_override.enable()
        self.addCleanup(self._media_override.disable)
        self.addCleanup(self._media_tmp.cleanup)

    def _run_and_wait(self, query_pk, media_id, mime_type):
        thread = whatsapp._process_whatsapp_drawing_media_background(query_pk, media_id, mime_type)
        thread.join(timeout=5)

    @override_settings(WHATSAPP_ACCESS_TOKEN='test-token', WHATSAPP_PHONE_NUMBER_ID='123')
    @patch('materials.views.whatsapp._send_whatsapp_text_message')
    @patch('materials.views.whatsapp._download_whatsapp_media')
    def test_downloads_and_saves_drawing_then_advances(self, mock_download, mock_send):
        mock_download.return_value = (b'%PDF-1.4 fake pdf bytes', 'application/pdf')
        query = Query.objects.create(
            source='whatsapp', contact_phone='919876543210', company_name='Drawing Co',
            contact_email='drawing@example.com', grade='EN8D', size=Decimal('1.2'),
        )

        self._run_and_wait(query.pk, 'media-id-1', 'application/pdf')

        query.refresh_from_db()
        self.assertTrue(query.drawing)
        self.assertTrue(query.drawing.name.endswith('.pdf'))
        self.assertEqual(query.drawing.read()[:4], b'%PDF')
        self.assertEqual(query.drawing_notes, 'Drawing attached via WhatsApp')
        mock_send.assert_called_once_with('919876543210', WHATSAPP_QUERY_QUESTIONS['notes'])
        query.drawing.delete(save=False)

    @patch('materials.views.whatsapp._send_whatsapp_text_message')
    @patch('materials.views.whatsapp._download_whatsapp_media')
    def test_download_failure_leaves_query_untouched(self, mock_download, mock_send):
        mock_download.side_effect = WhatsAppSendError("network boom")
        query = Query.objects.create(
            source='whatsapp', contact_phone='919876543210', company_name='Failed Drawing Co',
            grade='EN8D', size=Decimal('1.2'),
        )

        self._run_and_wait(query.pk, 'media-id-2', 'application/pdf')

        query.refresh_from_db()
        self.assertFalse(query.drawing)
        self.assertEqual(query.drawing_notes, '')
        mock_send.assert_not_called()

    @patch('materials.views.whatsapp._send_whatsapp_text_message')
    @patch('materials.views.whatsapp._download_whatsapp_media')
    def test_does_not_overwrite_if_already_answered_by_text(self, mock_download, mock_send):
        """A race: the customer replies 'no' by text right as an earlier
        image they sent finishes downloading — the text answer (already
        saved by the time this background thread gets the lock) must win,
        not get silently overwritten by the late-arriving image."""
        mock_download.return_value = (b'\xff\xd8\xff fake jpeg bytes', 'image/jpeg')
        query = Query.objects.create(
            source='whatsapp', contact_phone='919876543210', company_name='Race Co',
            grade='EN8D', size=Decimal('1.2'), drawing_notes='no',
        )

        self._run_and_wait(query.pk, 'media-id-3', 'image/jpeg')

        query.refresh_from_db()
        self.assertFalse(query.drawing)
        self.assertEqual(query.drawing_notes, 'no')
        mock_send.assert_not_called()


class WhatsAppMediaDownloadTests(TestCase):
    """_download_whatsapp_media itself — the two-step Graph API fetch
    (resolve media id -> signed URL, then download the bytes)."""

    @override_settings(WHATSAPP_ACCESS_TOKEN='')
    def test_raises_when_access_token_unset(self):
        with self.assertRaises(WhatsAppSendError):
            whatsapp._download_whatsapp_media('media-id')

    @override_settings(WHATSAPP_ACCESS_TOKEN='test-token')
    @patch('materials.views.whatsapp.urllib.request.urlopen')
    def test_downloads_bytes_from_resolved_url(self, mock_urlopen):
        meta_response = MagicMock()
        meta_response.read.return_value = json.dumps({
            'url': 'https://lookaside.fbsbx.com/whatsapp_media/fake',
            'mime_type': 'image/jpeg',
        }).encode('utf-8')
        meta_response.__enter__ = lambda self: meta_response
        meta_response.__exit__ = lambda self, *a: None

        data_response = MagicMock()
        data_response.read.return_value = b'\xff\xd8\xff real-looking jpeg bytes'
        data_response.__enter__ = lambda self: data_response
        data_response.__exit__ = lambda self, *a: None

        mock_urlopen.side_effect = [meta_response, data_response]

        content, mime_type = whatsapp._download_whatsapp_media('media-id-abc')
        self.assertEqual(content, b'\xff\xd8\xff real-looking jpeg bytes')
        self.assertEqual(mime_type, 'image/jpeg')
        self.assertEqual(mock_urlopen.call_count, 2)

    @override_settings(WHATSAPP_ACCESS_TOKEN='test-token')
    @patch('materials.views.whatsapp.urllib.request.urlopen')
    def test_missing_url_in_metadata_raises(self, mock_urlopen):
        meta_response = MagicMock()
        meta_response.read.return_value = json.dumps({'mime_type': 'image/jpeg'}).encode('utf-8')
        meta_response.__enter__ = lambda self: meta_response
        meta_response.__exit__ = lambda self, *a: None
        mock_urlopen.return_value = meta_response

        with self.assertRaises(WhatsAppSendError):
            whatsapp._download_whatsapp_media('media-id-def')

    def test_extension_for_mime_type(self):
        self.assertEqual(whatsapp._extension_for_mime_type('application/pdf'), '.pdf')
        self.assertEqual(whatsapp._extension_for_mime_type('image/jpeg'), '.jpg')
        self.assertEqual(whatsapp._extension_for_mime_type('image/jpeg; charset=binary'), '.jpg')
        self.assertEqual(whatsapp._extension_for_mime_type('application/octet-stream'), '')
        self.assertEqual(whatsapp._extension_for_mime_type(''), '')
