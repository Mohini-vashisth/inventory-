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

from ..models import ProductCategory, ProductType, Query
from ..views import whatsapp
from ..views.whatsapp import (
    WhatsAppSendError, WHATSAPP_CLOSING_MESSAGE, WHATSAPP_GST_INVALID_MESSAGE, _detect_product_category,
    WHATSAPP_QUERY_CHOICES, WHATSAPP_QUERY_FIELDS, WHATSAPP_QUERY_QUESTIONS,
)


def _sign_whatsapp_payload(body_bytes, secret):
    digest = hmac.new(secret.encode('utf-8'), body_bytes, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


# A plausible reply to every question, in the order the bot asks them.
ANSWERS = {
    'company_name': 'Ramesh Traders', 'contact_email': 'ramesh@example.com',
    'gst_number': '22AAAAA0000A1Z5', 'gst_address': '12 Industrial Area, Faridabad',
    'product_description': 'Round bar', 'drawing': 'no', 'grade': 'EN8D',
    'technical_requirements': 'no', 'end_use': 'automotive shafts', 'delivery_form': 'Coil',
    'quantity_text': '2 tons monthly',
}


def _answered_field(field):
    return 'drawing_notes' if field == 'drawing' else field


def _query_awaiting(field, phone='919876543210'):
    """A Query that has answered every question before `field`, so `field`
    is the one the bot is waiting on. Pass None for a fully answered one."""
    values = {}
    for name in WHATSAPP_QUERY_FIELDS:
        if name == field:
            break
        values[_answered_field(name)] = ANSWERS[name]
    return Query.objects.create(source='call', contact_phone=phone, **values)


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

    def _interactive_payload(self, phone, title):
        reply = {'type': 'button_reply', 'button_reply': {'id': title.lower(), 'title': title}}
        value = {'messages': [{'from': phone, 'type': 'interactive', 'interactive': reply}]}
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

    def test_intake_sequence_is_in_the_requested_order(self):
        self.assertEqual(WHATSAPP_QUERY_FIELDS, [
            'company_name', 'contact_email', 'gst_number', 'gst_address', 'product_description',
            'drawing', 'grade', 'technical_requirements', 'end_use', 'delivery_form', 'quantity_text',
        ])

    def test_every_question_after_company_name_has_wording(self):
        # company_name is asked by the opening template, not by this table.
        self.assertEqual(set(WHATSAPP_QUERY_QUESTIONS), set(WHATSAPP_QUERY_FIELDS) - {'company_name'})

    @patch('materials.views.whatsapp._send_whatsapp_buttons_message_background')
    @patch('materials.views.whatsapp._send_whatsapp_text_message_background')
    def test_each_answer_is_saved_and_the_next_question_asked(self, mock_send, mock_buttons):
        fields = WHATSAPP_QUERY_FIELDS
        for index, field in enumerate(fields[1:], start=1):
            if field in ('gst_number', 'gst_address'):
                continue  # the GST reply is split in two; covered by the GST tests below
            with self.subTest(field=field):
                mock_send.reset_mock()
                mock_buttons.reset_mock()
                phone = f'9198765432{index:02d}'
                _query_awaiting(field, phone=phone)
                reply = ANSWERS[field] if field in ('contact_email', 'delivery_form') else f'reply for {field}'
                self._post_payload(self._message_payload(phone, reply))

                query = Query.objects.get(contact_phone=phone)
                self.assertEqual(getattr(query, _answered_field(field)), reply)
                following = fields[index + 1] if index + 1 < len(fields) else None
                if following in WHATSAPP_QUERY_CHOICES:
                    mock_buttons.assert_called_once_with(
                        phone, WHATSAPP_QUERY_QUESTIONS[following], WHATSAPP_QUERY_CHOICES[following])
                    mock_send.assert_not_called()
                else:
                    expected = WHATSAPP_QUERY_QUESTIONS[following] if following else WHATSAPP_CLOSING_MESSAGE
                    mock_send.assert_called_once_with(phone, expected)
                    mock_buttons.assert_not_called()

    def test_the_product_type_is_detected_from_the_requirements_answer(self):
        cases = {
            'Round bright bar 12mm': 'Round Bright Bar',
            'we need ROUND  BRIGHT BARS': 'Round Bright Bar',
            'half round bright bar, 20 mm': 'Half Round Bright Bar',   # not the shorter "Round Bright Bar" inside it
            'Flat wire for springs': 'Flat Wire',
            'key steel 8x7': 'Key Steel',
            'cold rolled strip 0.5 thick': 'Cold Rolled Strip',
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(_detect_product_category(text).name, expected)

    def test_an_unclear_requirements_answer_detects_nothing(self):
        for text in ('need steel', 'square bar', 'round bright bar and flat wire', '', None):
            with self.subTest(text=text):
                self.assertIsNone(_detect_product_category(text))

    @patch('materials.views.whatsapp._send_whatsapp_text_message_background')
    def test_answering_requirements_sets_the_product_type_on_the_query(self, mock_send):
        _query_awaiting('product_description')
        self._post_payload(self._message_payload('919876543210', 'Hexagonal bright bar, 17 mm'))
        query = Query.objects.get(contact_phone='919876543210')
        self.assertEqual(query.product_description, 'Hexagonal bright bar, 17 mm')
        self.assertEqual(query.product_category.name, 'Hexagonal Bright Bar')

    @patch('materials.views.whatsapp._send_whatsapp_text_message_background')
    def test_an_ambiguous_answer_leaves_the_product_type_for_staff(self, mock_send):
        _query_awaiting('product_description')
        self._post_payload(self._message_payload('919876543210', 'round bright bar and flat wire'))
        self.assertIsNone(Query.objects.get(contact_phone='919876543210').product_category)

    @patch('materials.views.whatsapp._send_whatsapp_text_message_background')
    def test_a_product_type_staff_already_set_is_not_overwritten(self, mock_send):
        query = _query_awaiting('product_description')
        query.product_category = ProductCategory.objects.get(name='Key Steel')
        query.save(update_fields=['product_category'])
        self._post_payload(self._message_payload('919876543210', 'Round bright bar 12mm'))
        query.refresh_from_db()
        self.assertEqual(query.product_category.name, 'Key Steel')

    def test_choice_questions_fit_whatsapp_reply_button_limits(self):
        for field, choices in WHATSAPP_QUERY_CHOICES.items():
            with self.subTest(field=field):
                self.assertIn(field, WHATSAPP_QUERY_FIELDS)
                self.assertLessEqual(len(choices), 3)  # Meta allows at most 3 reply buttons
                for _value, label in choices:
                    self.assertLessEqual(len(label), 20)  # and 20 characters per title

    def test_delivery_form_offers_coil_and_bar(self):
        self.assertEqual(WHATSAPP_QUERY_CHOICES['delivery_form'], [('Coil', 'Coil'), ('Bar', 'Bar')])

    @patch('materials.views.whatsapp._whatsapp_graph_request')
    def test_buttons_message_payload_matches_the_cloud_api_format(self, mock_request):
        whatsapp._send_whatsapp_buttons_message('919876543210', 'Pick one', [('Coil', 'Coil'), ('Bar', 'Bar')])
        mock_request.assert_called_once_with({
            'messaging_product': 'whatsapp', 'to': '919876543210', 'type': 'interactive',
            'interactive': {
                'type': 'button',
                'body': {'text': 'Pick one'},
                'action': {'buttons': [
                    {'type': 'reply', 'reply': {'id': 'Coil', 'title': 'Coil'}},
                    {'type': 'reply', 'reply': {'id': 'Bar', 'title': 'Bar'}},
                ]},
            },
        })

    @patch('materials.views.whatsapp._send_whatsapp_text_message_background')
    def test_tapping_a_delivery_form_button_saves_it_and_asks_for_quantity(self, mock_send):
        for index, title in enumerate(['Coil', 'Bar']):
            with self.subTest(title=title):
                mock_send.reset_mock()
                phone = f'9198400000{index:02d}'
                _query_awaiting('delivery_form', phone=phone)
                self._post_payload(self._interactive_payload(phone, title))
                self.assertEqual(Query.objects.get(contact_phone=phone).delivery_form, title)
                mock_send.assert_called_once_with(phone, WHATSAPP_QUERY_QUESTIONS['quantity_text'])

    @patch('materials.views.whatsapp._send_whatsapp_text_message_background')
    def test_typed_delivery_form_answers_are_normalised(self, mock_send):
        cases = {'coil': 'Coil', 'Coils please': 'Coil', 'BAR': 'Bar', 'bars': 'Bar', 'in a coil form': 'Coil'}
        for index, (reply, expected) in enumerate(cases.items()):
            with self.subTest(reply=reply):
                phone = f'9198500000{index:02d}'
                _query_awaiting('delivery_form', phone=phone)
                self._post_payload(self._message_payload(phone, reply))
                self.assertEqual(Query.objects.get(contact_phone=phone).delivery_form, expected)

    @patch('materials.views.whatsapp._send_whatsapp_buttons_message_background')
    def test_unrecognised_delivery_form_reasks_with_the_buttons(self, mock_buttons):
        for index, reply in enumerate(['straight lengths', 'coil or bar', 'sheet', 'no', 'barely']):
            with self.subTest(reply=reply):
                mock_buttons.reset_mock()
                phone = f'9198600000{index:02d}'
                _query_awaiting('delivery_form', phone=phone)
                self._post_payload(self._message_payload(phone, reply))
                self.assertEqual(Query.objects.get(contact_phone=phone).delivery_form, '')
                mock_buttons.assert_called_once_with(
                    phone, WHATSAPP_QUERY_QUESTIONS['delivery_form'], WHATSAPP_QUERY_CHOICES['delivery_form'])

    @patch('materials.views.whatsapp._send_whatsapp_text_message_background')
    def test_list_reply_is_treated_like_a_button_tap(self, mock_send):
        _query_awaiting('delivery_form')
        reply = {'type': 'list_reply', 'list_reply': {'id': 'bar', 'title': 'Bar'}}
        payload = {'entry': [{'changes': [{'value': {'messages': [
            {'from': '919876543210', 'type': 'interactive', 'interactive': reply}]}}]}]}
        self._post_payload(payload)
        self.assertEqual(Query.objects.get(contact_phone='919876543210').delivery_form, 'Bar')

    def _gst_reply(self, mock_send, reply, phone='919876543210'):
        mock_send.reset_mock()
        _query_awaiting('gst_number', phone=phone)
        self._post_payload(self._message_payload(phone, reply))
        return Query.objects.get(contact_phone=phone)

    @patch('materials.views.whatsapp._send_whatsapp_text_message_background')
    def test_gst_number_and_address_in_one_message_fill_both_and_skip_the_address_question(self, mock_send):
        cases = {
            '22AAAAA0000A1Z5, 12 Industrial Area, Faridabad': '12 Industrial Area, Faridabad',
            'GSTIN: 22AAAAA0000A1Z5\n12 Industrial Area, Faridabad': '12 Industrial Area, Faridabad',
            '12 Industrial Area, Faridabad - 22aaaaa0000a1z5': '12 Industrial Area, Faridabad',
            '22AAAAA0000A1Z5, 14 GST Road, Chennai': '14 GST Road, Chennai',  # "GST" inside the address survives
        }
        for index, (reply, address) in enumerate(cases.items()):
            with self.subTest(reply=reply):
                query = self._gst_reply(mock_send, reply, phone=f'9198300000{index:02d}')
                self.assertEqual(query.gst_number, '22AAAAA0000A1Z5')
                self.assertEqual(query.gst_address, address)
                mock_send.assert_called_once_with(query.contact_phone, WHATSAPP_QUERY_QUESTIONS['product_description'])

    @patch('materials.views.whatsapp._send_whatsapp_text_message_background')
    def test_gst_number_alone_is_normalised_and_the_address_is_asked_next(self, mock_send):
        for index, reply in enumerate(['22aaaaa0000a1z5', ' 22 AAAAA 0000 A 1Z5 ']):
            with self.subTest(reply=reply):
                query = self._gst_reply(mock_send, reply, phone=f'9198000000{index:02d}')
                self.assertEqual(query.gst_number, '22AAAAA0000A1Z5')
                self.assertEqual(query.gst_address, '')
                mock_send.assert_called_once_with(query.contact_phone, WHATSAPP_QUERY_QUESTIONS['gst_address'])

    @patch('materials.views.whatsapp._send_whatsapp_text_message_background')
    def test_address_question_after_a_number_only_reply_saves_the_address(self, mock_send):
        _query_awaiting('gst_address')
        self._post_payload(self._message_payload('919876543210', '12 Industrial Area, Faridabad'))

        query = Query.objects.get(contact_phone='919876543210')
        self.assertEqual(query.gst_address, '12 Industrial Area, Faridabad')
        mock_send.assert_called_once_with('919876543210', WHATSAPP_QUERY_QUESTIONS['product_description'])

    @patch('materials.views.whatsapp._send_whatsapp_text_message_background')
    def test_na_is_not_accepted_because_every_company_has_a_gst_number(self, mock_send):
        replies = ['NA', 'n/a', 'No', 'none', 'Not registered', 'NA, 45 Mall Road']
        for index, reply in enumerate(replies):
            with self.subTest(reply=reply):
                query = self._gst_reply(mock_send, reply, phone=f'9198100000{index:02d}')
                self.assertEqual(query.gst_number, '')
                self.assertEqual(query.gst_address, '')
                mock_send.assert_called_once_with(query.contact_phone, WHATSAPP_GST_INVALID_MESSAGE)

    def test_gst_question_does_not_say_one_message_or_offer_na(self):
        for text in (WHATSAPP_QUERY_QUESTIONS['gst_number'], WHATSAPP_QUERY_QUESTIONS['gst_address'], WHATSAPP_GST_INVALID_MESSAGE):
            with self.subTest(text=text):
                self.assertNotIn('one message', text)
                self.assertNotRegex(text, r'\bNA\b')

    @patch('materials.views.whatsapp._send_whatsapp_text_message_background')
    def test_invalid_gst_reply_reasks_without_saving(self, mock_send):
        replies = ['GST pending', '22AAAAA0000A1Z', '1234567890ABCDE', '12 Industrial Area, Faridabad', 'no. 5 Mall Road']
        for index, reply in enumerate(replies):
            with self.subTest(reply=reply):
                query = self._gst_reply(mock_send, reply, phone=f'9198200000{index:02d}')
                self.assertEqual(query.gst_number, '')
                self.assertEqual(query.gst_address, '')
                mock_send.assert_called_once_with(query.contact_phone, WHATSAPP_GST_INVALID_MESSAGE)

    @patch('materials.views.whatsapp._send_whatsapp_text_message_background')
    def test_no_is_a_real_answer_that_advances_the_sequence(self, mock_send):
        _query_awaiting('technical_requirements')
        self._post_payload(self._message_payload('919876543210', 'no'))

        query = Query.objects.get(contact_phone='919876543210')
        self.assertEqual(query.technical_requirements, 'no')
        mock_send.assert_called_once_with('919876543210', WHATSAPP_QUERY_QUESTIONS['end_use'])

    @patch('materials.views.whatsapp._send_whatsapp_text_message_background')
    def test_text_reply_to_drawing_question_saves_as_drawing_notes_and_asks_for_grade_next(self, mock_send):
        _query_awaiting('drawing')
        self._post_payload(self._message_payload('919876543210', 'no'))

        query = Query.objects.get(contact_phone='919876543210')
        self.assertEqual(query.drawing_notes, 'no')
        self.assertFalse(query.drawing)
        mock_send.assert_called_once_with('919876543210', WHATSAPP_QUERY_QUESTIONS['grade'])

    @patch('materials.views.whatsapp._send_whatsapp_text_message_background')
    def test_image_reply_to_drawing_question_is_routed_for_download(self, mock_send):
        """The actual download happens in a background thread — this just
        confirms the webhook recognizes an image reply as answering the
        drawing question and hands it off, without touching drawing_notes
        (a text reply would) or advancing past 'drawing' synchronously."""
        _query_awaiting('drawing')
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
        _query_awaiting('contact_email')
        with patch('materials.views.whatsapp._process_whatsapp_drawing_media_background') as mock_bg:
            self._post_payload(self._media_payload('919876543210', 'image', 'media-id-456'))
            mock_bg.assert_not_called()

    @patch('materials.views.whatsapp._send_whatsapp_text_message_background')
    def test_inbound_invalid_email_reasks_without_saving(self, mock_send):
        _query_awaiting('contact_email')
        self._post_payload(self._message_payload('919876543210', 'not an email'))

        query = Query.objects.get(contact_phone='919876543210')
        self.assertEqual(query.contact_email, '')
        mock_send.assert_called_once_with('919876543210', WHATSAPP_QUERY_QUESTIONS['contact_email'])

    @patch('materials.views.whatsapp._send_whatsapp_text_message_background')
    def test_completing_the_sequence_does_not_auto_match_a_product_type(self, mock_send):
        """Size isn't asked any more, so the bot can't match a catalogue
        product from grade+size; choosing standard vs customised is staff's
        call. Even a query that already has both (entered by staff) isn't
        matched behind their back."""
        ProductType.objects.create(item_code='Catalogue Bar', grade='EN8D', size='1.200')
        query = _query_awaiting('quantity_text')
        query.size = Decimal('1.2')
        query.save(update_fields=['size'])
        self._post_payload(self._message_payload('919876543210', '2 tons monthly'))

        query.refresh_from_db()
        self.assertIsNone(query.product_type)

    @patch('materials.views.whatsapp._send_whatsapp_text_message_background')
    def test_message_after_sequence_complete_is_appended_to_notes(self, mock_send):
        query = _query_awaiting(None)
        query.notes = 'standard requirement'
        query.save(update_fields=['notes'])
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
            'source': 'indiamart', 'contact_phone': '+91 98765 43210',
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
        query = _query_awaiting('drawing')

        self._run_and_wait(query.pk, 'media-id-1', 'application/pdf')

        query.refresh_from_db()
        self.assertTrue(query.drawing)
        self.assertTrue(query.drawing.name.endswith('.pdf'))
        self.assertEqual(query.drawing.read()[:4], b'%PDF')
        self.assertEqual(query.drawing_notes, 'Drawing attached via WhatsApp')
        mock_send.assert_called_once_with('919876543210', WHATSAPP_QUERY_QUESTIONS['grade'])
        query.drawing.delete(save=False)

    @patch('materials.views.whatsapp._send_whatsapp_text_message')
    @patch('materials.views.whatsapp._download_whatsapp_media')
    def test_download_failure_leaves_query_untouched(self, mock_download, mock_send):
        mock_download.side_effect = WhatsAppSendError("network boom")
        query = _query_awaiting('drawing')

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
        query = _query_awaiting('grade')  # drawing already answered by text

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
