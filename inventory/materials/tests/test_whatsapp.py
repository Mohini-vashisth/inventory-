"""The WhatsApp intake bot: webhook, question sequence, drawing media download."""

import hashlib
import hmac
import json
import tempfile
import threading
import time
from datetime import timedelta

from decimal import Decimal
from django.contrib.auth.models import User
from django.core.files.base import ContentFile
from django.utils import timezone
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from unittest.mock import patch, MagicMock

from .helpers import create_query
from ..models import ProductCategory, ProductType, Query, QueryItem, WhatsAppMessage
from ..views import whatsapp
from ..views.whatsapp import (
    WhatsAppSendError, WHATSAPP_CLOSING_MESSAGE, WHATSAPP_GST_INVALID_MESSAGE, _detect_product_category,
    WHATSAPP_QUERY_CHOICES, WHATSAPP_QUERY_FIELDS, WHATSAPP_QUERY_QUESTIONS, WHATSAPP_DIMENSION_INVALID_MESSAGES,
    _parse_whatsapp_quantity_kg, _parse_whatsapp_dimension, WHATSAPP_QUANTITY_INVALID_MESSAGE,
    WHATSAPP_SUMMARY_CHOICES, WHATSAPP_SUMMARY_QUESTION,
)


def _sign_whatsapp_payload(body_bytes, secret):
    digest = hmac.new(secret.encode('utf-8'), body_bytes, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


# A plausible reply to every question, in the order the bot asks them.
ANSWERS = {
    'company_name': 'Ramesh Traders', 'contact_email': 'ramesh@example.com',
    'gst_number': '22AAAAA0000A1Z5', 'gst_address': '12 Industrial Area, Faridabad',
    'item_count': '1', 'product_category': 'Flat Bright Bar', 'drawing': 'no', 'grade': 'EN8D', 'width': '50', 'thickness': '6',
    'technical_requirements': 'no', 'end_use': 'automotive shafts', 'delivery_form': 'Coil',
    'quantity': '8000',
}


def _answered_field(field):
    return 'drawing_notes' if field == 'drawing' else field


ITEM_FIELDS = ('product_category', 'grade', 'width', 'thickness', 'delivery_form', 'quantity')


def _answered_values(field):
    """The query-level model values for every question before `field` (all of them for None)."""
    values = {}
    for name in WHATSAPP_QUERY_FIELDS:
        if name == field:
            break
        if name != 'item_count' and name not in ITEM_FIELDS:
            values[_answered_field(name)] = ANSWERS[name]
    return values


def _item_values(field):
    """The per-product values for every per-product question before `field`."""
    values = {}
    for name in WHATSAPP_QUERY_FIELDS:
        if name == field:
            break
        if name in ITEM_FIELDS:
            values[name] = ProductCategory.objects.get_or_create(name=ANSWERS[name])[0] if name == 'product_category' else ANSWERS[name]
    return values


def _item_of(phone):
    """The first product of the query with this phone number."""
    return QueryItem.objects.filter(query__contact_phone=phone).order_by('position', 'pk').first()


def _query_awaiting(field, phone='919876543210', items=1, **extra):
    """A Query that has answered every question before `field`, so `field` is the one the bot is waiting
    on (with `items` products if the number of products was already asked). Pass None for a fully
    answered one."""
    query = Query.objects.create(source='call', contact_phone=phone, **{**_answered_values(field), **extra})
    asked_count = field is None or WHATSAPP_QUERY_FIELDS.index(field) > WHATSAPP_QUERY_FIELDS.index('item_count')
    if asked_count:
        for number in range(items):
            QueryItem.objects.create(query=query, position=number + 1, **_item_values(field))
    return query


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
            'company_name', 'contact_email', 'gst_number', 'gst_address', 'item_count', 'product_category',
            'drawing', 'grade', 'width', 'thickness', 'technical_requirements', 'end_use', 'delivery_form',
            'quantity',
        ])

    def test_every_question_after_company_name_has_wording(self):
        # company_name is asked by the opening template, not by this table.
        self.assertEqual(set(WHATSAPP_QUERY_QUESTIONS), set(WHATSAPP_QUERY_FIELDS) - {'company_name'})

    @patch('materials.views.whatsapp._send_whatsapp_buttons_message_background')
    @patch('materials.views.whatsapp._send_whatsapp_text_message_background')
    def test_each_answer_is_saved_and_the_next_question_asked(self, mock_send, mock_buttons):
        fields = WHATSAPP_QUERY_FIELDS
        for index, field in enumerate(fields[1:], start=1):
            if field in ('gst_number', 'gst_address', 'item_count', 'width', 'thickness', 'product_category', 'quantity'):
                continue  # GST is split in two, the count/width/thickness/quantity are numbers and the type is a list; covered by their own tests
            with self.subTest(field=field):
                mock_send.reset_mock()
                mock_buttons.reset_mock()
                phone = f'9198765432{index:02d}'
                _query_awaiting(field, phone=phone)
                reply = ANSWERS[field] if field in ('contact_email', 'delivery_form') else f'reply for {field}'
                self._post_payload(self._message_payload(phone, reply))

                query = Query.objects.get(contact_phone=phone)
                expected_saved = ''.join(ch for ch in reply if ch.isalnum()).upper() if field == 'grade' else reply
                holder = _item_of(phone) if field in ITEM_FIELDS else query
                self.assertEqual(getattr(holder, _answered_field(field)), expected_saved)
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
            'Flat bright bar 50x6': 'Flat Bright Bar',
            'we need SQUARE  BRIGHT BARS': 'Square Bright Bar',
            'shaped bright bar for gears': 'Profile/Shaped Bright Bar',      # either half of "Profile/Shaped"
            'Profile bright bar': 'Profile/Shaped Bright Bar',
            'chamfer steel 10x10': 'Chamfer Steel',
            'cold rolled strip 0.5 thick': 'Cold Rolled Strip',
            'cold rolled profile': 'Cold Rolled Profile',
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(_detect_product_category(text).name, expected)

    def test_an_unclear_requirements_answer_detects_nothing(self):
        for text in ('need steel', 'square bar', 'square bright bar and flat bright bar', '', None):
            with self.subTest(text=text):
                self.assertIsNone(_detect_product_category(text))

    def test_the_bot_no_longer_asks_a_free_text_requirements_question(self):
        self.assertNotIn('product_description', WHATSAPP_QUERY_FIELDS)
        self.assertNotIn('product_description', WHATSAPP_QUERY_QUESTIONS)
        self.assertNotIn('product_description', [f for f, _ in Query.INTAKE_TEXT_FIELDS])
        self.assertEqual(WHATSAPP_QUERY_FIELDS[WHATSAPP_QUERY_FIELDS.index('product_category') + 1], 'drawing')

    @patch('materials.views.whatsapp._send_whatsapp_text_message_background')
    def test_the_catalogue_code_is_linked_once_type_and_grade_are_known(self, mock_send):
        ProductType.objects.create(item_code='FBB009', category=ProductCategory.objects.get(name='Flat Bright Bar'), grade='EN8D')
        _query_awaiting('grade')   # type (Flat Bright Bar) already chosen
        self._post_payload(self._message_payload('919876543210', 'en-8d'))
        item = _item_of('919876543210')
        self.assertEqual(item.grade, 'EN8D')
        self.assertEqual(item.product_type.item_code, 'FBB009')

    @patch('materials.views.whatsapp._send_whatsapp_text_message_background')
    def test_no_code_is_linked_when_the_catalogue_has_none(self, mock_send):
        _query_awaiting('grade')
        self._post_payload(self._message_payload('919876543210', 'SS304'))
        self.assertIsNone(_item_of('919876543210').product_type)

    def test_a_product_type_staff_already_set_skips_the_question(self):
        query = create_query(source='call', contact_phone='919876543299', **_answered_values('product_category'))
        QueryItem.objects.create(query=query, position=1, product_category=ProductCategory.objects.get(name='Chamfer Steel'))
        self.assertEqual(whatsapp._next_expected_query_field(query), 'drawing')

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
                self.assertEqual(_item_of(phone).delivery_form, title)
                mock_send.assert_called_once_with(phone, WHATSAPP_QUERY_QUESTIONS['quantity'])

    @patch('materials.views.whatsapp._send_whatsapp_text_message_background')
    def test_typed_delivery_form_answers_are_normalised(self, mock_send):
        cases = {'coil': 'Coil', 'Coils please': 'Coil', 'BAR': 'Bar', 'bars': 'Bar', 'in a coil form': 'Coil'}
        for index, (reply, expected) in enumerate(cases.items()):
            with self.subTest(reply=reply):
                phone = f'9198500000{index:02d}'
                _query_awaiting('delivery_form', phone=phone)
                self._post_payload(self._message_payload(phone, reply))
                self.assertEqual(_item_of(phone).delivery_form, expected)

    @patch('materials.views.whatsapp._send_whatsapp_buttons_message_background')
    def test_unrecognised_delivery_form_reasks_with_the_buttons(self, mock_buttons):
        for index, reply in enumerate(['straight lengths', 'coil or bar', 'sheet', 'no', 'barely']):
            with self.subTest(reply=reply):
                mock_buttons.reset_mock()
                phone = f'9198600000{index:02d}'
                _query_awaiting('delivery_form', phone=phone)
                self._post_payload(self._message_payload(phone, reply))
                self.assertEqual(_item_of(phone).delivery_form, '')
                mock_buttons.assert_called_once_with(
                    phone, WHATSAPP_QUERY_QUESTIONS['delivery_form'], WHATSAPP_QUERY_CHOICES['delivery_form'])

    @patch('materials.views.whatsapp._send_whatsapp_text_message_background')
    def test_list_reply_is_treated_like_a_button_tap(self, mock_send):
        _query_awaiting('delivery_form')
        reply = {'type': 'list_reply', 'list_reply': {'id': 'bar', 'title': 'Bar'}}
        payload = {'entry': [{'changes': [{'value': {'messages': [
            {'from': '919876543210', 'type': 'interactive', 'interactive': reply}]}}]}]}
        self._post_payload(payload)
        self.assertEqual(_item_of('919876543210').delivery_form, 'Bar')

    @patch('materials.views.whatsapp._send_whatsapp_list_message_background')
    @patch('materials.views.whatsapp._send_whatsapp_text_message_background')
    def test_the_product_type_question_is_a_tappable_list_of_the_types_for_one_product(self, mock_send, mock_list):
        _query_awaiting('item_count')
        self._post_payload(self._message_payload('919876543210', '1'))
        mock_send.assert_not_called()
        phone, text, button, rows = mock_list.call_args[0]
        self.assertEqual((phone, text, button), ('919876543210', WHATSAPP_QUERY_QUESTIONS['product_category'], 'Choose type'))
        self.assertEqual([r[2] for r in rows], list(ProductCategory.objects.values_list('name', flat=True)))
        for row_id, title, description in rows:
            self.assertTrue(row_id.startswith('category:'))
            self.assertLessEqual(len(title), 24)   # Meta's limit on a row title
        self.assertLessEqual(len(rows), 10)        # and on the number of rows
        self.assertLessEqual(len('Choose type'), 20)

    def test_a_long_type_name_is_cut_to_fit_a_row_title_but_kept_in_the_description(self):
        rows = {r[2]: r[1] for r in whatsapp._product_category_rows()}
        self.assertEqual(rows['Flat Bright Bar'], 'Flat Bright Bar')
        self.assertEqual(rows['Profile/Shaped Bright Bar'], 'Profile/Shaped Bright B…')

    @patch('materials.views.whatsapp._whatsapp_graph_request')
    def test_list_message_payload_matches_the_cloud_api_format(self, mock_request):
        whatsapp._send_whatsapp_list_message('919876543210', 'Pick one', 'Choose', [('category:1', 'Flat', 'Flat Bright Bar')])
        mock_request.assert_called_once_with({
            'messaging_product': 'whatsapp', 'to': '919876543210', 'type': 'interactive',
            'interactive': {
                'type': 'list',
                'body': {'text': 'Pick one'},
                'action': {'button': 'Choose', 'sections': [{
                    'title': 'Product types',
                    'rows': [{'id': 'category:1', 'title': 'Flat', 'description': 'Flat Bright Bar'}],
                }]},
            },
        })

    @patch('materials.views.whatsapp._send_whatsapp_text_message_background')
    def test_tapping_a_type_row_saves_the_full_type_even_when_its_title_was_cut(self, mock_send):
        _query_awaiting('product_category')
        category = ProductCategory.objects.get(name='Profile/Shaped Bright Bar')
        reply = {'type': 'list_reply', 'list_reply': {'id': f'category:{category.pk}', 'title': 'Profile/Shaped Bright B…'}}
        self._post_payload({'entry': [{'changes': [{'value': {'messages': [
            {'from': '919876543210', 'type': 'interactive', 'interactive': reply}]}}]}]})
        self.assertEqual(_item_of('919876543210').product_category, category)
        mock_send.assert_called_once_with('919876543210', WHATSAPP_QUERY_QUESTIONS['drawing'])

    @patch('materials.views.whatsapp._send_whatsapp_text_message_background')
    def test_typing_a_type_name_instead_of_tapping_works(self, mock_send):
        _query_awaiting('product_category')
        self._post_payload(self._message_payload('919876543210', 'cold rolled strip please'))
        self.assertEqual(_item_of('919876543210').product_category.name, 'Cold Rolled Strip')

    @patch('materials.views.whatsapp._send_whatsapp_list_message_background')
    def test_an_unrecognised_or_ambiguous_type_re_sends_the_list_without_advancing(self, mock_list):
        for index, reply in enumerate(['some steel', 'square bright bar and flat bright bar']):
            with self.subTest(reply=reply):
                mock_list.reset_mock()
                phone = f'9198400000{index:02d}'
                _query_awaiting('product_category', phone=phone)
                self._post_payload(self._message_payload(phone, reply))
                self.assertIsNone(_item_of(phone).product_category)
                self.assertEqual(mock_list.call_count, 1)

    @patch('materials.views.whatsapp._send_whatsapp_text_message_background')
    def test_with_more_types_than_a_list_can_hold_the_customer_is_asked_to_type_it(self, mock_send):
        for index in range(4):
            ProductCategory.objects.create(name=f'Extra Type {index}')
        self.assertIsNone(whatsapp._product_category_rows())
        whatsapp._send_whatsapp_question('919876543210', 'product_category')
        self.assertIn('type its name', mock_send.call_args[0][1])

    def _gst_reply(self, mock_send, reply, phone='919876543210'):
        mock_send.reset_mock()
        _query_awaiting('gst_number', phone=phone)
        self._post_payload(self._message_payload(phone, reply))
        return Query.objects.get(contact_phone=phone)

    @patch('materials.views.whatsapp._send_whatsapp_list_message_background')
    @patch('materials.views.whatsapp._send_whatsapp_text_message_background')
    def test_gst_number_and_address_in_one_message_fill_both_and_skip_the_address_question(self, mock_send, mock_list):
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
                self.assertEqual(mock_list.call_args[0][:2], (query.contact_phone, WHATSAPP_QUERY_QUESTIONS['item_count']))
                mock_list.reset_mock()

    @patch('materials.views.whatsapp._send_whatsapp_text_message_background')
    def test_gst_number_alone_is_normalised_and_the_address_is_asked_next(self, mock_send):
        for index, reply in enumerate(['22aaaaa0000a1z5', ' 22 AAAAA 0000 A 1Z5 ']):
            with self.subTest(reply=reply):
                query = self._gst_reply(mock_send, reply, phone=f'9198000000{index:02d}')
                self.assertEqual(query.gst_number, '22AAAAA0000A1Z5')
                self.assertEqual(query.gst_address, '')
                mock_send.assert_called_once_with(query.contact_phone, WHATSAPP_QUERY_QUESTIONS['gst_address'])

    @patch('materials.views.whatsapp._send_whatsapp_text_message_background')
    @patch('materials.views.whatsapp._send_whatsapp_list_message_background')
    def test_address_question_after_a_number_only_reply_saves_the_address(self, mock_list, mock_send):
        _query_awaiting('gst_address')
        self._post_payload(self._message_payload('919876543210', '12 Industrial Area, Faridabad'))

        query = Query.objects.get(contact_phone='919876543210')
        self.assertEqual(query.gst_address, '12 Industrial Area, Faridabad')
        self.assertEqual(mock_list.call_args[0][:2], ('919876543210', WHATSAPP_QUERY_QUESTIONS['item_count']))

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
    def test_the_grade_answer_is_followed_by_the_width_question_then_thickness(self, mock_send):
        _query_awaiting('grade')
        self._post_payload(self._message_payload('919876543210', 'EN8D'))
        mock_send.assert_called_once_with('919876543210', WHATSAPP_QUERY_QUESTIONS['width'])
        mock_send.reset_mock()
        self._post_payload(self._message_payload('919876543210', '50'))
        mock_send.assert_called_once_with('919876543210', WHATSAPP_QUERY_QUESTIONS['thickness'])

    @patch('materials.views.whatsapp._send_whatsapp_text_message_background')
    def test_width_and_thickness_replies_are_saved_in_mm_and_the_next_question_follows(self, mock_send):
        _query_awaiting('width')
        self._post_payload(self._message_payload('919876543210', '50 mm'))
        self._post_payload(self._message_payload('919876543210', '1,2 mm'))
        item = _item_of('919876543210')
        self.assertEqual((item.width, item.thickness), (Decimal('50'), Decimal('1.2')))
        self.assertEqual(Query.objects.get(contact_phone='919876543210').notes, '')
        mock_send.assert_called_with('919876543210', WHATSAPP_QUERY_QUESTIONS['technical_requirements'])

    @patch('materials.views.whatsapp._send_whatsapp_text_message_background')
    def test_a_reply_with_no_usable_number_re_asks_without_advancing(self, mock_send):
        for field in ('width', 'thickness'):
            for index, reply in enumerate(['as per drawing', 'no', '0', '1.2345', 'big']):
                with self.subTest(field=field, reply=reply):
                    mock_send.reset_mock()
                    phone = f'9198300{field[0]}{index:02d}'.replace('w', '1').replace('t', '2')
                    _query_awaiting(field, phone=phone)
                    self._post_payload(self._message_payload(phone, reply))
                    self.assertIsNone(getattr(_item_of(phone), field))
                    mock_send.assert_called_once_with(phone, WHATSAPP_DIMENSION_INVALID_MESSAGES[field])

    @patch('materials.views.whatsapp._send_whatsapp_text_message_background')
    def test_several_numbers_in_one_reply_keep_the_first_and_show_staff_the_whole_reply(self, mock_send):
        _query_awaiting('thickness')
        self._post_payload(self._message_payload('919876543210', '10 and 12 mm'))
        self.assertEqual(_item_of('919876543210').thickness, Decimal('10'))
        self.assertIn('Thickness reply: 10 and 12 mm', Query.objects.get(contact_phone='919876543210').notes)

    def test_dimension_parsing(self):
        cases = {'12': '12', '1.2': '1.2', '12 mm': '12', '12,5 mm round': '12.5', 'Dia 25.4mm': '25.4',
                 '6.500': '6.5', '0': None, '': None, 'abc': None, '1.2345': None, '99999999': None}
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(_parse_whatsapp_dimension(text), Decimal(expected) if expected else None)

    def test_width_and_thickness_questions_ask_for_mm(self):
        self.assertIn('width', WHATSAPP_QUERY_QUESTIONS['width'])
        self.assertIn('thickness', WHATSAPP_QUERY_QUESTIONS['thickness'])
        self.assertIn('mm', WHATSAPP_QUERY_QUESTIONS['width'])
        self.assertIn('mm', WHATSAPP_QUERY_QUESTIONS['thickness'])

    def test_quantity_parsing_only_trusts_kilograms(self):
        cases = {
            '8000 kgs monthly': '8000', '8000kg': '8000', '8,000 KG one time': '8000', '1500.5 kgs': '1500.5',
            'monthly 8000': '8000', 'one time, 5000 kgs': '5000',
            '8 ton monthly': None, '2 tons': None, '10 quintal': None,
            '3 months 8000': None, '5000 kgs and 3000 kgs': None,
            'monthly': None, '': None, '0 kgs': None,
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(_parse_whatsapp_quantity_kg(text), Decimal(expected) if expected else None)

    @patch('materials.views.whatsapp._send_whatsapp_steps_background')
    def test_a_kg_answer_is_saved_as_the_quantity(self, mock_steps):
        _query_awaiting('quantity')
        self._post_payload(self._message_payload('919876543210', '8000 kgs'))
        self.assertEqual(_item_of('919876543210').quantity, Decimal('8000'))

    @patch('materials.views.whatsapp._send_whatsapp_text_message_background')
    def test_a_quantity_not_in_kgs_is_asked_for_again_not_guessed(self, mock_send):
        for index, reply in enumerate(['8 ton monthly', '2 tons', 'a lot', '0', 'monthly']):
            with self.subTest(reply=reply):
                mock_send.reset_mock()
                phone = f'9198500000{index:02d}'
                _query_awaiting('quantity', phone=phone)
                self._post_payload(self._message_payload(phone, reply))
                self.assertIsNone(_item_of(phone).quantity)
                mock_send.assert_called_once_with(phone, WHATSAPP_QUANTITY_INVALID_MESSAGE)

    def test_the_quantity_question_is_short_and_asks_for_kgs(self):
        self.assertEqual(WHATSAPP_QUERY_QUESTIONS['quantity'], "Please enter the quantity in kgs.")

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
        """The bot never picks a catalogue product code: the product code is
        matched later, on the quote or the query's Edit page, where staff can
        see it. A catalogue code for the same grade is not linked behind
        their back."""
        ProductType.objects.create(item_code='Catalogue Bar', grade='EN8D')
        query = _query_awaiting('quantity')
        with patch('materials.views.whatsapp._send_whatsapp_steps_background'):
            self._post_payload(self._message_payload('919876543210', '2000'))

        self.assertIsNone(query.items.get().product_type)

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

    @override_settings(WHATSAPP_ACCESS_TOKEN='test-token', WHATSAPP_PHONE_NUMBER_ID='123')
    @patch('materials.views.whatsapp._send_whatsapp_buttons_message')
    @patch('materials.views.whatsapp._send_whatsapp_text_message')
    @patch('materials.views.whatsapp._download_whatsapp_media')
    def test_a_replacement_drawing_is_held_until_the_customer_confirms_it(self, mock_download, mock_text, mock_buttons):
        mock_download.return_value = (b'%PDF-1.4 the new drawing', 'application/pdf')
        query = Query.objects.create(source='call', contact_phone='919876543210', bot_stage='editing', edit_field='drawing',
                                     drawing_notes='no', **{k: v for k, v in _answered_values(None).items() if k != 'drawing_notes'})

        self._run_and_wait(query.pk, 'media-id-new', 'application/pdf')

        query.refresh_from_db()
        self.assertEqual(query.bot_stage, 'confirm_change')
        self.assertTrue(query.pending_drawing)
        self.assertFalse(query.drawing)   # not stored yet
        self.assertEqual(query.drawing_notes, 'no')

        whatsapp._review_reply(query, 'Yes, save it', True)   # the customer confirms
        for thread in threading.enumerate():
            if thread is not threading.current_thread() and thread.daemon:
                thread.join(timeout=2)

        query.refresh_from_db()
        self.assertTrue(query.drawing)
        self.assertEqual(query.drawing.read()[:4], b'%PDF')
        self.assertFalse(query.pending_drawing)
        self.assertEqual((query.bot_stage, query.drawing_notes), ('summary', 'Drawing attached via WhatsApp'))
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


@override_settings(WHATSAPP_VERIFY_TOKEN='test-verify-token', WHATSAPP_APP_SECRET='test-app-secret')
class WhatsAppResilienceTests(TestCase):
    """The app can be offline (the PC off, a restart) while customers keep replying; Meta then
    replays the backlog — late, repeated and out of order. None of that may corrupt a query."""

    PHONE = '919876543210'

    def _deliver(self, *messages):
        """POST one webhook delivery holding these (text, id, timestamp) messages."""
        value = {'messages': [
            {'from': self.PHONE, 'id': mid, 'timestamp': str(ts), 'type': 'text', 'text': {'body': text}}
            for text, mid, ts in messages]}
        body = json.dumps({'entry': [{'changes': [{'value': value}]}]}).encode('utf-8')
        return self.client.post(reverse('whatsapp_webhook'), data=body, content_type='application/json',
                                HTTP_X_HUB_SIGNATURE_256=_sign_whatsapp_payload(body, 'test-app-secret'))

    def _now(self):
        return int(time.time())

    @patch('materials.views.whatsapp._send_whatsapp_text_message_background')
    def test_a_message_delivered_twice_is_handled_once(self, mock_send):
        _query_awaiting('contact_email')
        for _ in range(3):
            self._deliver(('ramesh@example.com', 'wamid.A', self._now()))
        self.assertEqual(Query.objects.get(contact_phone=self.PHONE).contact_email, 'ramesh@example.com')
        mock_send.assert_called_once_with(self.PHONE, WHATSAPP_QUERY_QUESTIONS['gst_number'])
        self.assertEqual(WhatsAppMessage.objects.filter(message_id='wamid.A').count(), 1)

    @patch('materials.views.whatsapp._send_whatsapp_text_message_background')
    def test_a_message_without_an_id_is_still_handled(self, mock_send):
        _query_awaiting('contact_email')
        self._deliver(('ramesh@example.com', '', self._now()))
        self.assertEqual(Query.objects.get(contact_phone=self.PHONE).contact_email, 'ramesh@example.com')

    @patch('materials.views.whatsapp._send_whatsapp_text_message_background')
    def test_a_failed_attempt_does_not_use_up_the_message_so_meta_s_retry_works(self, mock_send):
        _query_awaiting('contact_email')
        with patch('materials.views.whatsapp._handle_whatsapp_message', side_effect=RuntimeError('boom')):
            with self.assertRaises(RuntimeError):
                self._deliver(('ramesh@example.com', 'wamid.B', self._now()))
        self.assertFalse(WhatsAppMessage.objects.filter(message_id='wamid.B').exists())
        self._deliver(('ramesh@example.com', 'wamid.B', self._now()))   # the retry
        self.assertEqual(Query.objects.get(contact_phone=self.PHONE).contact_email, 'ramesh@example.com')

    @patch('materials.views.whatsapp._send_whatsapp_text_message_background')
    def test_a_batch_is_handled_oldest_first_and_asks_only_one_question(self, mock_send):
        _query_awaiting('width')
        now = self._now()
        # listed newest first, as a replayed backlog might be
        self._deliver(('6', 'wamid.T', now - 10), ('50', 'wamid.W', now - 20))
        item = _item_of(self.PHONE)
        self.assertEqual((item.width, item.thickness), (Decimal('50'), Decimal('6')))   # not swapped
        mock_send.assert_called_once_with(self.PHONE, WHATSAPP_QUERY_QUESTIONS['technical_requirements'])

    @patch('materials.views.whatsapp._send_whatsapp_text_message_background')
    def test_a_reply_older_than_one_already_handled_is_not_used_as_an_answer(self, mock_send):
        _query_awaiting('width')
        now = self._now()
        self._deliver(('50', 'wamid.NEW', now - 10))
        mock_send.reset_mock()
        self._deliver(('40', 'wamid.OLD', now - 60))   # sent earlier, but only delivered now
        query = Query.objects.get(contact_phone=self.PHONE)
        self.assertEqual(_item_of(self.PHONE).width, Decimal('50'))
        self.assertIsNone(_item_of(self.PHONE).thickness)
        self.assertIn('[late reply', query.notes)
        self.assertIn('40', query.notes)
        self.assertTrue(query.needs_review)
        self.assertIn('out of order', query.review_note)
        mock_send.assert_not_called()

    @patch('materials.views.whatsapp._send_whatsapp_text_message_background')
    def test_when_the_24_hour_window_has_closed_the_answer_is_kept_but_nothing_is_sent(self, mock_send):
        _query_awaiting('width')
        two_days_ago = self._now() - 2 * 86400
        self._deliver(('50', 'wamid.STALE', two_days_ago))
        query = Query.objects.get(contact_phone=self.PHONE)
        self.assertEqual(_item_of(self.PHONE).width, Decimal('50'))   # the data is fine, keep it
        mock_send.assert_not_called()                  # Meta would refuse a free-form message now
        self.assertTrue(query.needs_review)
        self.assertIn('24-hour', query.review_note)

    @patch('materials.views.whatsapp._send_whatsapp_text_message_background')
    def test_the_bot_carries_on_once_the_customer_writes_again(self, mock_send):
        _query_awaiting('width')
        self._deliver(('50', 'wamid.STALE', self._now() - 2 * 86400))
        self._deliver(('6', 'wamid.FRESH', self._now()))   # a new message re-opens the window
        self.assertEqual(_item_of(self.PHONE).thickness, Decimal('6'))
        mock_send.assert_called_once_with(self.PHONE, WHATSAPP_QUERY_QUESTIONS['technical_requirements'])

    @patch('materials.views.whatsapp._send_whatsapp_text_message_background')
    def test_a_question_already_asked_is_not_asked_again(self, mock_send):
        _query_awaiting('width')
        self._deliver(('50', 'wamid.W', self._now()))
        mock_send.assert_called_once_with(self.PHONE, WHATSAPP_QUERY_QUESTIONS['thickness'])
        whatsapp._advance_whatsapp_query(Query.objects.get(contact_phone=self.PHONE))
        whatsapp._advance_whatsapp_query(Query.objects.get(contact_phone=self.PHONE))
        self.assertEqual(mock_send.call_count, 1)

    @patch('materials.views.whatsapp._send_whatsapp_steps_background')
    def test_the_summary_is_sent_once(self, mock_steps):
        _query_awaiting('quantity')
        self._deliver(('8000', 'wamid.Q', self._now()))
        self.assertEqual(mock_steps.call_count, 1)
        whatsapp._advance_whatsapp_query(Query.objects.get(contact_phone=self.PHONE))
        self.assertEqual(mock_steps.call_count, 1)

    @patch('materials.views.whatsapp._send_whatsapp_text_message_background')
    def test_an_invalid_reply_is_still_told_so_in_a_normal_conversation(self, mock_send):
        _query_awaiting('width')
        self._deliver(('as per drawing', 'wamid.X', self._now()))
        mock_send.assert_called_once_with(self.PHONE, WHATSAPP_DIMENSION_INVALID_MESSAGES['width'])

    def test_old_message_ids_are_not_kept_forever(self):
        from datetime import timedelta
        from django.utils import timezone
        old = WhatsAppMessage.objects.create(message_id='wamid.OLD')
        WhatsAppMessage.objects.filter(pk=old.pk).update(received_at=timezone.now() - timedelta(days=60))
        _query_awaiting('contact_email')
        with patch('materials.views.whatsapp._send_whatsapp_text_message_background'):
            self._deliver(('ramesh@example.com', 'wamid.NEW', self._now()))
        self.assertFalse(WhatsAppMessage.objects.filter(message_id='wamid.OLD').exists())
        self.assertTrue(WhatsAppMessage.objects.filter(message_id='wamid.NEW').exists())


class QueryReviewFlagTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user('review_staff', password='pw', is_staff=True)
        self.client.force_login(self.staff)
        self.query = Query.objects.create(source='whatsapp', contact_phone='919876543299', company_name='Flag Co',
                                          needs_review=True, review_note='A reply reached the bot out of order.')

    def test_a_flagged_query_shows_on_the_dashboard_and_detail_page(self):
        self.assertContains(self.client.get(reverse('query_dashboard')), 'Needs a look')
        detail = self.client.get(reverse('query_detail', kwargs={'pk': self.query.pk}))
        self.assertContains(detail, 'needs a look')
        self.assertContains(detail, 'A reply reached the bot out of order.')

    def test_marking_it_reviewed_clears_the_flag(self):
        self.client.post(reverse('query_clear_review', kwargs={'pk': self.query.pk}))
        self.query.refresh_from_db()
        self.assertFalse(self.query.needs_review)
        self.assertEqual(self.query.review_note, '')
        self.assertNotContains(self.client.get(reverse('query_dashboard')), 'Needs a look')

    def test_a_get_does_not_clear_it(self):
        self.client.get(reverse('query_clear_review', kwargs={'pk': self.query.pk}))
        self.query.refresh_from_db()
        self.assertTrue(self.query.needs_review)

    def test_anonymous_cannot_clear_it(self):
        self.client.logout()
        self.client.post(reverse('query_clear_review', kwargs={'pk': self.query.pk}))
        self.query.refresh_from_db()
        self.assertTrue(self.query.needs_review)


@override_settings(WHATSAPP_VERIFY_TOKEN='test-verify-token', WHATSAPP_APP_SECRET='test-app-secret')
class WhatsAppReviewBase(TestCase):
    """Shared plumbing for the end-of-conversation tests: a query waiting at the summary, and ways to
    send the bot taps and text and read back what it replied (the sends are captured, not made)."""

    PHONE = '919876543210'
    PRODUCTS = 1

    def setUp(self):
        patcher = patch('materials.views.whatsapp._send_whatsapp_steps_background')
        self.steps = patcher.start()
        self.addCleanup(patcher.stop)
        text_patcher = patch('materials.views.whatsapp._send_whatsapp_text_message_background')
        self.say = text_patcher.start()
        self.addCleanup(text_patcher.stop)
        self.query = _query_awaiting(None, phone=self.PHONE, items=self.PRODUCTS, bot_stage='summary', company_name='Ramesh Traders')
        for number, item in enumerate(self.query.item_list()):
            item.width, item.thickness, item.quantity = Decimal('50') + number, Decimal('6.5'), Decimal('8000')
            item.save()
        self.counter = 0

    def _send(self, body):
        self.counter += 1
        value = {'messages': [{'from': self.PHONE, 'id': f'wamid.R{self.counter}', 'timestamp': str(int(time.time())), **body}]}
        payload = json.dumps({'entry': [{'changes': [{'value': value}]}]}).encode('utf-8')
        return self.client.post(reverse('whatsapp_webhook'), data=payload, content_type='application/json',
                                HTTP_X_HUB_SIGNATURE_256=_sign_whatsapp_payload(payload, 'test-app-secret'))

    def _text(self, text):
        return self._send({'type': 'text', 'text': {'body': text}})

    def _tap_button(self, title):
        return self._send({'type': 'interactive', 'interactive': {'type': 'button_reply', 'button_reply': {'id': title.lower(), 'title': title}}})

    def _tap_row(self, row_id, title='x'):
        return self._send({'type': 'interactive', 'interactive': {'type': 'list_reply', 'list_reply': {'id': row_id, 'title': title}}})

    def _refresh(self):
        self.query.refresh_from_db()
        return self.query

    def _items(self):
        return list(self.query.items.order_by('position', 'pk'))

    def _last_steps(self):
        return self.steps.call_args[0][1]

    def _start_editing(self, row_id, *then):
        """Change something -> a row (a query answer, or a product and then one of its details)."""
        self._tap_button('Change something')
        self._tap_row(row_id)
        for next_row in then:
            self._tap_row(next_row)


class WhatsAppReviewTests(WhatsAppReviewBase):
    """The end of the conversation for one product: a summary, Confirm / Change, and a confirmation
    before a change is stored."""

    # ── the summary ───────────────────────────────────────────────────────────

    def test_when_the_last_answer_arrives_the_summary_and_buttons_are_sent_instead_of_thanks(self):
        query = _query_awaiting('quantity', phone='919876500001')
        self._send_to('919876500001', '8000', 'wamid.LAST')
        query.refresh_from_db()
        self.assertEqual(query.bot_stage, 'summary')
        self.assertEqual(query.items.get().quantity, Decimal('8000'))
        phone, steps = self.steps.call_args[0]
        self.assertEqual(phone, '919876500001')
        self.assertEqual([step[0] for step in steps], ['text', 'buttons'])   # one thread, in this order
        self.assertEqual(steps[1], ('buttons', WHATSAPP_SUMMARY_QUESTION, WHATSAPP_SUMMARY_CHOICES))
        self.assertNotIn(WHATSAPP_CLOSING_MESSAGE, [step[1] for step in steps])   # thanks comes after Confirm

    def _send_to(self, phone, text, message_id):
        value = {'messages': [{'from': phone, 'id': message_id, 'timestamp': str(int(time.time())), 'type': 'text', 'text': {'body': text}}]}
        payload = json.dumps({'entry': [{'changes': [{'value': value}]}]}).encode('utf-8')
        return self.client.post(reverse('whatsapp_webhook'), data=payload, content_type='application/json',
                                HTTP_X_HUB_SIGNATURE_256=_sign_whatsapp_payload(payload, 'test-app-secret'))

    def test_the_summary_lists_every_answer(self):
        summary = whatsapp._summary_text(self._refresh())
        for text in ('Company name: Ramesh Traders', 'Email: ramesh@example.com', 'GST number & address: 22AAAAA0000A1Z5, 12 Industrial Area',
                     'Product: Flat Bright Bar · EN8D · 50 x 6.5 mm · 8000 kg · Coil',
                     'Drawing / sample: no', 'Make/properties/process: no', 'End use: automotive shafts'):
            with self.subTest(text=text):
                self.assertIn(text, summary)

    def test_the_summary_buttons_fit_whatsapp_limits(self):
        self.assertLessEqual(len(WHATSAPP_SUMMARY_CHOICES), 3)
        for _, label in WHATSAPP_SUMMARY_CHOICES + whatsapp.WHATSAPP_CHANGE_CHOICES:
            self.assertLessEqual(len(label), 20)

    # ── confirm ───────────────────────────────────────────────────────────────

    def test_confirm_stores_the_confirmation_and_only_then_says_thanks(self):
        for reply in ['Confirm', 'yes', 'Yes it is correct']:
            with self.subTest(reply=reply):
                Query.objects.filter(pk=self.query.pk).update(bot_stage='summary', intake_confirmed_at=None)
                self.steps.reset_mock()
                (self._tap_button if reply == 'Confirm' else self._text)(reply)
                query = self._refresh()
                self.assertEqual(query.bot_stage, 'done')
                self.assertIsNotNone(query.intake_confirmed_at)
                self.assertEqual(self._last_steps(), [('text', WHATSAPP_CLOSING_MESSAGE)])

    def test_a_message_after_confirming_is_just_noted(self):
        self._tap_button('Confirm')
        self.steps.reset_mock()
        self._text('also need delivery by Friday')
        self.assertIn('also need delivery by Friday', self._refresh().notes)
        self.steps.assert_not_called()

    def test_something_unclear_at_the_summary_shows_the_buttons_again(self):
        self._text('hmm')
        self.assertEqual(self._refresh().bot_stage, 'summary')
        self.assertEqual(self._last_steps(), [('buttons', WHATSAPP_SUMMARY_QUESTION, WHATSAPP_SUMMARY_CHOICES)])

    # ── change ────────────────────────────────────────────────────────────────

    def test_change_offers_a_tappable_list_that_respects_the_row_limit(self):
        self._tap_button('Change something')
        self.assertEqual(self._refresh().bot_stage, 'pick_field')
        kind, text, button, rows = self._last_steps()[0]
        self.assertEqual((kind, button), ('list', 'Choose'))
        self.assertLessEqual(len(rows), 10)
        self.assertLessEqual(len(button), 20)
        for row_id, title, description in rows:
            self.assertLessEqual(len(title), 24)
            self.assertLessEqual(len(description), 72)
        ids = [r[0] for r in rows]
        self.assertEqual(ids[-1], 'more')
        self.assertIn('item:0', ids)                  # the product, which opens its details
        self.assertIn('field:company_name', ids)

    def test_every_answer_can_be_reached_from_the_lists(self):
        first = whatsapp._change_list_step(self.query)[3]
        second = whatsapp._change_list_step(self.query, page_two=True)[3]
        reachable = {r[0].split(':', 1)[1] for r in first + second if r[0].startswith('field:')}
        self.assertEqual(reachable, {key for key, _ in whatsapp.WHATSAPP_QUERY_REVIEW_FIELDS})
        details = whatsapp._item_field_list_step(self.query, 0)[3]
        self.assertEqual({r[0].split(':', 1)[1] for r in details if r[0].startswith('field:')},
                         {key for key, _ in whatsapp.WHATSAPP_ITEM_REVIEW_FIELDS})
        for rows in (first, second, details):
            self.assertLessEqual(len(rows), 10)

    def test_more_and_back_move_between_the_lists(self):
        self._tap_button('Change something')
        self._tap_row('more')
        self.assertEqual(self._refresh().bot_stage, 'pick_more')
        self.assertIn('field:end_use', [r[0] for r in self._last_steps()[0][3]])
        self._tap_row('back')
        self.assertEqual(self._refresh().bot_stage, 'pick_field')

    def test_choosing_a_query_answer_asks_its_question_again(self):
        self._start_editing('field:contact_email')
        query = self._refresh()
        self.assertEqual((query.bot_stage, query.edit_field), ('editing', 'contact_email'))
        self.assertEqual(self._last_steps(), [('text', WHATSAPP_QUERY_QUESTIONS['contact_email'])])

    def test_choosing_the_product_opens_its_details_and_a_detail_asks_its_question_again(self):
        self._start_editing('item:0')
        self.assertEqual(self._refresh().bot_stage, 'pick_item_field')
        self.assertEqual({r[0] for r in self._last_steps()[0][3]}, {'field:product_category', 'field:grade', 'field:width',
                                                                    'field:thickness', 'field:delivery_form', 'field:quantity', 'back'})
        self._tap_row('field:width')
        query = self._refresh()
        self.assertEqual((query.bot_stage, query.edit_field), ('editing', 'item:0:width'))
        self.assertEqual(self._last_steps(), [('text', WHATSAPP_QUERY_QUESTIONS['width'])])   # no "Product 1:" with one product

    def test_back_from_a_products_details_returns_to_the_first_list(self):
        self._start_editing('item:0', 'back')
        self.assertEqual(self._refresh().bot_stage, 'pick_field')

    def test_typing_what_to_change_works_too(self):
        self._tap_button('Change something')
        self._text('the thickness please')
        self.assertEqual(self._refresh().edit_field, 'item:0:thickness')   # one product: straight to the detail
        Query.objects.filter(pk=self.query.pk).update(bot_stage='pick_field')
        self._text('my email')
        self.assertEqual(self._refresh().edit_field, 'contact_email')

    def test_the_tappable_questions_are_asked_as_lists_and_buttons_when_changing(self):
        self._start_editing('item:0', 'field:product_category')
        self.assertEqual(self._last_steps()[0][0], 'list')
        Query.objects.filter(pk=self.query.pk).update(bot_stage='pick_item_field')
        self._tap_row('field:delivery_form')
        self.assertEqual(self._last_steps()[0][0], 'buttons')

    # ── the new answer is held until confirmed ────────────────────────────────

    def test_a_new_answer_is_not_stored_until_the_customer_confirms_it(self):
        self._start_editing('item:0', 'field:width')
        self._text('75')
        query = self._refresh()
        self.assertEqual(self._items()[0].width, Decimal('50'))   # still the old value
        self.assertEqual(query.bot_stage, 'confirm_change')
        self.assertEqual(query.pending_value, {'_items': {'width': ['75']}, '_item_only': 0})
        kind, text, choices = self._last_steps()[0]
        self.assertEqual(kind, 'buttons')
        self.assertIn('width to:\n75 mm', text)
        self.assertEqual(choices, whatsapp.WHATSAPP_CHANGE_CHOICES)

    def test_yes_saves_it_and_shows_the_updated_summary(self):
        self._start_editing('item:0', 'field:width')
        self._text('75')
        self._tap_button('Yes, save it')
        query = self._refresh()
        self.assertEqual(self._items()[0].width, Decimal('75'))
        self.assertEqual((query.bot_stage, query.pending_value, query.edit_field), ('summary', {}, ''))
        steps = self._last_steps()
        self.assertEqual([s[0] for s in steps], ['text', 'buttons'])
        self.assertIn("Done, I've updated that.", steps[0][1])
        self.assertIn('75 x 6.5 mm', steps[0][1])

    def test_no_keeps_the_old_value(self):
        self._start_editing('item:0', 'field:width')
        self._text('75')
        self._tap_button('No, keep old')
        query = self._refresh()
        self.assertEqual((self._items()[0].width, query.bot_stage, query.pending_value), (Decimal('50'), 'summary', {}))
        self.assertIn('kept it as it was', self._last_steps()[0][1])
        self.assertIn('50 x 6.5 mm', self._last_steps()[0][1])

    def test_an_invalid_new_answer_is_asked_again_and_nothing_is_held(self):
        self._start_editing('item:0', 'field:width')
        self.steps.reset_mock()
        self._text('as per drawing')
        query = self._refresh()
        self.assertEqual((query.bot_stage, query.pending_value, self._items()[0].width), ('editing', {}, Decimal('50')))
        self.assertEqual(self._last_steps(), [('text', WHATSAPP_DIMENSION_INVALID_MESSAGES['width'])])

    def test_cancel_goes_back_to_the_summary(self):
        self._start_editing('item:0', 'field:width')
        self._text('cancel')
        self.assertEqual(self._refresh().bot_stage, 'summary')

    def test_each_kind_of_answer_can_be_changed(self):
        cases = {
            ('field:company_name',): ('New Traders', lambda q, i: q.company_name == 'New Traders'),
            ('field:contact_email',): ('new@example.com', lambda q, i: q.contact_email == 'new@example.com'),
            ('field:gst_number',): ('06AAAAA0000A1Z5, 9 New Road', lambda q, i: (q.gst_number, q.gst_address) == ('06AAAAA0000A1Z5', '9 New Road')),
            ('field:technical_requirements',): ('Tata make', lambda q, i: q.technical_requirements == 'Tata make'),
            ('field:end_use',): ('gear shafts', lambda q, i: q.end_use == 'gear shafts'),
            ('field:drawing',): ('no drawing', lambda q, i: q.drawing_notes == 'no drawing'),
            ('item:0', 'field:thickness'): ('8', lambda q, i: i.thickness == Decimal('8')),
            ('item:0', 'field:quantity'): ('12000', lambda q, i: i.quantity == Decimal('12000')),
            ('item:0', 'field:delivery_form'): ('Bar', lambda q, i: i.delivery_form == 'Bar'),
            ('item:0', 'field:product_category'): ('square bright bar', lambda q, i: i.product_category.name == 'Square Bright Bar'),
            ('item:0', 'field:grade'): ('ss-304', lambda q, i: i.grade == 'SS304'),
        }
        for route, (answer, check) in cases.items():
            with self.subTest(route=route):
                Query.objects.filter(pk=self.query.pk).update(bot_stage='summary', edit_field='')
                self._start_editing(*route)
                self._text(answer)
                self.assertEqual(self._refresh().bot_stage, 'confirm_change')
                self._tap_button('Yes, save it')
                self.assertTrue(check(self._refresh(), self._items()[0]), route)

    def test_changing_the_gst_number_alone_keeps_the_address(self):
        self._start_editing('field:gst_number')
        self._text('06AAAAA0000A1Z5')
        self._tap_button('Yes, save it')
        query = self._refresh()
        self.assertEqual((query.gst_number, query.gst_address), ('06AAAAA0000A1Z5', '12 Industrial Area, Faridabad'))

    def test_changing_the_grade_relinks_the_product_code(self):
        flat = ProductCategory.objects.get(name='Flat Bright Bar')
        old = ProductType.objects.create(item_code='FBB001', category=flat, grade='EN8D')
        new = ProductType.objects.create(item_code='FBB002', category=flat, grade='SS304')
        QueryItem.objects.filter(query=self.query).update(product_type=old)
        self._start_editing('item:0', 'field:grade')
        self._text('SS304')
        self._tap_button('Yes, save it')
        self.assertEqual(self._items()[0].product_type, new)
        Query.objects.filter(pk=self.query.pk).update(bot_stage='summary')
        self._start_editing('item:0', 'field:grade')
        self._text('EN9')   # no code for this one: the link is dropped, not left pointing at the wrong code
        self._tap_button('Yes, save it')
        self.assertIsNone(self._items()[0].product_type)

    def test_a_text_answer_replaces_an_attached_drawing(self):
        self.query.drawing.save('old.pdf', ContentFile(b'%PDF old'), save=True)
        self.addCleanup(lambda: self.query.drawing and self.query.drawing.delete(save=False))
        self.assertIn('File attached', whatsapp._summary_text(self._refresh()))
        self._start_editing('field:drawing')
        self._text('no')
        self._tap_button('Yes, save it')
        query = self._refresh()
        self.assertFalse(query.drawing)
        self.assertEqual(query.drawing_notes, 'no')

    def test_a_window_that_has_closed_sends_nothing_but_the_change_still_works(self):
        self.query.last_inbound_at = timezone.now() - timedelta(days=2)
        self.query.save()
        self._tap_button('Change something')   # delivered late: its own timestamp is "now", so the window re-opens
        self.assertEqual(self._refresh().bot_stage, 'pick_field')


class WhatsAppMultiProductReviewTests(WhatsAppReviewBase):
    """With several products the summary lists each, and Change asks which product first."""

    PRODUCTS = 3

    def test_the_summary_lists_each_product(self):
        summary = whatsapp._summary_text(self._refresh())
        for number, width in enumerate(('50', '51', '52'), start=1):
            self.assertIn(f'Product {number}: Flat Bright Bar · EN8D · {width} x 6.5 mm · 8000 kg · Coil', summary)

    def test_the_first_list_has_a_row_per_product_and_stays_within_ten_rows(self):
        for products in (1, 3, 5):
            with self.subTest(products=products):
                query = _query_awaiting(None, phone=f'91987650{products:04d}', items=products)
                rows = whatsapp._change_list_step(query)[3]
                self.assertLessEqual(len(rows), 10)
                self.assertEqual([r[0] for r in rows if r[0].startswith('item:')], [f'item:{n}' for n in range(products)])

    def test_choosing_a_product_names_it_when_there_are_several(self):
        self._start_editing('item:1')
        kind, heading, button, rows = self._last_steps()[0]
        self.assertIn('Product 2', heading)
        self._tap_row('field:grade')
        self.assertEqual(self._refresh().edit_field, 'item:1:grade')
        self.assertEqual(self._last_steps()[0][1], 'Product 2: ' + WHATSAPP_QUERY_QUESTIONS['grade'])

    def test_a_change_touches_only_the_product_chosen(self):
        self._start_editing('item:1', 'field:width')
        self._text('99')
        self.assertIn('product 2 width to:\n99 mm', self._last_steps()[0][1])
        self._tap_button('Yes, save it')
        self.assertEqual([item.width for item in self._items()], [Decimal('50'), Decimal('99'), Decimal('52')])
        self.assertIn('Product 2: Flat Bright Bar · EN8D · 99 x 6.5 mm', self._last_steps()[0][1])

    def test_typing_product_2_picks_it(self):
        self._tap_button('Change something')
        self._text('product 3')
        query = self._refresh()
        self.assertEqual((query.bot_stage, query.edit_field), ('pick_item_field', 'item:2'))

    def test_the_grade_of_one_product_relinks_only_that_products_code(self):
        flat = ProductCategory.objects.get(name='Flat Bright Bar')
        codes = {grade: ProductType.objects.create(item_code=f'FBB-{grade}', category=flat, grade=grade) for grade in ('EN8D', 'SS304')}
        QueryItem.objects.filter(query=self.query).update(product_type=codes['EN8D'])
        self._start_editing('item:2', 'field:grade')
        self._text('SS304')
        self._tap_button('Yes, save it')
        self.assertEqual([item.product_type for item in self._items()], [codes['EN8D'], codes['EN8D'], codes['SS304']])


class QueryDetailShowsTheConfirmationTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user('confirm_detail_staff', password='pw', is_staff=True)
        self.client.force_login(self.staff)

    def test_waiting_and_confirmed_states_are_shown(self):
        query = Query.objects.create(source='whatsapp', contact_phone='919876500009', bot_stage='summary')
        url = reverse('query_detail', kwargs={'pk': query.pk})
        self.assertContains(self.client.get(url), 'Waiting for the customer to confirm their answers')
        query.bot_stage, query.intake_confirmed_at = 'done', timezone.now()
        query.save()
        html = self.client.get(url).content.decode()
        self.assertIn('Answers confirmed', html)
        self.assertIn('By the customer on WhatsApp', html)

    def test_nothing_is_shown_for_a_query_that_never_reached_the_review(self):
        query = Query.objects.create(source='call', contact_phone='919876500010')
        html = self.client.get(reverse('query_detail', kwargs={'pk': query.pk})).content.decode()
        self.assertNotIn('Answers confirmed', html)
        self.assertNotIn('WhatsApp review', html)


@override_settings(WHATSAPP_VERIFY_TOKEN='test-verify-token', WHATSAPP_APP_SECRET='test-app-secret')
class WhatsAppMultiProductIntakeTests(WhatsAppReviewBase):
    """Asking how many products, then one comma-separated list per question."""

    PHONE = '919876599999'

    def setUp(self):
        super().setUp()
        self.query.delete()
        self.query = None

    def _reply(self, text):
        return self._text(text)

    def _awaiting(self, field, items=3):
        self.query = _query_awaiting(field, phone=self.PHONE, items=items)
        return self.query

    def test_the_count_comes_first_and_creates_that_many_products(self):
        for text, expected in [('3', 3), ('three', 3), ('2 products', 2), ('5', 5)]:
            with self.subTest(text=text):
                Query.objects.filter(contact_phone=self.PHONE).delete()
                _query_awaiting('item_count', phone=self.PHONE)
                self._reply(text)
                self.assertEqual(Query.objects.get(contact_phone=self.PHONE).items.count(), expected)

    def test_a_bad_count_is_asked_again_and_creates_nothing(self):
        for text in ('6', '0', 'lots'):
            with self.subTest(text=text):
                Query.objects.filter(contact_phone=self.PHONE).delete()
                _query_awaiting('item_count', phone=self.PHONE)
                self._reply(text)
                self.assertEqual(Query.objects.get(contact_phone=self.PHONE).items.count(), 0)

    def test_the_count_question_is_a_list_of_one_to_five(self):
        rows = whatsapp._item_count_rows()
        self.assertEqual([r[0] for r in rows], [f'count:{n}' for n in range(1, 6)])

    def test_a_list_with_one_value_per_product_fills_them_in_order(self):
        self._awaiting('width')
        self._reply('50, 60.5 , 70')
        self.assertEqual([i.width for i in self._items()], [Decimal('50'), Decimal('60.5'), Decimal('70')])

    def test_the_wrong_number_of_values_is_refused_with_the_counts(self):
        self._awaiting('width')
        self._reply('50, 60')
        self.assertEqual([i.width for i in self._items()], [None, None, None])
        sent = ' '.join(str(c) for c in self.say.call_args_list)
        self.assertIn('I received 2 values but need 3', sent)

    def test_a_bad_value_names_the_product(self):
        self._awaiting('width')
        self._reply('50, abc, 70')
        sent = ' '.join(str(c) for c in self.say.call_args_list)
        self.assertIn('Product 2:', sent)

    def test_product_types_and_delivery_forms_are_read_from_lists(self):
        self._awaiting('product_category')
        self._reply('Flat Bright Bar, Square Bright Bar, Flat Bright Bar')
        self.assertEqual([i.product_category.name for i in self._items()],
                         ['Flat Bright Bar', 'Square Bright Bar', 'Flat Bright Bar'])
        Query.objects.filter(contact_phone=self.PHONE).delete()
        self._awaiting('delivery_form')
        self._reply('coil, bar, coil')
        self.assertEqual([i.delivery_form for i in self._items()], ['Coil', 'Bar', 'Coil'])
