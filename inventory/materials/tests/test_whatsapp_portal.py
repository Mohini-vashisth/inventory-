"""A chat started from a company's portal: the bot recognises the company, asks Yes/No, and a company already on
file goes straight to the product questions."""

import json
import time
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.urls import reverse

from .test_whatsapp import _sign_whatsapp_payload
from ..models import Customer, Order, Query
from ..views.whatsapp import WHATSAPP_PORTAL_CONFIRM_CHOICES, portal_chat_ref


@override_settings(WHATSAPP_VERIFY_TOKEN='t', WHATSAPP_APP_SECRET='test-app-secret')
class PortalChatTests(TestCase):
    PHONE = '919876500001'

    def setUp(self):
        self.acme = Customer.objects.create(name='ACME TOOLS', email='acme@example.com')
        Order.objects.create(customer=self.acme, quantity=5, status='completed')
        patches = {name: patch(f'materials.views.whatsapp.{name}') for name in (
            '_send_whatsapp_text_message_background', '_send_whatsapp_buttons_message_background',
            '_send_whatsapp_list_message_background', '_send_whatsapp_steps_background')}
        self.sent = {}
        for name, patcher in patches.items():
            self.sent[name.replace('_send_whatsapp_', '').replace('_background', '')] = patcher.start()
            self.addCleanup(patcher.stop)
        self.n = 0

    def say(self, body):
        self.n += 1
        value = {'messages': [{'from': self.PHONE, 'id': f'wamid.P{self.n}', 'timestamp': str(int(time.time())), **body}]}
        payload = json.dumps({'entry': [{'changes': [{'value': value}]}]}).encode()
        return self.client.post(reverse('whatsapp_webhook'), data=payload, content_type='application/json',
                                HTTP_X_HUB_SIGNATURE_256=_sign_whatsapp_payload(payload, 'test-app-secret'))

    def text(self, text):
        return self.say({'type': 'text', 'text': {'body': text}})

    def tap(self, title):
        return self.say({'type': 'interactive', 'interactive': {'type': 'button_reply', 'button_reply': {'id': title.lower(), 'title': title}}})

    def open_chat(self):
        return self.text(f"Hi, I'm from ACME TOOLS. Ref: {portal_chat_ref(self.acme)}")

    def query(self):
        return Query.objects.get(contact_phone=self.PHONE)

    def test_the_first_message_gets_a_yes_no_question_naming_the_company(self):
        self.open_chat()
        text, choices = self.sent['buttons_message'].call_args[0][1:3]
        self.assertIn('ACME TOOLS', text)
        self.assertEqual(choices, WHATSAPP_PORTAL_CONFIRM_CHOICES)
        self.assertEqual(self.query().bot_stage, 'portal_confirm')

    def test_yes_goes_straight_to_the_product_questions(self):
        self.open_chat()
        self.tap("Yes, that's us")
        query = self.query()
        self.assertEqual((query.company_name, query.contact_email, query.bot_stage, query.skip_company_details),
                         ('ACME TOOLS', 'acme@example.com', '', True))
        # the next thing asked is the number of products (no email or GST questions)
        self.assertEqual(self.sent['list_message'].call_args[0][2], 'Choose number')
        self.assertIn('Thank you', self.sent['list_message'].call_args[0][1])

    def test_no_asks_for_the_company_name_and_a_known_name_skips_to_products(self):
        self.open_chat()
        self.tap('No, someone else')
        self.assertIn('company name', self.sent['text_message'].call_args[0][1])
        self.text('acme  tools')
        query = self.query()
        self.assertEqual((query.company_name, query.skip_company_details, query.bot_stage), ('ACME TOOLS', True, ''))
        self.assertEqual(self.sent['list_message'].call_args[0][2], 'Choose number')

    def test_no_and_a_new_name_runs_the_normal_intake(self):
        self.open_chat()
        self.tap('No, someone else')
        self.text('Brand New Industries')
        query = self.query()
        self.assertEqual((query.company_name, query.skip_company_details, query.bot_stage), ('Brand New Industries', False, ''))
        self.assertIn('email', self.sent['text_message'].call_args[0][1].lower())   # the next normal question

    def test_an_unknown_ref_just_asks_for_the_company_name(self):
        self.text("Hi, I'm from NOBODY. Ref: 00000000")
        self.assertEqual(self.query().bot_stage, 'portal_name')
        self.assertIn('company name', self.sent['text_message'].call_args[0][1])

    def test_something_other_than_yes_or_no_asks_again(self):
        self.open_chat()
        self.text('hmm')
        self.assertEqual(self.sent['buttons_message'].call_count, 2)
        self.assertEqual(self.query().bot_stage, 'portal_confirm')

    def test_a_message_without_a_ref_is_handled_as_before(self):
        self.text('Hello there')
        self.assertEqual(self.query().bot_stage, '')
        self.sent['buttons_message'].assert_not_called()

    def test_a_number_with_an_open_query_keeps_its_conversation(self):
        Query.objects.create(source='call', contact_phone=self.PHONE, company_name='Earlier Co')
        self.open_chat()
        self.assertEqual(Query.objects.filter(contact_phone=self.PHONE).count(), 1)
        self.assertEqual(self.query().bot_stage, '')
