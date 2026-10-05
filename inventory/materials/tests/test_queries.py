"""The Query dashboard (pre-quote leads)."""

from decimal import Decimal
from django.contrib.auth.models import User
from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse
from unittest.mock import patch

from ..models import Customer, Order, ProductType, Query, Quotation
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
            'source': 'call', 'contact_phone': '9123456780',
        })
        self.assertRedirects(response, reverse('query_dashboard'))
        query = Query.objects.get(contact_phone='9123456780')
        self.assertEqual(query.source, 'call')
        self.assertEqual(query.status, 'new')
        self.assertEqual(query.company_name, '')
        mock_send.assert_called_once_with(
            '9123456780', WHATSAPP_QUERY_INTAKE_TEMPLATE, language=WHATSAPP_QUERY_INTAKE_TEMPLATE_LANGUAGE,
        )

    @patch('materials.views.whatsapp._send_whatsapp_template_message')
    def test_logging_a_query_normalizes_phone_to_digits_only(self, mock_send):
        self.client.force_login(self.staff)
        self.client.post(reverse('query_dashboard'), {
            'source': 'call', 'contact_phone': '+91 98765 43210',
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
            'source': 'call', 'contact_phone': '9123456780',
        }, follow=True)

        self.assertTrue(Query.objects.filter(contact_phone='9123456780').exists())
        self.assertContains(response, "Please reach out directly")

    @patch('materials.views.whatsapp._send_whatsapp_template_message')
    def test_logging_a_query_twice_for_same_phone_is_rejected(self, mock_send):
        self.client.force_login(self.staff)
        self.client.post(reverse('query_dashboard'), {'source': 'call', 'contact_phone': '9123456780'})
        response = self.client.post(reverse('query_dashboard'), {'source': 'call', 'contact_phone': '9123456780'})

        self.assertEqual(Query.objects.filter(contact_phone='9123456780').count(), 1)
        self.assertContains(response, "already in progress")
        mock_send.assert_called_once()

    @patch('materials.views.whatsapp._send_whatsapp_template_message')
    def test_logging_a_query_for_same_phone_allowed_once_prior_query_closed(self, mock_send):
        self.client.force_login(self.staff)
        Query.objects.create(source='call', contact_phone='9123456780', status='converted')
        response = self.client.post(reverse('query_dashboard'), {'source': 'call', 'contact_phone': '9123456780'})

        self.assertRedirects(response, reverse('query_dashboard'))
        self.assertEqual(Query.objects.filter(contact_phone='9123456780').count(), 2)

    def test_logging_a_query_requires_phone_and_source(self):
        self.client.force_login(self.staff)
        response = self.client.post(reverse('query_dashboard'), {'source': 'call', 'contact_phone': ''})
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
        response = self.client.post(reverse('query_dashboard'), {'source': 'call', 'contact_phone': '+91 '})
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
    """What the WhatsApp bot collects (GST, product, make, ...) must be
    visible to staff, editable, and useful when they go on to quote."""

    def setUp(self):
        self.staff = User.objects.create_user('intake_staff', password='pw', is_staff=True)
        self.client.force_login(self.staff)
        self.query = Query.objects.create(
            source='whatsapp', contact_phone='919876543210', company_name='Intake Co',
            gst_number='22AAAAA0000A1Z5', gst_address='12 Industrial Area, Faridabad',
            product_description='Round bar, 12 mm\nwith chamfer', technical_requirements='Tata make', quantity_text='2 tons monthly',
        )

    def test_gst_and_requirement_details_are_kept_apart_and_in_asking_order(self):
        self.assertEqual(self.query.gst_details(), [
            ('GST number', '22AAAAA0000A1Z5'),
            ('GST address', '12 Industrial Area, Faridabad'),
        ])
        self.assertEqual(self.query.requirement_details(), [
            ('Product', 'Round bar, 12 mm\nwith chamfer'),
            ('Make / properties / process', 'Tata make'),
            ('Quantity & frequency', '2 tons monthly'),
        ])

    def test_dashboard_shows_gst_with_the_company_and_only_product_details_under_requirements(self):
        import re
        Query.objects.create(source='call', contact_phone='9000000000', company_name='Bare Co')
        html = self.client.get(reverse('query_dashboard')).content.decode()

        requirements = re.findall(r'<details.*?</details>', html, re.S)
        self.assertEqual(len(requirements), 1)  # only the query that has product details gets one
        self.assertIn('Tata make', requirements[0])
        self.assertIn('Round bar', requirements[0])
        self.assertNotIn('GST', requirements[0])
        self.assertNotIn('22AAAAA0000A1Z5', requirements[0])

        company_cell = re.search(r'Intake Co.*?</td>', html, re.S).group(0)
        self.assertIn('GST number:', company_cell)
        self.assertIn('22AAAAA0000A1Z5', company_cell)
        self.assertIn('12 Industrial Area, Faridabad', company_cell)

    def test_gst_shows_even_when_there_are_no_product_details(self):
        Query.objects.all().delete()
        Query.objects.create(source='whatsapp', contact_phone='9111111111', company_name='Gst Only Co', gst_number='NA')
        html = self.client.get(reverse('query_dashboard')).content.decode()
        self.assertIn('GST number:', html)
        self.assertNotIn('<details', html)

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
        response = self._edit(gst_number=' 27 bbbbb 1111 b 1z6 ', end_use_delivery='shafts, coil', technical_requirements='polishing')
        self.assertRedirects(response, reverse('query_dashboard'))
        self.query.refresh_from_db()
        self.assertEqual(self.query.gst_number, '27BBBBB1111B1Z6')
        self.assertEqual(self.query.end_use_delivery, 'shafts, coil')
        self.assertEqual(self.query.technical_requirements, 'polishing')

    def test_edit_rejects_an_overlong_gst_number_and_saves_nothing(self):
        response = self._edit(gst_number='X' * 20, end_use_delivery='should not be saved')
        self.assertContains(response, 'error-msg')
        self.query.refresh_from_db()
        self.assertEqual(self.query.gst_number, '22AAAAA0000A1Z5')
        self.assertEqual(self.query.end_use_delivery, '')

    def test_quote_form_prefills_address_gstin_and_product_description(self):
        response = self.client.get(f"{reverse('quotation_form')}?query={self.query.pk}")
        form_initial = response.context['form'].initial
        self.assertEqual(form_initial['customer_address'], '12 Industrial Area, Faridabad\nGSTIN: 22AAAAA0000A1Z5')
        self.assertContains(response, 'value="Round bar, 12 mm"')  # first line only, in the item description

    def test_quote_form_leaves_gstin_out_for_an_unregistered_customer(self):
        self.query.gst_number = 'NA'
        self.query.save(update_fields=['gst_number'])
        response = self.client.get(f"{reverse('quotation_form')}?query={self.query.pk}")
        self.assertEqual(response.context['form'].initial['customer_address'], '12 Industrial Area, Faridabad')

    def test_quote_form_has_no_address_prefill_without_gst_details(self):
        bare = Query.objects.create(source='call', contact_phone='9000000001', company_name='Bare Co')
        response = self.client.get(f"{reverse('quotation_form')}?query={bare.pk}")
        self.assertNotIn('customer_address', response.context['form'].initial)
