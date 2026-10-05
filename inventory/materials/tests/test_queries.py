"""The Query dashboard (pre-quote leads)."""

import re
from decimal import Decimal
from django.contrib.auth.models import User
from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from unittest.mock import patch

from ..models import Customer, Order, ProductCategory, ProductType, Query, Quotation
from ..views.whatsapp import (
    WhatsAppSendError,
    WHATSAPP_QUERY_INTAKE_TEMPLATE,
    WHATSAPP_QUERY_INTAKE_TEMPLATE_LANGUAGE,
)
from .helpers import quotation_item_post_data


class QueryDashboardTests(TestCase):
    """Queries are logged before any Customer/Order exists — staff decide
    which ones to pursue by sending a quote (email, or a copy-link fallback
    when there's no address on file)."""

    def setUp(self):
        self.staff = User.objects.create_user('query_staff', password='pw', is_staff=True)
        self.product_type = ProductType.objects.create(item_code='Query Bar', grade='EN8D', size='1.200')

    def test_anonymous_cannot_view_dashboard(self):
        response = self.client.get(reverse('query_dashboard'))
        self.assertRedirects(response, f"{reverse('admin_login')}?next={reverse('query_dashboard')}")

    @patch('materials.views.whatsapp._send_whatsapp_template_message')
    def test_logging_a_query_with_minimal_fields(self, mock_send):
        self.client.force_login(self.staff)
        response = self.client.post(reverse('query_dashboard'), {
            'source': 'indiamart', 'contact_phone': '9123456780',
        })
        self.assertRedirects(response, reverse('query_dashboard'))
        query = Query.objects.get(contact_phone='9123456780')
        self.assertEqual(query.source, 'indiamart')
        self.assertEqual(query.status, 'new')
        self.assertEqual(query.company_name, '')
        mock_send.assert_called_once_with(
            '9123456780', WHATSAPP_QUERY_INTAKE_TEMPLATE, language=WHATSAPP_QUERY_INTAKE_TEMPLATE_LANGUAGE,
        )

    @patch('materials.views.whatsapp._send_whatsapp_template_message')
    def test_logging_a_query_normalizes_phone_to_digits_only(self, mock_send):
        self.client.force_login(self.staff)
        self.client.post(reverse('query_dashboard'), {
            'source': 'indiamart', 'contact_phone': '+91 98765 43210',
        })
        Query.objects.get(contact_phone='919876543210')
        mock_send.assert_called_once_with(
            '919876543210', WHATSAPP_QUERY_INTAKE_TEMPLATE, language=WHATSAPP_QUERY_INTAKE_TEMPLATE_LANGUAGE,
        )

    @patch('materials.views.whatsapp._send_whatsapp_template_message')
    def test_logging_a_query_surfaces_warning_when_whatsapp_send_fails(self, mock_send):
        mock_send.side_effect = WhatsAppSendError("boom")
        self.client.force_login(self.staff)
        response = self.client.post(reverse('query_dashboard'), {
            'source': 'indiamart', 'contact_phone': '9123456780',
        }, follow=True)

        self.assertTrue(Query.objects.filter(contact_phone='9123456780').exists())
        self.assertContains(response, "Please reach out directly")

    @patch('materials.views.whatsapp._send_whatsapp_template_message')
    def test_logging_a_query_twice_for_same_phone_is_rejected(self, mock_send):
        self.client.force_login(self.staff)
        self.client.post(reverse('query_dashboard'), {'source': 'indiamart', 'contact_phone': '9123456780'})
        response = self.client.post(reverse('query_dashboard'), {'source': 'indiamart', 'contact_phone': '9123456780'})

        self.assertEqual(Query.objects.filter(contact_phone='9123456780').count(), 1)
        self.assertContains(response, "already in progress")
        mock_send.assert_called_once()

    @patch('materials.views.whatsapp._send_whatsapp_template_message')
    def test_logging_a_query_for_same_phone_allowed_once_prior_query_closed(self, mock_send):
        self.client.force_login(self.staff)
        Query.objects.create(source='call', contact_phone='9123456780', status='converted')
        response = self.client.post(reverse('query_dashboard'), {'source': 'indiamart', 'contact_phone': '9123456780'})

        self.assertRedirects(response, reverse('query_dashboard'))
        self.assertEqual(Query.objects.filter(contact_phone='9123456780').count(), 2)

    def test_logging_a_query_requires_phone_and_source(self):
        self.client.force_login(self.staff)
        response = self.client.post(reverse('query_dashboard'), {'source': 'indiamart', 'contact_phone': ''})
        self.assertContains(response, "Phone number is required.")
        self.assertEqual(Query.objects.count(), 0)

        response = self.client.post(reverse('query_dashboard'), {'source': '', 'contact_phone': '9123456780'})
        self.assertContains(response, "Please select where this query came from.")
        self.assertEqual(Query.objects.count(), 0)

    def test_logging_a_query_rejects_incomplete_phone(self):
        # The field is pre-filled with "+91 " — submitting without adding
        # the actual number normalizes to just "91", not empty, so this
        # needs its own check beyond "phone number is required".
        self.client.force_login(self.staff)
        response = self.client.post(reverse('query_dashboard'), {'source': 'indiamart', 'contact_phone': '+91 '})
        self.assertContains(response, "complete phone number")
        self.assertEqual(Query.objects.count(), 0)

    @override_settings(EMAIL_HOST_USER='sender@example.com', DEFAULT_FROM_EMAIL='sender@example.com')
    def test_send_quote_creates_customer_and_emails_the_link(self):
        query = Query.objects.create(source='referral', company_name='Referral Co', contact_email='ref@example.com')
        self.client.force_login(self.staff)
        response = self.client.post(
            f"{reverse('quotation_form')}?query={query.pk}",
            quotation_item_post_data(**{'item-0-rate_per_kg': '75.25'}), follow=True,
        )

        query.refresh_from_db()
        self.assertEqual(query.status, 'quote_sent')
        customer = Customer.objects.get(name='Referral Co')
        self.assertEqual(query.customer, customer)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn('ref@example.com', mail.outbox[0].to)
        quotation = Quotation.objects.get(customer=customer)
        self.assertEqual(quotation.source_query, query)
        self.assertEqual(quotation.line_items.get().rate_per_kg, Decimal('75.25'))
        self.assertContains(response, f"Quotation {quotation.formatted_no()} sent to {customer.email}")

    def test_send_quote_without_email_shows_copy_link_fallback(self):
        query = Query.objects.create(source='call', company_name='No Email Co', contact_phone='9999999999')
        self.client.force_login(self.staff)
        response = self.client.post(
            f"{reverse('quotation_form')}?query={query.pk}",
            quotation_item_post_data(**{'item-0-rate_per_kg': '75.25'}), follow=True,
        )

        query.refresh_from_db()
        self.assertEqual(query.status, 'quote_sent')
        self.assertEqual(len(mail.outbox), 0)
        self.assertContains(response, "Copy")

    def test_send_quote_rejects_missing_rate(self):
        query = Query.objects.create(source='call', company_name='Rateless Co', contact_email='rateless@example.com')
        self.client.force_login(self.staff)
        response = self.client.post(
            f"{reverse('quotation_form')}?query={query.pk}",
            quotation_item_post_data(**{'item-0-rate_per_kg': ''}), follow=True,
        )

        query.refresh_from_db()
        self.assertEqual(query.status, 'new')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Quotation.objects.count(), 0)
        self.assertEqual(len(mail.outbox), 0)

    def test_send_quote_form_prefills_grade_size_product_type_from_query(self):
        """The GET-rendered form pre-fills the item row from the query —
        submitting it unchanged (the normal path: a real browser submits
        whatever's already in the pre-filled inputs) carries those values
        through to the QuotationLineItem."""
        query = Query.objects.create(
            source='call', company_name='Prefill Rate Co', contact_email='prefill@example.com',
            product_type=self.product_type, grade='EN8D', size=Decimal('1.200'),
        )
        self.client.force_login(self.staff)
        get_response = self.client.get(f"{reverse('quotation_form')}?query={query.pk}")
        self.assertContains(get_response, 'value="EN8D"')
        self.assertContains(get_response, 'value="1.200"')

        self.client.post(
            f"{reverse('quotation_form')}?query={query.pk}",
            quotation_item_post_data(**{
                'item-0-rate_per_kg': '60', 'item-0-grade': 'EN8D', 'item-0-size': '1.200',
                'item-0-product_type': str(self.product_type.pk),
            }),
        )

        quotation = Quotation.objects.get(source_query=query)
        item = quotation.line_items.get()
        self.assertEqual(item.grade, 'EN8D')
        self.assertEqual(item.size, Decimal('1.200'))
        self.assertEqual(item.product_type, self.product_type)

    @override_settings(
        PUBLIC_QUOTE_BASE_URL='https://quote.mattadrawing.com',
        ALLOWED_HOSTS=['mdw.tail2734e7.ts.net', 'testserver'],
    )
    def test_copy_link_uses_public_base_url_not_the_admin_request_host(self):
        """Same private-address problem as the emailed link — the Copy Link
        fallback previously built its URL from the admin's own request
        host instead of PUBLIC_QUOTE_BASE_URL."""
        query = Query.objects.create(source='call', company_name='No Email Co', contact_phone='9999999999')
        self.client.force_login(self.staff)
        self.client.post(
            f"{reverse('quotation_form')}?query={query.pk}",
            quotation_item_post_data(**{'item-0-rate_per_kg': '75.25'}),
        )
        query.refresh_from_db()

        response = self.client.get(reverse('query_dashboard'), HTTP_HOST='mdw.tail2734e7.ts.net')
        self.assertContains(response, f"https://quote.mattadrawing.com/quote/{query.customer.quote_token}/")
        self.assertNotContains(response, 'tail2734e7')

    def test_not_interested_sets_status(self):
        query = Query.objects.create(source='other', company_name='Dead End Co')
        self.client.force_login(self.staff)
        self.client.post(reverse('query_not_interested', kwargs={'pk': query.pk}))
        query.refresh_from_db()
        self.assertEqual(query.status, 'not_interested')

    def test_anonymous_cannot_edit_query(self):
        query = Query.objects.create(source='call', contact_phone='9123456780')
        response = self.client.get(reverse('query_edit', kwargs={'pk': query.pk}))
        self.assertRedirects(response, reverse('home'))

    def test_edit_query_updates_fields(self):
        query = Query.objects.create(source='whatsapp', contact_phone='919123456780')
        self.client.force_login(self.staff)
        response = self.client.post(reverse('query_edit', kwargs={'pk': query.pk}), {
            'company_name': 'Fixed Co Name', 'contact_phone': '+91 91234 56780',
            'contact_email': 'fixed@example.com', 'grade': 'EN8D',
            'size': '1.200', 'quantity': '500', 'notes': 'corrected via dashboard',
        })
        self.assertRedirects(response, reverse('query_dashboard'))
        query.refresh_from_db()
        self.assertEqual(query.company_name, 'Fixed Co Name')
        self.assertEqual(query.contact_phone, '919123456780')
        self.assertEqual(query.contact_email, 'fixed@example.com')
        self.assertEqual(query.grade, 'EN8D')
        self.assertEqual(query.size, Decimal('1.200'))
        self.assertEqual(query.quantity, Decimal('500'))
        self.assertEqual(query.notes, 'corrected via dashboard')

    def test_edit_query_links_product_type(self):
        product_type = ProductType.objects.create(item_code='Edit Bar', grade='SS304', size='2.500')
        query = Query.objects.create(source='call', contact_phone='9123456780')
        self.client.force_login(self.staff)
        self.client.post(reverse('query_edit', kwargs={'pk': query.pk}), {
            'company_name': '', 'contact_phone': '9123456780', 'contact_email': '',
            'product_type': str(product_type.pk), 'grade': '', 'size': '', 'quantity': '', 'notes': '',
        })
        query.refresh_from_db()
        self.assertEqual(query.product_type, product_type)

    def test_edit_query_rejects_invalid_size(self):
        query = Query.objects.create(source='call', contact_phone='9123456780')
        self.client.force_login(self.staff)
        response = self.client.post(reverse('query_edit', kwargs={'pk': query.pk}), {
            'company_name': '', 'contact_phone': '9123456780', 'contact_email': '',
            'grade': '', 'size': 'not-a-number', 'quantity': '', 'notes': '',
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "must be a decimal number")
        query.refresh_from_db()
        self.assertIsNone(query.size)

    def test_edit_query_does_not_change_status_or_source(self):
        query = Query.objects.create(source='call', contact_phone='9123456780', status='quote_sent')
        self.client.force_login(self.staff)
        self.client.post(reverse('query_edit', kwargs={'pk': query.pk}), {
            'company_name': 'Renamed Co', 'contact_phone': '9123456780', 'contact_email': '',
            'grade': '', 'size': '', 'quantity': '', 'notes': '',
        })
        query.refresh_from_db()
        self.assertEqual(query.source, 'call')
        self.assertEqual(query.status, 'quote_sent')

    def test_quote_form_prefills_from_in_flight_query(self):
        customer = Customer.objects.create(name='Prefill Co')
        Query.objects.create(
            source='call', company_name='Prefill Co', customer=customer, status='quote_sent',
            product_type=self.product_type, grade='EN8D', size=Decimal('1.200'),
            quantity=Decimal('750'), notes='Call back before Friday',
        )
        response = self.client.get(reverse('quote_form', kwargs={'token': customer.quote_token}))
        self.assertContains(response, 'value="EN8D"')
        self.assertContains(response, 'value="1.200"')
        self.assertContains(response, 'value="750.000"')
        self.assertContains(response, 'Call back before Friday')

    def test_quote_form_blank_when_no_query(self):
        customer = Customer.objects.create(name='No Query Co')
        response = self.client.get(reverse('quote_form', kwargs={'token': customer.quote_token}))
        self.assertNotContains(response, 'value="EN8D"')

    def test_submitting_quote_form_converts_query_and_links_order(self):
        customer = Customer.objects.create(name='Convert Co')
        query = Query.objects.create(
            source='indiamart', company_name='Convert Co', customer=customer, status='quote_sent',
        )
        self.client.post(reverse('quote_form', kwargs={'token': customer.quote_token}), {'quantity': '250'})

        query.refresh_from_db()
        self.assertEqual(query.status, 'converted')
        order = Order.objects.get(customer=customer)
        self.assertEqual(order.source_query, query)

    @override_settings(EMAIL_HOST_USER='sender@example.com', DEFAULT_FROM_EMAIL='sender@example.com')
    def test_send_quote_falls_back_to_phone_when_no_company_name(self):
        query = Query.objects.create(source='whatsapp', contact_phone='9998887776')
        self.client.force_login(self.staff)
        self.client.post(
            f"{reverse('quotation_form')}?query={query.pk}",
            quotation_item_post_data(**{'item-0-rate_per_kg': '40'}),
        )

        query.refresh_from_db()
        customer = Customer.objects.get(name='9998887776')
        self.assertEqual(query.customer, customer)


class QueryIntakeDetailsTests(TestCase):
    """What the WhatsApp bot collects (GST, product, make, ...) lives on the
    query's detail page, is editable, and is useful when staff go on to
    quote. The dashboard itself only shows name and number."""

    def setUp(self):
        self.staff = User.objects.create_user('intake_staff', password='pw', is_staff=True)
        self.client.force_login(self.staff)
        self.query = Query.objects.create(
            source='whatsapp', contact_phone='919876543210', company_name='Intake Co',
            contact_email='intake@example.com', grade='EN8D', quantity=Decimal('500'),
            gst_number='22AAAAA0000A1Z5', gst_address='12 Industrial Area, Faridabad',
            product_description='Round bar, 12 mm\nwith chamfer', technical_requirements='Tata make',
            quantity_text='2 tons monthly',
        )

    def test_gst_and_requirement_rows_are_kept_apart_and_in_asking_order(self):
        self.assertEqual(self.query.gst_rows(), [
            ('GST number', '22AAAAA0000A1Z5'),
            ('GST address', '12 Industrial Area, Faridabad'),
        ])
        self.assertEqual(self.query.requirement_rows(), [
            ('Requirements', 'Round bar, 12 mm\nwith chamfer'),
            ('Make / properties / process', 'Tata make'),
            ('End use', ''),  # blank rows are kept so the detail page can show a dash
            ('Delivery form', ''),
            ('Quantity & frequency', '2 tons monthly'),
        ])

    def test_dashboard_shows_only_name_and_number(self):
        html = self.client.get(reverse('query_dashboard')).content.decode()
        self.assertIn('Intake Co', html)
        self.assertIn('919876543210', html)
        for hidden in ('intake@example.com', 'GST number', '22AAAAA0000A1Z5', '12 Industrial Area',
                       'Tata make', 'Round bar', 'EN8D', '<details'):
            with self.subTest(hidden=hidden):
                self.assertNotIn(hidden, html)
        self.assertNotIn('<th>Product</th>', html)
        self.assertNotIn('<th>Notes</th>', html)

    def test_dashboard_links_each_query_to_its_detail_page(self):
        detail_url = reverse('query_detail', kwargs={'pk': self.query.pk})
        self.assertContains(self.client.get(reverse('query_dashboard')), f'href="{detail_url}"', count=2)  # name + Details button

    def test_detail_page_shows_everything(self):
        response = self.client.get(reverse('query_detail', kwargs={'pk': self.query.pk}))
        self.assertEqual(response.status_code, 200)
        for text in ('Intake Co', '919876543210', 'intake@example.com', 'WhatsApp', 'EN8D', '22AAAAA0000A1Z5',
                     '12 Industrial Area, Faridabad', 'Round bar, 12 mm', 'Tata make', '2 tons monthly'):
            with self.subTest(text=text):
                self.assertContains(response, text)

    def test_detail_page_shows_a_dash_for_unanswered_fields(self):
        bare = Query.objects.create(source='call', contact_phone='9000000000')
        response = self.client.get(reverse('query_detail', kwargs={'pk': bare.pk}))
        self.assertContains(response, 'GST number')
        self.assertContains(response, 'Make / properties / process')
        self.assertContains(response, '—')
        self.assertNotContains(response, 'Quotations')  # no quotations yet, so no empty card

    def test_detail_page_offers_the_same_actions_as_the_dashboard(self):
        response = self.client.get(reverse('query_detail', kwargs={'pk': self.query.pk}))
        self.assertContains(response, reverse('query_edit', kwargs={'pk': self.query.pk}))
        self.assertContains(response, f"{reverse('quotation_form')}?query={self.query.pk}")
        self.assertContains(response, reverse('query_not_interested', kwargs={'pk': self.query.pk}))

    def test_detail_page_lists_quotations_with_a_pdf_link_and_the_copy_link_once_quoted(self):
        customer = Customer.objects.create(name='Intake Co')
        self.query.customer = customer
        self.query.status = 'quote_sent'
        self.query.save(update_fields=['customer', 'status'])
        quotation = Quotation.objects.create(customer=customer, source_query=self.query, status='sent')
        response = self.client.get(reverse('query_detail', kwargs={'pk': self.query.pk}))
        self.assertContains(response, quotation.formatted_no())
        self.assertContains(response, reverse('quotation_pdf', kwargs={'pk': quotation.pk}))
        self.assertContains(response, 'Copy Link')

    def test_detail_page_requires_staff(self):
        self.client.logout()
        url = reverse('query_detail', kwargs={'pk': self.query.pk})
        self.assertRedirects(self.client.get(url), f"{reverse('admin_login')}?next={url}")

    def test_edit_form_shows_the_collected_answers(self):
        response = self.client.get(reverse('query_edit', kwargs={'pk': self.query.pk}))
        self.assertContains(response, '22AAAAA0000A1Z5')
        self.assertContains(response, '12 Industrial Area, Faridabad')
        self.assertContains(response, 'Round bar, 12 mm')

    def _edit(self, **overrides):
        data = {
            'company_name': 'Intake Co', 'contact_phone': '919876543210', 'contact_email': '',
            'product_type': '', 'grade': '', 'size': '', 'quantity': '', 'notes': '',
        }
        data.update({f: getattr(self.query, f) for f, _ in Query.INTAKE_TEXT_FIELDS})
        data.update(overrides)
        return self.client.post(reverse('query_edit', kwargs={'pk': self.query.pk}), data)

    def test_edit_saves_intake_fields_and_normalises_the_gst_number(self):
        response = self._edit(gst_number=' 27 bbbbb 1111 b 1z6 ', end_use='shafts', delivery_form='Bar', technical_requirements='polishing')
        self.assertRedirects(response, reverse('query_dashboard'))
        self.query.refresh_from_db()
        self.assertEqual(self.query.gst_number, '27BBBBB1111B1Z6')
        self.assertEqual(self.query.end_use, 'shafts')
        self.assertEqual(self.query.delivery_form, 'Bar')
        self.assertEqual(self.query.technical_requirements, 'polishing')

    def test_edit_rejects_a_malformed_gst_number_and_saves_nothing(self):
        for bad in ('X' * 20, 'NA', '22AAAAA0000A1Z', 'not a gstin'):
            with self.subTest(gst_number=bad):
                response = self._edit(gst_number=bad, end_use='should not be saved')
                self.assertContains(response, 'error-msg')
                self.query.refresh_from_db()
                self.assertEqual(self.query.gst_number, '22AAAAA0000A1Z5')
                self.assertEqual(self.query.end_use, '')

    def test_edit_rejects_a_delivery_form_that_is_not_coil_or_bar(self):
        response = self._edit(delivery_form='Sheet', end_use='should not be saved')
        self.assertContains(response, 'error-msg')
        self.query.refresh_from_db()
        self.assertEqual((self.query.delivery_form, self.query.end_use), ('', ''))

    def test_edit_form_offers_delivery_form_as_a_coil_or_bar_dropdown(self):
        self.query.delivery_form = 'Bar'
        self.query.save(update_fields=['delivery_form'])
        html = self.client.get(reverse('query_edit', kwargs={'pk': self.query.pk})).content.decode()
        self.assertIn('<select name="delivery_form">', html)
        self.assertIn('<option value="Coil" >Coil</option>', html)
        self.assertIn('<option value="Bar" selected>Bar</option>', html)

    def test_edit_can_leave_the_gst_number_blank(self):
        # Blank just means "not collected yet" (e.g. a phone-call lead); it can't be NA.
        self.assertRedirects(self._edit(gst_number=''), reverse('query_dashboard'))

    def test_quote_form_prefills_address_gstin_and_product_description(self):
        response = self.client.get(f"{reverse('quotation_form')}?query={self.query.pk}")
        form_initial = response.context['form'].initial
        self.assertEqual(form_initial['customer_address'], '12 Industrial Area, Faridabad\nGSTIN: 22AAAAA0000A1Z5')
        self.assertContains(response, 'value="Round bar, 12 mm"')  # first line only, in the item description

    def test_quote_form_has_no_address_prefill_without_gst_details(self):
        bare = Query.objects.create(source='call', contact_phone='9000000001', company_name='Bare Co')
        response = self.client.get(f"{reverse('quotation_form')}?query={bare.pk}")
        self.assertNotIn('customer_address', response.context['form'].initial)


class QuerySourceTests(TestCase):
    """Logging a query by hand offers four sources; Referral and Other each
    take optional follow-up details."""

    def setUp(self):
        self.staff = User.objects.create_user('source_staff', password='pw', is_staff=True)
        self.client.force_login(self.staff)

    def _log(self, **fields):
        data = {'source': 'indiamart', 'contact_phone': '9123456780'}
        data.update(fields)
        with patch('materials.views.whatsapp._send_whatsapp_template_message'):
            return self.client.post(reverse('query_dashboard'), data)

    def test_log_form_offers_exactly_indiamart_google_referral_and_other(self):
        html = self.client.get(reverse('query_dashboard')).content.decode()
        offered = re.findall(r'<option value="([a-z]+)"[^>]*>([^<]+)</option>', html.split('name="source"')[1].split('</select>')[0])
        self.assertEqual(offered, [('indiamart', 'IndiaMART'), ('google', 'Google'), ('referral', 'Referral'), ('other', 'Other')])

    def test_whatsapp_and_call_are_not_selectable_when_logging_by_hand(self):
        for source in ('whatsapp', 'call', 'bogus', ''):
            with self.subTest(source=source):
                response = self._log(source=source, contact_phone='9123456781')
                self.assertContains(response, 'Please select where this query came from.')
                self.assertEqual(Query.objects.count(), 0)

    def test_legacy_sources_still_display_for_existing_queries(self):
        for source, label in (('whatsapp', 'WhatsApp'), ('call', 'Phone Call')):
            query = Query.objects.create(source=source, contact_phone='9000000000' if source == 'call' else '9000000001')
            self.assertEqual(query.get_source_display(), label)

    def test_google_and_indiamart_need_no_extra_details(self):
        for index, source in enumerate(('google', 'indiamart')):
            self._log(source=source, contact_phone=f'91234567{index:02d}')
        self.assertEqual(sorted(Query.objects.values_list('source', flat=True)), ['google', 'indiamart'])

    def test_referral_saves_the_referrer_name_and_phone(self):
        self._log(source='referral', referrer_name='  Raju Traders ', referrer_phone='98100 12345')
        query = Query.objects.get()
        self.assertEqual((query.source, query.referrer_name, query.referrer_phone), ('referral', 'Raju Traders', '98100 12345'))

    def test_referral_details_are_optional(self):
        self._log(source='referral')
        query = Query.objects.get()
        self.assertEqual((query.source, query.referrer_name, query.referrer_phone), ('referral', '', ''))

    def test_other_saves_where_from_and_it_is_optional(self):
        self._log(source='other', source_detail='Trade fair, Delhi', contact_phone='9123456781')
        self._log(source='other', contact_phone='9123456782')
        self.assertEqual(sorted(Query.objects.values_list('source_detail', flat=True)), ['', 'Trade fair, Delhi'])

    def test_details_for_a_different_source_are_ignored(self):
        self._log(source='google', referrer_name='Ignored', referrer_phone='1', source_detail='Ignored too')
        query = Query.objects.get()
        self.assertEqual((query.referrer_name, query.referrer_phone, query.source_detail), ('', '', ''))

    def test_overlong_details_are_rejected_without_logging(self):
        response = self._log(source='referral', referrer_name='x' * 101)
        self.assertContains(response, 'error-msg')
        self.assertEqual(Query.objects.count(), 0)

    def test_form_markup_has_the_conditional_groups_hidden_by_default(self):
        html = self.client.get(reverse('query_dashboard')).content.decode()
        self.assertIn('<div id="referral-fields" style="display:none;">', html)
        self.assertIn('<div id="other-fields" style="display:none;">', html)
        self.assertIn('name="referrer_name"', html)
        self.assertIn('name="referrer_phone"', html)
        self.assertIn('name="source_detail"', html)

    def test_detail_page_shows_referrer_or_where_from_only_for_the_matching_source(self):
        referral = Query.objects.create(source='referral', contact_phone='9000000010', referrer_name='Raju', referrer_phone='98100')
        other = Query.objects.create(source='other', contact_phone='9000000011', source_detail='Trade fair')
        google = Query.objects.create(source='google', contact_phone='9000000012')
        page = lambda q: self.client.get(reverse('query_detail', kwargs={'pk': q.pk})).content.decode()  # noqa: E731
        self.assertIn('Referred by', page(referral))
        self.assertIn('Raju · 98100', page(referral))
        self.assertIn('Where from', page(other))
        self.assertIn('Trade fair', page(other))
        for text in ('Referred by', 'Where from'):
            self.assertNotIn(text, page(google))

    def test_edit_page_shows_and_saves_the_follow_up_for_its_own_source(self):
        referral = Query.objects.create(source='referral', contact_phone='9000000010', referrer_name='Raju')
        url = reverse('query_edit', kwargs={'pk': referral.pk})
        self.assertContains(self.client.get(url), 'name="referrer_name"')
        self.assertNotContains(self.client.get(url), 'name="source_detail"')
        base = {'company_name': '', 'contact_phone': '9000000010', 'contact_email': '', 'product_type': '',
                'grade': '', 'size': '', 'quantity': '', 'notes': ''}
        self.client.post(url, {**base, 'referrer_name': 'Raju Traders', 'referrer_phone': '98100 12345', 'source_detail': 'ignored'})
        referral.refresh_from_db()
        self.assertEqual((referral.referrer_name, referral.referrer_phone, referral.source_detail), ('Raju Traders', '98100 12345', ''))

        other = Query.objects.create(source='other', contact_phone='9000000011')
        other_url = reverse('query_edit', kwargs={'pk': other.pk})
        self.assertContains(self.client.get(other_url), 'name="source_detail"')
        self.client.post(other_url, {**base, 'contact_phone': '9000000011', 'source_detail': 'Walk-in'})
        other.refresh_from_db()
        self.assertEqual(other.source_detail, 'Walk-in')


class QuoteTrackingTests(TestCase):
    """Every send is its own immutable Quotation; sending again for the same
    query means the quote was updated, so the dashboard shows when it was
    sent and which revision it is, and all earlier quotes stay reachable."""

    def setUp(self):
        self.staff = User.objects.create_user('tracking_staff', password='pw', is_staff=True)
        self.client.force_login(self.staff)
        self.query = Query.objects.create(source='indiamart', company_name='Tracked Co', contact_phone='9123456780')

    def _send(self):
        with patch('materials.views.quotations._dispatch_quote_email'):
            return self.client.post(f"{reverse('quotation_form')}?query={self.query.pk}", quotation_item_post_data())

    def test_sent_at_is_set_when_a_quotation_is_sent_and_never_moves(self):
        customer = Customer.objects.create(name='Tracked Co')
        draft = Quotation.objects.create(customer=customer, status='draft')
        self.assertIsNone(draft.sent_at)
        draft.status = 'sent'
        draft.save()
        first = draft.sent_at
        self.assertIsNotNone(first)
        draft.ref_no = 'EDITED'
        draft.save()
        draft.refresh_from_db()
        self.assertEqual(draft.sent_at, first)

    def test_a_query_with_no_quotes_has_no_history(self):
        self.assertIsNone(self.query.latest_sent_quotation())
        self.assertEqual(self.query.quote_revision(), 0)
        self.assertEqual(self.query.quotation_history(), [])

    def test_each_send_is_a_new_quotation_and_counts_as_a_revision(self):
        self._send()
        self.assertEqual(Quotation.objects.filter(source_query=self.query, status='sent').count(), 1)
        self.assertEqual(self.query.quote_revision(), 0)
        self._send()
        self._send()
        quotes = list(Quotation.objects.filter(source_query=self.query, status='sent').order_by('quotation_no'))
        self.assertEqual(len(quotes), 3)
        self.assertEqual(len({q.quotation_no for q in quotes}), 3)   # none overwritten or renumbered
        query = Query.objects.get(pk=self.query.pk)
        self.assertEqual(query.quote_revision(), 2)
        self.assertEqual(query.latest_sent_quotation(), quotes[-1])

    def test_history_is_newest_first_with_revisions_and_drafts_on_top(self):
        self._send()
        self._send()
        draft = Quotation.objects.create(customer=Customer.objects.get(name='Tracked Co'), source_query=self.query, status='draft')
        history = Query.objects.get(pk=self.query.pk).quotation_history()
        self.assertEqual([row['revision'] for row in history], [None, 1, 0])
        self.assertEqual(history[0]['quotation'], draft)

    def test_dashboard_shows_when_the_quote_was_sent_and_the_revision(self):
        self._send()
        html = self.client.get(reverse('query_dashboard')).content.decode()
        self.assertIn('Quote sent', html)
        self.assertNotIn('Rev ', html)
        self.assertNotIn('View all', html)
        self._send()
        html = self.client.get(reverse('query_dashboard')).content.decode()
        self.assertIn('Rev 1', html)
        self.assertIn('View all 2 quotes', html)
        self.assertIn(f"{reverse('query_detail', kwargs={'pk': self.query.pk})}#quotations", html)

    def test_detail_page_lists_every_quote_with_its_revision_and_pdf(self):
        self._send()
        self._send()
        html = self.client.get(reverse('query_detail', kwargs={'pk': self.query.pk})).content.decode()
        self.assertIn('Quotation history', html)
        self.assertIn('Original', html)
        self.assertIn('Rev 1', html)
        for quotation in Quotation.objects.filter(source_query=self.query):
            with self.subTest(quotation=quotation.formatted_no()):
                self.assertIn(quotation.formatted_no(), html)
                self.assertIn(reverse('quotation_pdf', kwargs={'pk': quotation.pk}), html)

    def test_the_quote_form_prefills_the_revision_when_resending(self):
        first = self.client.get(f"{reverse('quotation_form')}?query={self.query.pk}").context['form'].initial
        self.assertNotIn('rev_no', first)
        self._send()
        second = self.client.get(f"{reverse('quotation_form')}?query={self.query.pk}").context['form'].initial
        self.assertEqual(second['rev_no'], 1)
        self.assertEqual(second['rev_date'], timezone.localdate())


class QueryProductTypeTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user('type_staff', password='pw', is_staff=True)
        self.client.force_login(self.staff)
        self.round_bar = ProductCategory.objects.get(name='Round Bright Bar')
        self.query = Query.objects.create(source='indiamart', contact_phone='9123456780', company_name='Type Co')

    def _edit(self, **overrides):
        data = {'company_name': 'Type Co', 'contact_phone': '9123456780', 'contact_email': '', 'product_type': '',
                'grade': '', 'size': '', 'quantity': '', 'notes': '', 'product_category': ''}
        data.update(overrides)
        return self.client.post(reverse('query_edit', kwargs={'pk': self.query.pk}), data)

    def test_edit_page_offers_all_fourteen_product_types(self):
        html = self.client.get(reverse('query_edit', kwargs={'pk': self.query.pk})).content.decode()
        self.assertIn('<select name="product_category">', html)
        for name in ProductCategory.objects.values_list('name', flat=True):
            with self.subTest(name=name):
                self.assertIn(f'>{name}</option>', html)

    def test_edit_saves_and_clears_the_product_type(self):
        self._edit(product_category=str(self.round_bar.pk))
        self.query.refresh_from_db()
        self.assertEqual(self.query.product_category, self.round_bar)
        self._edit(product_category='')
        self.query.refresh_from_db()
        self.assertIsNone(self.query.product_category)

    def test_detail_page_shows_the_product_type(self):
        self.query.product_category = self.round_bar
        self.query.save(update_fields=['product_category'])
        html = self.client.get(reverse('query_detail', kwargs={'pk': self.query.pk})).content.decode()
        self.assertIn('Product type', html)
        self.assertIn('Round Bright Bar', html)
