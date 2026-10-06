"""Quotations: the Send Quote form, drafts, and the PDF."""

from decimal import Decimal
from django.contrib.auth.models import User
from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse
from unittest.mock import patch

from ..models import Customer, ProductCategory, ProductType, Query, Quotation, QuotationLineItem
from ..pdf import generate_quotation_pdf
from .helpers import quotation_item_post_data


class QuotationFormDispatchTests(TestCase):
    """quotation_form is the one page every Send Quote action funnels
    into — this covers the ?customer=<pk> (existing customer, formerly
    send_quote_email) and no-params (brand-new customer, formerly
    quick_send_quote) paths. ?query=<pk> is covered separately in
    QueryDashboardTests, since that path also has to juggle a Query."""

    def setUp(self):
        self.staff = User.objects.create_user('quote_email_staff', password='pw', is_staff=True)
        self.customer = Customer.objects.create(name='Email Test Co', email='client@example.com')

    def _item_data(self, **overrides):
        return quotation_item_post_data(**overrides)

    def test_anonymous_cannot_send(self):
        response = self.client.post(
            f"{reverse('quotation_form')}?customer={self.customer.pk}", self._item_data(),
        )
        self.assertRedirects(response, reverse('home'))
        self.assertEqual(len(mail.outbox), 0)
        self.assertEqual(Quotation.objects.count(), 0)

    def test_anonymous_cannot_view_form(self):
        response = self.client.get(f"{reverse('quotation_form')}?customer={self.customer.pk}")
        self.assertRedirects(response, reverse('home'))

    @override_settings(EMAIL_HOST_USER='sender@example.com', DEFAULT_FROM_EMAIL='sender@example.com')
    def test_multiple_line_items_all_saved(self):
        self.client.force_login(self.staff)
        data = self._item_data()
        data.update({
            'item-TOTAL_FORMS': '2',
            'item-1-description': 'Steel Chamfer', 'item-1-quantity': '500',
            'item-1-rate_per_kg': '140.00', 'item-1-unit': 'KGS',
        })
        self.client.post(f"{reverse('quotation_form')}?customer={self.customer.pk}", data)
        quotation = Quotation.objects.get(customer=self.customer)
        self.assertEqual(quotation.line_items.count(), 2)
        self.assertEqual(quotation.subtotal(), Decimal('10') * Decimal('85.50') + Decimal('500') * Decimal('140.00'))

    def test_new_customer_form_prefilled_from_get_params(self):
        """'Send Form to Them' on the Orders dashboard carries the already-
        typed Company Name/Email/Phone into the URL rather than losing it."""
        response = self.client.get(reverse('quotation_form'))  # anonymous — just checking the redirect first
        self.assertRedirects(response, reverse('home'))
        self.client.force_login(self.staff)
        response = self.client.get(f"{reverse('quotation_form')}?name=Carried+Over+Co&email=co@example.com&phone=999")
        self.assertContains(response, 'value="Carried Over Co"')
        self.assertContains(response, 'value="co@example.com"')

    @override_settings(EMAIL_HOST_USER='sender@example.com', DEFAULT_FROM_EMAIL='sender@example.com')
    def test_staff_send_quote_delivers_with_the_link(self):
        self.client.force_login(self.staff)
        response = self.client.post(
            f"{reverse('quotation_form')}?customer={self.customer.pk}", self._item_data(), follow=True,
        )
        quotation = Quotation.objects.get(customer=self.customer)
        self.assertContains(response, f"Quotation {quotation.formatted_no()} sent to {self.customer.email}")
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn(self.customer.email, mail.outbox[0].to)
        self.assertIn(str(self.customer.quote_token), mail.outbox[0].body)
        self.assertEqual(quotation.line_items.get().rate_per_kg, Decimal('85.50'))
        self.assertEqual(len(mail.outbox[0].attachments), 1)
        filename, content, mimetype = mail.outbox[0].attachments[0]
        self.assertEqual(filename, f"{quotation.formatted_no()}.pdf")
        self.assertEqual(mimetype, 'application/pdf')
        self.assertTrue(content.startswith(b'%PDF'))

    def test_rejects_missing_rate(self):
        self.client.force_login(self.staff)
        response = self.client.post(
            f"{reverse('quotation_form')}?customer={self.customer.pk}",
            self._item_data(**{'item-0-rate_per_kg': ''}), follow=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Quotation.objects.count(), 0)
        self.assertEqual(len(mail.outbox), 0)

    def test_rejects_zero_or_negative_rate(self):
        self.client.force_login(self.staff)
        for bad_rate in ('0', '-5'):
            self.client.post(
                f"{reverse('quotation_form')}?customer={self.customer.pk}",
                self._item_data(**{'item-0-rate_per_kg': bad_rate}), follow=True,
            )
        self.assertEqual(Quotation.objects.count(), 0)
        self.assertEqual(len(mail.outbox), 0)

    @override_settings(EMAIL_HOST_USER='sender@example.com', DEFAULT_FROM_EMAIL='sender@example.com')
    def test_quotation_numbers_are_sequential(self):
        self.client.force_login(self.staff)
        self.client.post(f"{reverse('quotation_form')}?customer={self.customer.pk}", self._item_data())
        self.client.post(f"{reverse('quotation_form')}?customer={self.customer.pk}", self._item_data())
        numbers = list(Quotation.objects.order_by('quotation_no').values_list('quotation_no', flat=True))
        self.assertEqual(numbers, [numbers[0], numbers[0] + 1])

    @override_settings(
        EMAIL_HOST_USER='sender@example.com', DEFAULT_FROM_EMAIL='sender@example.com',
        PUBLIC_QUOTE_BASE_URL='https://quote.mattadrawing.com',
        ALLOWED_HOSTS=['mdw.tail2734e7.ts.net', 'testserver'],
    )
    def test_quote_link_uses_public_base_url_not_the_admin_request_host(self):
        """Admins only ever reach this app over Tailscale — the email must
        not link to that private address, which a real customer can't open."""
        self.client.force_login(self.staff)
        self.client.post(
            f"{reverse('quotation_form')}?customer={self.customer.pk}", self._item_data(),
            HTTP_HOST='mdw.tail2734e7.ts.net',
        )
        self.assertEqual(len(mail.outbox), 1)
        body = mail.outbox[0].body
        self.assertIn(f"https://quote.mattadrawing.com/quote/{self.customer.quote_token}/", body)
        self.assertNotIn('tail2734e7', body)

    @override_settings(EMAIL_HOST_USER='sender@example.com', DEFAULT_FROM_EMAIL='sender@example.com')
    def test_quote_link_falls_back_to_request_host_when_public_base_url_unset(self):
        self.client.force_login(self.staff)
        self.client.post(f"{reverse('quotation_form')}?customer={self.customer.pk}", self._item_data())
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn(f"testserver/quote/{self.customer.quote_token}/", mail.outbox[0].body)

    @override_settings(EMAIL_HOST_USER='sender@example.com', DEFAULT_FROM_EMAIL='sender@example.com')
    def test_email_says_the_link_is_single_use_not_a_standing_link(self):
        """The link stops working once an order is placed (the token is
        regenerated), so the email must not read like a permanent link."""
        self.client.force_login(self.staff)
        self.client.post(f"{reverse('quotation_form')}?customer={self.customer.pk}", self._item_data())
        body = mail.outbox[0].body
        self.assertIn('can be used once', body)
        self.assertIn('new quotation and link', body)
        self.assertNotIn('unique to your company', body)

    def test_fails_gracefully_without_recipient_address(self):
        no_email_customer = Customer.objects.create(name='No Email Co')
        self.client.force_login(self.staff)
        response = self.client.post(
            f"{reverse('quotation_form')}?customer={no_email_customer.pk}", self._item_data(), follow=True,
        )
        self.assertContains(response, f"No email on file for {no_email_customer.name}")
        self.assertEqual(len(mail.outbox), 0)
        # The Quotation record is still created even without an email on
        # file, so the rate quoted is on file either way.
        self.assertTrue(Quotation.objects.filter(customer=no_email_customer).exists())

    @override_settings(EMAIL_HOST_USER='')
    def test_shows_error_when_email_not_configured(self):
        self.client.force_login(self.staff)
        response = self.client.post(
            f"{reverse('quotation_form')}?customer={self.customer.pk}", self._item_data(), follow=True,
        )
        self.assertContains(response, "Email is not configured")
        self.assertEqual(len(mail.outbox), 0)

    @override_settings(EMAIL_HOST_USER='sender@example.com', DEFAULT_FROM_EMAIL='sender@example.com')
    def test_new_customer_creates_and_sends(self):
        self.client.force_login(self.staff)
        response = self.client.post(reverse('quotation_form'), self._item_data(**{
            'customer_name': 'Brand New Co', 'customer_email': 'new@example.com', 'customer_phone': '9999999999',
        }), follow=True)
        customer = Customer.objects.get(name='Brand New Co')
        quotation = Quotation.objects.get(customer=customer)
        self.assertContains(response, f"Quotation {quotation.formatted_no()} sent to new@example.com")
        self.assertEqual(customer.email, 'new@example.com')
        self.assertEqual(len(mail.outbox), 1)

    def test_new_customer_requires_company_name(self):
        self.client.force_login(self.staff)
        response = self.client.post(reverse('quotation_form'), self._item_data(**{
            'customer_email': 'rateless@example.com',
        }), follow=True)
        self.assertContains(response, "Company name is required")
        self.assertFalse(Customer.objects.filter(email='rateless@example.com').exists())
        self.assertEqual(len(mail.outbox), 0)

    def test_new_customer_without_email_still_creates_quotation(self):
        """No email on file is a valid state here too — same Copy Link/PDF
        fallback as the existing-customer and query paths, not a hard
        requirement like the old quick_send_quote enforced."""
        self.client.force_login(self.staff)
        response = self.client.post(reverse('quotation_form'), self._item_data(**{
            'customer_name': 'No Email Provided Co',
        }), follow=True)
        self.assertTrue(Customer.objects.filter(name='No Email Provided Co').exists())
        self.assertContains(response, "No email on file for No Email Provided Co")
        self.assertEqual(len(mail.outbox), 0)


class QuotationDraftTests(TestCase):
    """Save Draft is deliberately lenient — no formset validation, quantity/
    rate can be left blank, zero fully-formed items is fine — since a draft
    is explicitly a work in progress. Sending is unchanged: full validation,
    only then does it consume a quotation_no."""

    def setUp(self):
        self.staff = User.objects.create_user('draft_staff', password='pw', is_staff=True)

    def _draft_item_data(self, **overrides):
        data = {
            'item-TOTAL_FORMS': '1', 'item-INITIAL_FORMS': '0',
            'item-MIN_NUM_FORMS': '0', 'item-MAX_NUM_FORMS': '1000',
            'item-0-description': 'Steel Bar', 'item-0-unit': 'KGS',
            'action': 'save_draft',
        }
        data.update(overrides)
        return data

    def test_save_draft_creates_no_quotation_number_and_sends_no_email(self):
        self.client.force_login(self.staff)
        response = self.client.post(reverse('quotation_form'), self._draft_item_data(**{
            'customer_name': 'Draft Co',
        }), follow=True)
        quotation = Quotation.objects.get(customer__name='Draft Co')
        self.assertEqual(quotation.status, 'draft')
        self.assertIsNone(quotation.quotation_no)
        self.assertEqual(quotation.formatted_no(), 'DRAFT')
        self.assertEqual(len(mail.outbox), 0)
        self.assertContains(response, "Draft saved for Draft Co")

    def test_draft_allows_incomplete_line_item(self):
        """A row with a description but no quantity/rate yet still saves —
        that's the whole point of a draft."""
        self.client.force_login(self.staff)
        self.client.post(reverse('quotation_form'), self._draft_item_data(**{
            'customer_name': 'Incomplete Item Co',
        }))
        item = Quotation.objects.get(customer__name='Incomplete Item Co').line_items.get()
        self.assertEqual(item.description, 'Steel Bar')
        self.assertIsNone(item.quantity)
        self.assertIsNone(item.rate_per_kg)

    def test_draft_allows_zero_line_items(self):
        """A totally blank item row (no description) is just dropped, not
        rejected — a draft can exist with nothing filled in on it yet."""
        self.client.force_login(self.staff)
        data = self._draft_item_data(**{'customer_name': 'Blank Item Co', 'item-0-description': ''})
        self.client.post(reverse('quotation_form'), data)
        quotation = Quotation.objects.get(customer__name='Blank Item Co')
        self.assertEqual(quotation.line_items.count(), 0)

    def test_save_draft_still_requires_company_name(self):
        self.client.force_login(self.staff)
        response = self.client.post(reverse('quotation_form'), self._draft_item_data(), follow=True)
        self.assertContains(response, "Company name is required")
        self.assertEqual(Quotation.objects.count(), 0)

    def test_draft_from_query_does_not_flip_query_status(self):
        """A draft hasn't gone out to anyone — the Query must stay 'new'
        until the quotation is actually sent, not just saved as a draft."""
        query = Query.objects.create(source='call', company_name='Draft Query Co', contact_email='q@example.com')
        self.client.force_login(self.staff)
        self.client.post(f"{reverse('quotation_form')}?query={query.pk}", self._draft_item_data())
        query.refresh_from_db()
        self.assertEqual(query.status, 'new')
        self.assertIsNone(query.customer)

    def test_resume_draft_get_prefills_from_saved_data(self):
        self.client.force_login(self.staff)
        self.client.post(reverse('quotation_form'), self._draft_item_data(**{
            'customer_name': 'Resume Co', 'item-0-quantity': '250', 'item-0-rate_per_kg': '99.50',
        }))
        draft = Quotation.objects.get(customer__name='Resume Co')
        response = self.client.get(reverse('quotation_edit', kwargs={'pk': draft.pk}))
        self.assertContains(response, 'Edit Draft Quotation')
        self.assertContains(response, 'value="250.000"')
        self.assertContains(response, 'value="99.50"')
        self.assertContains(response, 'Resume Co')

    def test_saving_a_resumed_draft_again_updates_in_place(self):
        self.client.force_login(self.staff)
        self.client.post(reverse('quotation_form'), self._draft_item_data(**{'customer_name': 'Update Co'}))
        draft = Quotation.objects.get(customer__name='Update Co')

        self.client.post(reverse('quotation_edit', kwargs={'pk': draft.pk}), self._draft_item_data(**{
            'item-0-description': 'Updated Description',
        }))
        self.assertEqual(Quotation.objects.filter(customer__name='Update Co').count(), 1)
        draft.refresh_from_db()
        self.assertEqual(draft.line_items.get().description, 'Updated Description')
        self.assertEqual(draft.status, 'draft')
        self.assertIsNone(draft.quotation_no)

    @override_settings(EMAIL_HOST_USER='sender@example.com', DEFAULT_FROM_EMAIL='sender@example.com')
    def test_sending_a_resumed_draft_finalizes_it_in_place(self):
        self.client.force_login(self.staff)
        self.client.post(reverse('quotation_form'), self._draft_item_data(**{'customer_name': 'Finalize Co'}))
        draft = Quotation.objects.get(customer__name='Finalize Co')

        self.client.post(reverse('quotation_edit', kwargs={'pk': draft.pk}), quotation_item_post_data(**{
            'customer_name': 'Finalize Co', 'customer_email': 'finalize@example.com', 'action': 'send',
        }))
        draft.refresh_from_db()
        self.assertEqual(draft.status, 'sent')
        self.assertIsNotNone(draft.quotation_no)
        self.assertEqual(Quotation.objects.filter(customer__name='Finalize Co').count(), 1)
        self.assertEqual(len(mail.outbox), 1)

    def test_finalizing_a_draft_from_a_query_flips_query_status(self):
        query = Query.objects.create(source='call', company_name='Finalize Query Co', contact_email='fq@example.com')
        self.client.force_login(self.staff)
        self.client.post(f"{reverse('quotation_form')}?query={query.pk}", self._draft_item_data())
        draft = Quotation.objects.get(source_query=query)

        self.client.post(reverse('quotation_edit', kwargs={'pk': draft.pk}), quotation_item_post_data(**{
            'action': 'send',
        }))
        query.refresh_from_db()
        self.assertEqual(query.status, 'quote_sent')
        self.assertIsNotNone(query.customer)

    def test_cannot_edit_an_already_sent_quotation(self):
        customer = Customer.objects.create(name='Sent Already Co')
        sent = Quotation.objects.create(customer=customer, status='sent')
        self.client.force_login(self.staff)
        response = self.client.get(reverse('quotation_edit', kwargs={'pk': sent.pk}))
        self.assertEqual(response.status_code, 404)

    def test_discarding_a_draft_deletes_it(self):
        self.client.force_login(self.staff)
        self.client.post(reverse('quotation_form'), self._draft_item_data(**{'customer_name': 'Discard Co'}))
        draft = Quotation.objects.get(customer__name='Discard Co')

        self.client.post(reverse('quotation_discard', kwargs={'pk': draft.pk}), follow=True)
        self.assertFalse(Quotation.objects.filter(pk=draft.pk).exists())

    def test_anonymous_cannot_discard_a_draft(self):
        customer = Customer.objects.create(name='Guard Draft Co')
        draft = Quotation.objects.create(customer=customer, status='draft')
        response = self.client.post(reverse('quotation_discard', kwargs={'pk': draft.pk}))
        self.assertRedirects(response, reverse('home'))
        self.assertTrue(Quotation.objects.filter(pk=draft.pk).exists())

    def test_discarded_drafts_leave_no_gap_in_quotation_numbering(self):
        """Drafts never consume a quotation_no in the first place, so
        discarding one can't leave a gap — this just confirms the first
        real Send still lands on QUO-0001, not QUO-0003, after two drafts
        were created and discarded first."""
        self.client.force_login(self.staff)
        for i in range(2):
            self.client.post(reverse('quotation_form'), self._draft_item_data(**{'customer_name': f'Scratch Co {i}'}))
        for draft in Quotation.objects.filter(status='draft'):
            self.client.post(reverse('quotation_discard', kwargs={'pk': draft.pk}))

        self.client.post(reverse('quotation_form'), quotation_item_post_data(**{
            'customer_name': 'Real Co', 'action': 'send',
        }))
        quotation = Quotation.objects.get(customer__name='Real Co')
        self.assertEqual(quotation.quotation_no, 1)

    def test_anonymous_cannot_view_drafts_list(self):
        response = self.client.get(reverse('quotation_drafts'))
        self.assertRedirects(response, reverse('home'))

    def test_drafts_list_shows_drafts_with_resume_and_discard(self):
        self.client.force_login(self.staff)
        self.client.post(reverse('quotation_form'), self._draft_item_data(**{'customer_name': 'Listed Co'}))
        draft = Quotation.objects.get(customer__name='Listed Co')

        response = self.client.get(reverse('quotation_drafts'))
        self.assertContains(response, 'Listed Co')
        self.assertContains(response, reverse('quotation_edit', kwargs={'pk': draft.pk}))

    def test_sent_quotations_do_not_appear_in_drafts_list(self):
        self.client.force_login(self.staff)
        # customer_email set so no "No email on file" warning gets queued
        # in the session — that message would otherwise surface on the
        # *next* page rendering messages (the drafts list, right below),
        # not the send action's own redirect target, and confuse this
        # assertion about something unrelated to draft filtering.
        self.client.post(reverse('quotation_form'), quotation_item_post_data(**{
            'customer_name': 'Sent Not Draft Co', 'customer_email': 'sent@example.com', 'action': 'send',
        }))
        response = self.client.get(reverse('quotation_drafts'))
        self.assertNotContains(response, 'Sent Not Draft Co')


class QuotationPdfTests(TestCase):
    def _make_quotation(self, customer, rate_per_kg, grade='', size=None):
        quotation = Quotation.objects.create(customer=customer)
        QuotationLineItem.objects.create(
            quotation=quotation, order=1, description=grade or 'Item',
            grade=grade, size=size, quantity=Decimal('1'), unit='KGS', rate_per_kg=rate_per_kg,
        )
        return quotation

    def test_generate_quotation_pdf_returns_a_real_pdf(self):
        customer = Customer.objects.create(name='PDF Test Co', email='pdf@example.com')
        quotation = self._make_quotation(customer, Decimal('99.99'), grade='EN8D', size=Decimal('1.200'))
        pdf_bytes = generate_quotation_pdf(quotation)
        self.assertTrue(pdf_bytes.startswith(b'%PDF'))
        self.assertGreater(len(pdf_bytes), 0)

    def test_anonymous_cannot_download_quotation_pdf(self):
        customer = Customer.objects.create(name='PDF Guard Co')
        quotation = self._make_quotation(customer, Decimal('50'))
        response = self.client.get(reverse('quotation_pdf', kwargs={'pk': quotation.pk}))
        self.assertRedirects(response, reverse('home'))

    def test_staff_can_download_quotation_pdf(self):
        staff = User.objects.create_user('pdf_staff', password='pw', is_staff=True)
        customer = Customer.objects.create(name='PDF Download Co')
        quotation = self._make_quotation(customer, Decimal('50'))
        self.client.force_login(staff)
        response = self.client.get(reverse('quotation_pdf', kwargs={'pk': quotation.pk}))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'application/pdf')
        self.assertTrue(response.content.startswith(b'%PDF'))

    def test_generate_quotation_pdf_survives_markup_like_text(self):
        """Company/grade text can come from a raw WhatsApp reply with no
        HTML-safety check — reportlab's Paragraph parses a real XML-like
        markup subset, so unescaped text containing '<', '>' or '&' used to
        raise a parse error inside doc.build() and silently kill the quote
        email (or 500 the staff PDF-download endpoint)."""
        customer = Customer.objects.create(name='<b>Evil & Co</b>', email='evil@example.com', phone='999')
        quotation = self._make_quotation(customer, Decimal('50'), grade='<Foo & </para> Bar')
        pdf_bytes = generate_quotation_pdf(quotation)
        self.assertTrue(pdf_bytes.startswith(b'%PDF'))


class QuotationPdfSignatureTests(TestCase):
    """The PDF is generated by the system and sent as is: no signature block,
    just a note saying none is needed."""

    def setUp(self):
        self.customer = Customer.objects.create(name='Pdf Co')
        self.quotation = Quotation.objects.create(customer=self.customer, status='sent')
        QuotationLineItem.objects.create(quotation=self.quotation, order=1, description='Bar', quantity=10, rate_per_kg=90)

    def _pdf_text(self):
        import re
        from unittest.mock import patch
        with patch('reportlab.rl_config.pageCompression', 0):  # readable content streams
            data = generate_quotation_pdf(self.quotation)
        return b' '.join(re.findall(rb'\((.*?)\)\s*Tj', data)).decode('latin-1')

    def test_pdf_says_it_is_computer_generated_and_needs_no_signature(self):
        text = self._pdf_text()
        self.assertIn('This is a computer-generated quotation and does not require an authorised signature.', text)

    def test_pdf_has_no_signature_block(self):
        text = self._pdf_text()
        self.assertNotIn('Signatory', text)
        self.assertNotIn('Authorized', text)

    def test_pdf_date_is_the_day_it_was_sent_not_when_the_draft_was_started(self):
        import datetime
        from django.utils import timezone
        sent = timezone.make_aware(datetime.datetime(2026, 3, 14, 10, 30))
        Quotation.objects.filter(pk=self.quotation.pk).update(sent_at=sent)
        self.quotation.refresh_from_db()
        self.assertIn('Date: 14/03/2026', self._pdf_text())


class ProductCodeMatchingTests(TestCase):
    """A product code depends on product type + grade + size, and the quote
    maker matches it — the owner doesn't choose a code."""

    def setUp(self):
        self.staff = User.objects.create_user('match_staff', password='pw', is_staff=True)
        self.client.force_login(self.staff)
        self.customer = Customer.objects.create(name='Match Co', email='match@example.com')
        self.round_bar = ProductCategory.objects.get(name='Round Bright Bar')
        self.hex_bar = ProductCategory.objects.get(name='Hexagonal Bright Bar')
        self.flat_wire = ProductCategory.objects.get(name='Flat Wire')
        self.round_code = ProductType.objects.create(item_code='RB-EN8D-12', category=self.round_bar, grade='EN8D', size='12.000')
        self.hex_code = ProductType.objects.create(item_code='HB-EN8D-12', category=self.hex_bar, grade='EN8D', size='12.000')
        self.wire_code = ProductType.objects.create(item_code='FW-SS304-2', category=self.flat_wire, grade='SS304', size='2.000')

    def _send(self, action='send', **item):
        data = quotation_item_post_data(**{f'item-0-{k}': v for k, v in item.items()}, **{'action': action})
        with patch('materials.views.quotations._dispatch_quote_email'):
            self.client.post(f"{reverse('quotation_form')}?customer={self.customer.pk}", data)
        return QuotationLineItem.objects.order_by('-pk').first()

    def test_the_code_is_matched_from_type_grade_and_size(self):
        self.assertEqual(self._send(category=str(self.round_bar.pk), grade='EN8D', size='12').product_type, self.round_code)

    def test_the_same_grade_and_size_gives_a_different_code_for_a_different_type(self):
        line = self._send(category=str(self.hex_bar.pk), grade='EN8D', size='12')
        self.assertEqual(line.product_type, self.hex_code)
        self.assertEqual(line.category, self.hex_bar)

    def test_without_a_type_an_ambiguous_grade_and_size_matches_nothing(self):
        self.assertIsNone(self._send(grade='EN8D', size='12').product_type)

    def test_without_a_type_a_grade_and_size_only_one_code_has_still_matches(self):
        self.assertEqual(self._send(grade='ss304', size='2').product_type, self.wire_code)

    def test_sending_for_a_type_grade_and_size_with_no_code_is_refused_not_created(self):
        before = ProductType.objects.count()
        self._send(category=str(self.flat_wire.pk), grade='EN8D', size='12')
        self.assertEqual(ProductType.objects.count(), before)   # quotes never create codes
        self.assertFalse(QuotationLineItem.objects.exists())   # and the send was refused

    def test_saving_a_draft_for_such_a_combination_leaves_the_code_unassigned(self):
        line = self._send(action='save_draft', category=str(self.flat_wire.pk), grade='EN8D', size='12')
        self.assertIsNone(line.product_type)

    def test_a_code_picked_by_hand_is_never_overridden(self):
        line = self._send(category=str(self.round_bar.pk), grade='EN8D', size='12', product_type=str(self.wire_code.pk))
        self.assertEqual(line.product_type, self.wire_code)

    def test_save_draft_matches_the_code_too(self):
        line = self._send(action='save_draft', category=str(self.hex_bar.pk), grade='EN8D', size='12')
        self.assertEqual(line.product_type, self.hex_code)

    def test_grade_size_and_quantity_the_bot_collected_are_in_the_quote_form(self):
        query = Query.objects.create(source='whatsapp', contact_phone='9123456780', company_name='Bot Co',
                                     product_category=self.hex_bar, grade='EN-8D', size='12.000', quantity='8000')
        response = self.client.get(f"{reverse('quotation_form')}?query={query.pk}")
        initial = response.context['formset'].forms[0].initial
        self.assertEqual(initial['grade'], 'EN-8D')
        self.assertEqual(initial['size'], Decimal('12.000'))
        self.assertEqual(initial['quantity'], Decimal('8000'))
        html = response.content.decode()
        self.assertIn('value="EN-8D"', html)
        self.assertIn('value="12.000"', html)
        self.assertIn('value="8000.000"', html)
        detail = self.client.get(reverse('query_detail', kwargs={'pk': query.pk})).content.decode()
        self.assertIn('12.000 mm', detail)
        self.assertIn('8000', detail)

    def test_the_form_prefills_the_product_type_from_the_query_and_the_owner_fills_the_size(self):
        query = Query.objects.create(source='indiamart', contact_phone='9123456780', company_name='Match Co',
                                     product_category=self.hex_bar, grade='EN8D')
        response = self.client.get(f"{reverse('quotation_form')}?query={query.pk}")
        self.assertEqual(response.context['formset'].forms[0].initial['category'], self.hex_bar.pk)
        html = response.content.decode()
        self.assertRegex(html, rf'<option value="{self.hex_bar.pk}"\s+selected>Hexagonal Bright Bar</option>')

    def test_the_form_embeds_the_type_grade_size_map_for_live_matching(self):
        response = self.client.get(reverse('quotation_form'))
        by_pk = {entry['pk']: entry for entry in response.context['product_code_map']}
        self.assertEqual(by_pk[self.hex_code.pk], {'pk': self.hex_code.pk, 'category': self.hex_bar.pk, 'grade': 'en8d', 'size': '12.000'})

    def test_the_form_labels_say_product_type_and_product_code(self):
        html = self.client.get(reverse('quotation_form')).content.decode()
        self.assertIn('<label>Product Type</label>', html)
        self.assertIn('<label>Product Code</label>', html)


class QuotationHeaderValuesTests(TestCase):
    """The header fields ("More details") must show what was saved or prefilled -
    the page used to read them from an empty dict and show every one blank."""

    def setUp(self):
        self.staff = User.objects.create_user('header_staff', password='pw', is_staff=True)
        self.client.force_login(self.staff)
        self.customer = Customer.objects.create(name='Header Co', email='h@example.com')

    def _html(self, url):
        return self.client.get(url).content.decode()

    def test_resuming_a_draft_shows_its_saved_header_values(self):
        import datetime
        draft = Quotation.objects.create(
            customer=self.customer, status='draft', ref_no='REF-77', rev_no=2, rev_date=datetime.date(2026, 10, 7),
            sales_person='Sam', kind_attn='Mr Rao', subject='Chamfer', customer_address='12 Draft Road',
            customer_gstin='22AAAAA0000A1Z5', freight_amount=150, payment_terms='50% advance',
            same_state_as_us=False,
        )
        html = self._html(reverse('quotation_edit', kwargs={'pk': draft.pk}))
        for fragment in ('value="REF-77"', 'value="2"', 'value="2026-10-07"', 'value="Sam"', 'value="Mr Rao"',
                         'value="Chamfer"', '12 Draft Road</textarea>', 'value="22AAAAA0000A1Z5"',
                         'value="150.00"', 'value="50% advance"'):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, html)
        self.assertNotRegex(html, r'id="same_state_as_us"[^>]*checked')   # saved as out-of-state

    def test_a_new_quotation_starts_in_state_with_the_standard_terms(self):
        html = self._html(reverse('quotation_form'))
        self.assertRegex(html, r'id="same_state_as_us"[^>]*checked')
        self.assertIn('value="100% Advance"', html)

    def test_a_returning_customer_gets_the_address_and_gstin_from_their_last_quote(self):
        Quotation.objects.create(customer=self.customer, status='sent', customer_address='7 Repeat Lane', customer_gstin='27BBBBB1111B1Z6')
        html = self._html(f"{reverse('quotation_form')}?customer={self.customer.pk}")
        self.assertIn('7 Repeat Lane</textarea>', html)
        self.assertIn('value="27BBBBB1111B1Z6"', html)

    def test_a_resent_quote_shows_the_prefilled_revision_on_the_page(self):
        import datetime
        from django.utils import timezone
        query = Query.objects.create(source='indiamart', contact_phone='9123456780', company_name='Header Co')
        Quotation.objects.create(customer=self.customer, source_query=query, status='sent')
        html = self._html(f"{reverse('quotation_form')}?query={query.pk}")
        self.assertIn('name="rev_no" value="1"', html)
        self.assertIn(f'name="rev_date" value="{timezone.localdate().isoformat()}"', html)
        self.assertIsInstance(timezone.localdate(), datetime.date)

    def _send(self, **header):
        with patch('materials.views.quotations._dispatch_quote_email'):
            return self.client.post(f"{reverse('quotation_form')}?customer={self.customer.pk}",
                                    quotation_item_post_data(**{'action': 'send', **header}))

    def test_the_customer_gstin_is_normalised_and_saved(self):
        self._send(customer_gstin=' 22 aaaaa 0000 a1z5 ', customer_address='1 Road')
        quotation = Quotation.objects.get(customer=self.customer)
        self.assertEqual((quotation.customer_gstin, quotation.customer_address), ('22AAAAA0000A1Z5', '1 Road'))

    def test_an_invalid_gstin_is_rejected_and_nothing_is_sent(self):
        response = self._send(customer_gstin='NOTAGSTIN')
        self.assertContains(response, 'valid 15-character GST number')
        self.assertFalse(Quotation.objects.filter(customer=self.customer).exists())

    def test_the_gstin_is_optional(self):
        self._send(customer_gstin='')
        self.assertEqual(Quotation.objects.get(customer=self.customer).customer_gstin, '')


class QuotationPdfCustomerDetailsTests(TestCase):
    def _text(self, **fields):
        import re
        customer = Customer.objects.create(name='Pdf Details Co')
        quotation = Quotation.objects.create(customer=customer, status='sent', **fields)
        QuotationLineItem.objects.create(quotation=quotation, order=1, description='Bar', quantity=10, rate_per_kg=90)
        with patch('reportlab.rl_config.pageCompression', 0):
            data = generate_quotation_pdf(quotation)
        return b' '.join(re.findall(rb'\((.*?)\)\s*Tj', data)).decode('latin-1')

    def test_the_customers_address_and_gstin_print_in_the_quotation_to_box(self):
        text = self._text(customer_address='12 Industrial Area', customer_gstin='22AAAAA0000A1Z5')
        self.assertIn('12 Industrial Area', text)
        self.assertIn('22AAAAA0000A1Z5', text)

    def test_no_gstin_row_when_there_isnt_one(self):
        text = self._text(customer_address='12 Industrial Area')
        self.assertIn('12 Industrial Area', text)
        self.assertNotIn('22AAAAA0000A1Z5', text)

    def test_both_boxes_list_their_rows_in_the_same_order(self):
        from django.test import override_settings
        with override_settings(COMPANY_ADDRESS='1 Our Road', COMPANY_GST='06OURGST0000A1Z5', COMPANY_EMAIL='us@example.com',
                               COMPANY_PHONE='111-OURS', COMPANY_WEBSITE='ours.example'):
            text = self._text(customer_address='2 Their Road', customer_gstin='22AAAAA0000A1Z5')
        # "Our" box comes first on the page, then theirs; each must run address -> GSTIN -> email -> phone.
        ours, theirs = text.split('Quotation to', 1)
        self.assertLess(ours.index('1 Our Road'), ours.index('06OURGST0000A1Z5'))
        self.assertLess(ours.index('06OURGST0000A1Z5'), ours.index('us@example.com'))
        self.assertLess(ours.index('us@example.com'), ours.index('111-OURS'))
        self.assertLess(ours.index('111-OURS'), ours.index('ours.example'))
        self.assertLess(theirs.index('2 Their Road'), theirs.index('22AAAAA0000A1Z5'))

    def test_the_customer_name_prints_without_an_ms_prefix(self):
        text = self._text(customer_address='12 Industrial Area')
        self.assertNotIn('M/s', text)
        self.assertIn('Pdf Details Co', text)
