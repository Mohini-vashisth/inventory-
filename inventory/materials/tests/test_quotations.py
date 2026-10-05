"""Quotations: the Send Quote form, drafts, and the PDF."""

from decimal import Decimal
from django.contrib.auth.models import User
from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse

from ..models import Customer, Query, Quotation, QuotationLineItem
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
