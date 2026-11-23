"""The company portal: one permanent link per company, its orders, Order again, per-order tracking."""

from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from ..models import CompanyChangeRequest, Customer, Order, ProductCode, ProductType, Query, QueryItem, Quotation, QuotationLineItem


def make_order(customer, **extra):
    data = dict(quantity=Decimal('100'), status='completed', grade='EN8D', width=Decimal('50'), thickness=Decimal('6'))
    data.update(extra)
    return Order.objects.create(customer=customer, **data)


class PortalTests(TestCase):
    def setUp(self):
        self.acme = Customer.objects.create(name='ACME')
        self.other = Customer.objects.create(name='OTHER CO')
        self.done = make_order(self.acme)
        self.live = make_order(self.acme, status='in_production', grade='EN9')
        self.theirs = make_order(self.other, grade='SECRET1')
        self.home = reverse('portal_home', args=[self.acme.portal_token])

    def test_home_lists_current_and_past_orders_of_that_company_only(self):
        page = self.client.get(self.home)
        self.assertContains(page, 'ACME')
        self.assertContains(page, 'EN8D')     # the past order
        self.assertContains(page, 'EN9')      # the current one
        self.assertNotContains(page, 'SECRET1')
        self.assertContains(page, 'Order again')

    def test_a_company_without_orders_or_an_unknown_link_is_not_found(self):
        empty = Customer.objects.create(name='NEW CO')
        self.assertEqual(self.client.get(reverse('portal_home', args=[empty.portal_token])).status_code, 404)
        self.assertEqual(self.client.get(reverse('portal_home', args=['00000000-0000-4000-8000-000000000000'])).status_code, 404)

    def test_status_shows_only_where_tracking_is_on(self):
        self.assertNotContains(self.client.get(self.home), 'In production')
        self.live.customer_can_track = True
        self.live.save()
        self.assertContains(self.client.get(self.home), 'In production')

    def test_another_companys_order_cannot_be_repeated_through_this_link(self):
        url = reverse('portal_reorder', args=[self.acme.portal_token, self.theirs.pk])
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.client.post(url).status_code, 404)
        self.assertFalse(Query.objects.exists())

    def test_order_again_opens_a_reorder_query_with_the_product_and_places_no_order(self):
        t = ProductType.objects.get_or_create(name='Flat Bright Bar', defaults={'code': 'FBX'})[0]
        code = ProductCode.objects.create(product_type=t, grade='EN8D')
        self.done.product_code, self.done.product_type, self.done.mill_make, self.done.delivery_form = code, t, 'SAIL', 'bar'
        self.done.save()
        self.acme.phone, self.acme.gst_number = '919876543210', '27ABCDE1234F1Z5'
        self.acme.save()
        url = reverse('portal_reorder', args=[self.acme.portal_token, self.done.pk])
        self.assertContains(self.client.get(url), 'Flat Bright Bar')
        self.assertContains(self.client.post(url), 'Request received')
        self.assertEqual(Order.objects.filter(customer=self.acme).count(), 2)   # no new order
        query = Query.objects.get()
        self.assertEqual((query.source, query.status, query.customer, query.company_name, query.gst_number, query.contact_phone),
                         ('reorder', 'new', self.acme, 'ACME', '27ABCDE1234F1Z5', '919876543210'))
        self.assertIn('Reorder of ORD-', query.notes)
        item = query.items.get()
        self.assertEqual((item.product_type, item.product_code, item.grade, item.width, item.thickness, item.delivery_form, item.quantity),
                         (t, code, 'EN8D', Decimal('50'), Decimal('6'), 'Bar', None))
        self.assertIn('SAIL', item.technical_requirements)

    def test_asking_again_for_the_same_order_reuses_the_waiting_request(self):
        url = reverse('portal_reorder', args=[self.acme.portal_token, self.done.pk])
        self.client.post(url)
        self.client.post(url)
        self.assertEqual(Query.objects.count(), 1)

    def test_a_cancelled_order_cannot_be_repeated(self):
        gone = make_order(self.acme, status='cancelled')
        self.assertEqual(self.client.get(reverse('portal_reorder', args=[self.acme.portal_token, gone.pk])).status_code, 404)

    def test_manifest_service_worker_and_icons_make_it_installable(self):
        manifest = self.client.get(reverse('portal_manifest', args=[self.acme.portal_token]))
        data = manifest.json()
        self.assertEqual((data['display'], data['start_url']), ('standalone', self.home))
        self.assertTrue(all(self.client.get(icon['src']).status_code == 200 for icon in data['icons']))
        self.assertContains(self.client.get(reverse('portal_service_worker')), 'fetch')
        self.assertContains(self.client.get(self.home), 'rel="manifest"')

    def test_the_install_button_is_always_offered_with_instructions_for_browsers_that_cannot_prompt(self):
        page = self.client.get(self.home)
        self.assertContains(page, 'id="install-btn"')
        self.assertContains(page, 'Add to Home Screen')   # the iPhone steps
        self.assertContains(page, 'beforeinstallprompt')   # the one-tap install where the browser allows it


class PortalPageContentTests(TestCase):
    def setUp(self):
        self.acme = Customer.objects.create(name='ACME')
        make_order(self.acme)
        self.home = reverse('portal_home', args=[self.acme.portal_token])

    def test_welcome_heading_company_name_and_footer_details(self):
        with self.settings(COMPANY_NAME='Matta Drawing Works', COMPANY_ADDRESS='Plot 12 MIDC', COMPANY_PHONE='+91 99999 00000',
                           COMPANY_EMAIL='info@example.com', COMPANY_GST='27ABCDE1234F1Z5'):
            page = self.client.get(self.home)
        self.assertContains(page, 'Welcome to Matta Drawing Works')
        for text in ('ACME', 'Plot 12 MIDC', '+91 99999 00000', 'info@example.com', '27ABCDE1234F1Z5'):
            self.assertContains(page, text)

    def test_chat_button_opens_whatsapp_when_a_number_is_set_and_is_hidden_otherwise(self):
        with self.settings(WHATSAPP_CHAT_NUMBER='919876543210'):
            page = self.client.get(self.home)
        self.assertContains(page, 'https://wa.me/919876543210?text=')
        self.assertContains(page, f'Ref%3A%20{self.acme.portal_token.hex[:8]}')
        self.assertContains(page, 'Chat with us')
        with self.settings(WHATSAPP_CHAT_NUMBER=''):
            self.assertNotContains(self.client.get(self.home), 'wa.me')


class PortalPitchAndDocumentsTests(TestCase):
    def setUp(self):
        self.acme = Customer.objects.create(name='ACME')
        self.confirmed = make_order(self.acme, status='confirmed')
        self.pending = make_order(self.acme, status='pending', grade='EN9')
        self.home = reverse('portal_home', args=[self.acme.portal_token])

    def test_the_dashboard_stays_clean_and_links_to_a_separate_about_page(self):
        page = self.client.get(self.home)
        self.assertContains(page, reverse('portal_about', args=[self.acme.portal_token]))   # the hero's About button
        for text in ('What we make', 'Need something else?', 'Call us', 'Email us'):
            self.assertNotContains(page, text)

    def test_the_about_page_shows_the_pitch_facts_and_the_product_types_from_the_app(self):
        with self.settings(COMPANY_ABOUT='Family-owned since long ago.', COMPANY_FACTS=[('1976', 'Established')]):
            page = self.client.get(reverse('portal_about', args=[self.acme.portal_token]))
        for text in ('Family-owned since long ago.', '1976', 'Established', 'Flat Bright Bar'):
            self.assertContains(page, text)
        stranger = Customer.objects.create(name='NEW CO')
        self.assertEqual(self.client.get(reverse('portal_about', args=[stranger.portal_token])).status_code, 404)

    def test_only_confirmed_orders_link_a_summary_pdf(self):
        page = self.client.get(self.home)
        self.assertContains(page, reverse('portal_order_summary', args=[self.acme.portal_token, self.confirmed.pk]))
        self.assertNotContains(page, reverse('portal_order_summary', args=[self.acme.portal_token, self.pending.pk]))

    def test_the_summary_is_a_pdf_for_the_companys_own_confirmed_order_only(self):
        token = self.acme.portal_token
        response = self.client.get(reverse('portal_order_summary', args=[token, self.confirmed.pk]))
        self.assertEqual(response['Content-Type'], 'application/pdf')
        self.assertTrue(response.content.startswith(b'%PDF'))
        self.assertEqual(self.client.get(reverse('portal_order_summary', args=[token, self.pending.pk])).status_code, 404)
        other = Customer.objects.create(name='OTHER CO')
        theirs = make_order(other, status='confirmed')
        self.assertEqual(self.client.get(reverse('portal_order_summary', args=[token, theirs.pk])).status_code, 404)


class PortalStaffControlsTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user('boss', password='pw', is_staff=True)
        self.client.force_login(self.staff)
        self.acme = Customer.objects.create(name='ACME')
        self.order = make_order(self.acme, status='pending')

    def test_resetting_the_link_kills_the_old_one(self):
        old = self.acme.portal_token
        self.client.post(reverse('reset_portal_link', args=[self.acme.pk]))
        self.acme.refresh_from_db()
        self.assertNotEqual(self.acme.portal_token, old)
        self.assertEqual(self.client.get(reverse('portal_home', args=[old])).status_code, 404)
        self.assertEqual(self.client.get(reverse('portal_home', args=[self.acme.portal_token])).status_code, 200)

    def test_only_staff_can_reset(self):
        self.client.logout()
        old = self.acme.portal_token
        self.client.post(reverse('reset_portal_link', args=[self.acme.pk]))
        self.acme.refresh_from_db()
        self.assertEqual(self.acme.portal_token, old)

    def test_company_list_offers_the_portal_link_and_the_tracking_switch_is_on_the_order(self):
        page = self.client.get(reverse('company_dashboard'))
        self.assertContains(page, reverse('portal_home', args=[self.acme.portal_token]))
        self.client.post(reverse('order_detail', args=[self.order.pk]), {'quantity': '100', 'customer_can_track': 'on'})
        self.order.refresh_from_db()
        self.assertTrue(self.order.customer_can_track)


class CompaniesPageTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user('boss', password='pw', is_staff=True)
        self.client.force_login(self.staff)
        self.acme = Customer.objects.create(name='ACME', email='a@acme.co')
        make_order(self.acme)

    def test_the_dashboard_has_a_companies_tile_and_the_page_lists_companies(self):
        self.assertContains(self.client.get(reverse('staff_home')), reverse('company_dashboard'))
        page = self.client.get(reverse('company_dashboard'))
        self.assertContains(page, 'ACME')
        self.assertContains(page, 'Send Form')

    def test_the_orders_page_no_longer_carries_the_company_list(self):
        page = self.client.get(reverse('order_dashboard'))
        self.assertNotContains(page, 'Portal link')
        self.assertNotContains(page, 'Reset</button>')

    def test_actions_from_the_companies_page_come_back_to_it(self):
        back = self.client.post(reverse('reset_portal_link', args=[self.acme.pk]), {'next': 'company_dashboard'})
        self.assertRedirects(back, reverse('company_dashboard'))
        back = self.client.post(reverse('send_order_form'), {'name': 'ACME', 'next': 'company_dashboard'})
        self.assertRedirects(back, reverse('company_dashboard'))
        back = self.client.post(reverse('send_order_form'), {'name': 'ACME'})
        self.assertRedirects(back, reverse('order_dashboard'))

    def test_only_staff_can_open_it(self):
        self.client.logout()
        self.assertEqual(self.client.get(reverse('company_dashboard')).status_code, 302)


class PortalDetailsTests(TestCase):
    GST = '27ABCDE1234F1Z5'

    def setUp(self):
        self.acme = Customer.objects.create(name='ACME', email='old@acme.co', phone='111')
        make_order(self.acme)
        self.url = reverse('portal_details', args=[self.acme.portal_token])
        self.staff = User.objects.create_user('boss2', password='pw', is_staff=True)

    def _post(self, **fields):
        data = dict(name='ACME', email='old@acme.co', phone='111', gst_number='', gst_address='')
        data.update(fields)
        return self.client.post(self.url, data)

    def test_every_portal_page_has_my_details_and_about_buttons(self):
        for name in ('portal_home', 'portal_about', 'portal_details'):
            page = self.client.get(reverse(name, args=[self.acme.portal_token]))
            self.assertContains(page, self.url)
            self.assertContains(page, reverse('portal_about', args=[self.acme.portal_token]))

    def test_the_page_shows_the_details_on_file_and_changes_nothing_by_itself(self):
        page = self.client.get(self.url)
        self.assertContains(page, 'old@acme.co')
        self._post(email='new@acme.co', phone='999')
        self.acme.refresh_from_db()
        self.assertEqual((self.acme.email, self.acme.phone), ('old@acme.co', '111'))

    def test_a_request_stores_only_the_fields_that_differ(self):
        page = self._post(email='new@acme.co', gst_number=self.GST.lower(), name='acme')
        self.assertContains(page, 'We have your request')
        request = CompanyChangeRequest.objects.get()
        self.assertEqual(request.changes, {'email': 'new@acme.co', 'gst_number': self.GST})
        self.assertEqual(request.status, 'pending')

    def test_nothing_changed_makes_no_request(self):
        self.assertContains(self._post(), 'Nothing was changed')
        self.assertFalse(CompanyChangeRequest.objects.exists())

    def test_a_bad_gst_number_or_email_is_refused(self):
        self.assertContains(self._post(gst_number='NOTAGST'), 'valid 15-character GST number')
        self.assertContains(self._post(email='nope'), 'valid email')
        self.assertFalse(CompanyChangeRequest.objects.exists())

    def test_a_new_request_replaces_one_still_waiting(self):
        self._post(phone='222')
        self._post(phone='333')
        self.assertEqual(CompanyChangeRequest.objects.get().changes, {'phone': '333'})

    def test_staff_see_it_and_approving_applies_it(self):
        self._post(name='acme steels', email='new@acme.co')
        self.client.force_login(self.staff)
        page = self.client.get(reverse('company_dashboard'))
        self.assertContains(page, 'Change requests')
        self.assertContains(page, 'new@acme.co')
        change = CompanyChangeRequest.objects.get()
        self.client.post(reverse('change_request_decide', args=[change.pk]), {'decision': 'approve'})
        self.acme.refresh_from_db()
        change.refresh_from_db()
        self.assertEqual((self.acme.name, self.acme.email, change.status), ('ACME STEELS', 'new@acme.co', 'approved'))
        self.assertNotContains(self.client.get(reverse('company_dashboard')), 'Change requests')

    def test_rejecting_changes_nothing(self):
        self._post(email='new@acme.co')
        self.client.force_login(self.staff)
        change = CompanyChangeRequest.objects.get()
        self.client.post(reverse('change_request_decide', args=[change.pk]), {'decision': 'reject'})
        self.acme.refresh_from_db()
        change.refresh_from_db()
        self.assertEqual((self.acme.email, change.status), ('old@acme.co', 'rejected'))

    def test_a_name_already_used_by_another_company_cannot_be_approved(self):
        Customer.objects.create(name='TAKEN CO')
        self._post(name='taken co')
        self.client.force_login(self.staff)
        change = CompanyChangeRequest.objects.get()
        self.client.post(reverse('change_request_decide', args=[change.pk]), {'decision': 'approve'})
        self.acme.refresh_from_db()
        change.refresh_from_db()
        self.assertEqual((self.acme.name, change.status), ('ACME', 'pending'))

    def test_only_staff_can_decide_and_a_stranger_link_is_404(self):
        self._post(email='new@acme.co')
        change = CompanyChangeRequest.objects.get()
        self.client.post(reverse('change_request_decide', args=[change.pk]), {'decision': 'approve'})
        change.refresh_from_db()
        self.assertEqual(change.status, 'pending')
        stranger = Customer.objects.create(name='NEW CO')
        self.assertEqual(self.client.get(reverse('portal_details', args=[stranger.portal_token])).status_code, 404)


class CompanyDetailPageTests(TestCase):
    def setUp(self):
        self.client.force_login(User.objects.create_user('boss3', password='pw', is_staff=True))
        self.acme = Customer.objects.create(name='ACME', email='a@acme.co', phone='111', gst_number='27ABCDE1234F1Z5', gst_address='12 Mill Road')
        quote = Quotation.objects.create(customer=self.acme, status='sent')
        self.line = QuotationLineItem.objects.create(quotation=quote, description='Flat bar', grade='EN8D', width=Decimal('50'),
                                         thickness=Decimal('6'), quantity=Decimal('100'), rate_per_kg=Decimal('85'), discount_pct=Decimal('5'))
        self.quote = quote
        self.priced = make_order(self.acme, status='in_production', quotation_line=self.line)   # priced by the quoted line
        self.unpriced = make_order(self.acme, status='completed', grade='EN9')           # nothing quoted for it
        self.url = reverse('company_detail', args=[self.acme.pk])

    def test_the_page_shows_details_current_and_earlier_orders_and_the_quote(self):
        page = self.client.get(self.url)
        for text in ('ACME', 'a@acme.co', '27ABCDE1234F1Z5', '12 Mill Road', 'Current orders', 'Earlier orders',
                     'Quotations sent', self.quote.formatted_no(), '₹ 85'):
            self.assertContains(page, text)
        self.assertEqual([r['order'] for r in page.context['current']], [self.priced])
        self.assertEqual([r['order'] for r in page.context['past']], [self.unpriced])

    def test_each_order_shows_its_own_price_and_a_dash_when_it_has_none(self):
        page = self.client.get(self.url)
        self.assertEqual(page.context['current'][0]['price'], (Decimal('85.00'), Decimal('5.00')))
        self.assertIsNone(page.context['past'][0]['price'])

    def test_a_draft_quotation_is_not_shown_and_the_companies_list_links_here(self):
        Quotation.objects.create(customer=self.acme, status='draft')
        self.assertEqual(len(self.client.get(self.url).context['quotations']), 1)
        self.assertContains(self.client.get(reverse('company_dashboard')), self.url)

    def test_only_staff_can_open_it(self):
        self.client.logout()
        self.assertEqual(self.client.get(self.url).status_code, 302)


class PortalQueriesTests(TestCase):
    """The portal lists a company's latest queries that have not become an order: with the quotation to open when one
    was sent, otherwise a note that it is coming."""

    def setUp(self):
        self.acme = Customer.objects.create(name='ACME')
        make_order(self.acme)
        self.other = Customer.objects.create(name='OTHER CO')
        make_order(self.other)
        self.home = reverse('portal_home', args=[self.acme.portal_token])

    def query(self, company='ACME', **extra):
        q = Query.objects.create(company_name=company, contact_phone='9999900000', **extra)
        QueryItem.objects.create(query=q, grade='EN8D', width=Decimal('50'), thickness=Decimal('6'), quantity=Decimal('500'))
        return q

    def quote(self, query, customer=None):
        quotation = Quotation.objects.create(customer=customer or self.acme, source_query=query, status='sent')
        QuotationLineItem.objects.create(quotation=quotation, description='Flat bar', quantity=Decimal('500'), rate_per_kg=Decimal('60'), gst_pct=18)
        return quotation

    def test_a_query_with_no_quote_says_it_is_coming(self):
        self.query()
        page = self.client.get(self.home)
        self.assertContains(page, 'Your queries')
        self.assertContains(page, 'EN8D')
        self.assertContains(page, 'Quotation coming soon')
        self.assertNotContains(page, 'Open quotation (PDF)')

    def test_a_query_with_a_sent_quote_links_the_pdf_and_it_opens(self):
        quotation = self.quote(self.query(status='quote_sent'))
        page = self.client.get(self.home)
        url = reverse('portal_quotation_pdf', args=[self.acme.portal_token, quotation.pk])
        self.assertContains(page, url)
        self.assertNotContains(page, 'Quotation coming soon')
        response = self.client.get(url)
        self.assertEqual((response.status_code, response['Content-Type']), (200, 'application/pdf'))

    def test_a_draft_quote_is_not_shown_or_downloadable(self):
        query = self.query()
        draft = Quotation.objects.create(customer=self.acme, source_query=query, status='draft')
        self.assertContains(self.client.get(self.home), 'Quotation coming soon')
        self.assertEqual(self.client.get(reverse('portal_quotation_pdf', args=[self.acme.portal_token, draft.pk])).status_code, 404)

    def test_queries_that_became_orders_or_were_closed_are_left_out(self):
        self.query(status='converted')
        ordered = self.query(status='quote_sent')
        make_order(self.acme, source_query=ordered)
        self.query(status='not_interested')
        page = self.client.get(self.home)
        self.assertContains(page, 'No open queries.')
        self.assertNotContains(page, 'Asked on')

    def test_only_the_newest_few_are_listed(self):
        from ..views.portal import QUERY_LIMIT
        for _ in range(QUERY_LIMIT + 3):
            self.query()
        self.assertContains(self.client.get(self.home), 'Asked on', count=QUERY_LIMIT)

    def test_another_companys_query_and_quote_stay_private(self):
        theirs = self.query(company='OTHER CO')
        quotation = self.quote(theirs, customer=self.other)
        self.assertContains(self.client.get(self.home), 'No open queries.')
        self.assertEqual(self.client.get(reverse('portal_quotation_pdf', args=[self.acme.portal_token, quotation.pk])).status_code, 404)

    def test_a_query_linked_by_customer_after_the_quote_is_found_even_if_the_name_changed(self):
        query = self.query(company='ACME LTD', status='quote_sent', customer=self.acme)
        self.quote(query)
        self.assertContains(self.client.get(self.home), 'Open quotation (PDF)')


class PortalProductsTests(TestCase):
    """Order again is per product, not per order: the portal lists each product the company has ordered once, and
    the button opens a reorder request copied from that product's latest order."""

    def setUp(self):
        self.acme = Customer.objects.create(name='ACME')
        self.token = self.acme.portal_token
        self.home = reverse('portal_home', args=[self.token])
        self.old = make_order(self.acme, grade='EN8D')
        self.new = make_order(self.acme, grade='EN8D')
        self.other = make_order(self.acme, grade='EN9')
        make_order(self.acme, grade='SAE1008', status='cancelled')

    def test_each_product_is_listed_once_with_how_often_it_was_ordered(self):
        page = self.client.get(self.home)
        self.assertContains(page, 'Your products')
        self.assertContains(page, 'ordered 2 times')
        self.assertEqual([p['title'] for p in page.context['products']].count(self.new.spec_text()), 1)

    def test_a_cancelled_only_product_is_not_offered(self):
        self.assertNotIn('SAE1008', ' '.join(p['title'] for p in self.client.get(self.home).context['products']))

    def test_the_order_again_button_is_on_the_product_and_uses_its_latest_order(self):
        page = self.client.get(self.home)
        latest = next(p['latest'] for p in page.context['products'] if p['title'] == self.new.spec_text())
        self.assertEqual(latest.pk, self.new.pk)
        self.assertContains(page, reverse('portal_reorder', args=[self.token, self.new.pk]))
        self.assertNotContains(page, reverse('portal_reorder', args=[self.token, self.old.pk]))

    def test_orders_themselves_no_longer_carry_order_again(self):
        page = self.client.get(self.home)
        self.assertContains(page, 'Order again', count=len(page.context['products']))


class PortalNewQueryTests(TestCase):
    """A company can ask about products straight from its portal, one product or several."""

    def setUp(self):
        self.acme = Customer.objects.create(name='ACME', email='a@acme.co', gst_number='27ABCDE1234F1Z5', gst_address='1 Road')
        make_order(self.acme)
        self.type = ProductType.objects.get(code='FBB')   # the seven types come with the migrations
        self.url = reverse('portal_query_new', args=[self.acme.portal_token])

    def row(self, i, **extra):
        data = {f'item-{i}-product_type': self.type.pk, f'item-{i}-grade': 'en-8d', f'item-{i}-width': '50',
                f'item-{i}-thickness': '6.5', f'item-{i}-quantity': '800', f'item-{i}-delivery_form': 'Coil'}
        data.update({f'item-{i}-{k}': v for k, v in extra.items()})
        return data

    def post(self, rows, **extra):
        data = {'item-TOTAL': str(len(rows))}
        for row in rows:
            data.update(row)
        data.update(extra)
        return self.client.post(self.url, data)

    def test_the_home_page_has_a_new_query_button(self):
        self.assertContains(self.client.get(reverse('portal_home', args=[self.acme.portal_token])), self.url)

    def test_the_form_opens_with_one_product(self):
        page = self.client.get(self.url)
        self.assertContains(page, 'Product type')
        self.assertContains(page, 'Add another product')
        self.assertContains(page, 'name="item-TOTAL"')

    def test_one_product_creates_a_new_query_for_staff(self):
        response = self.post([self.row(0, end_use='Shafts')])
        self.assertContains(response, 'Query received')
        query = Query.objects.get(source='portal')
        self.assertEqual((query.customer, query.company_name, query.status, query.gst_number), (self.acme, 'ACME', 'new', '27ABCDE1234F1Z5'))
        item = query.items.get()
        self.assertEqual((item.product_type, item.grade, item.width, item.thickness, item.quantity, item.delivery_form, item.end_use),
                         (self.type, 'EN8D', Decimal('50'), Decimal('6.5'), Decimal('800'), 'Coil', 'Shafts'))

    def test_several_products_become_several_items_and_a_blank_extra_row_is_dropped(self):
        blank = {f'item-2-{name}': '' for name in ('product_type', 'grade', 'width', 'thickness', 'quantity', 'delivery_form')}
        self.post([self.row(0), self.row(1, grade='en9'), blank])
        self.assertEqual([item.grade for item in Query.objects.get(source='portal').items.order_by('position')], ['EN8D', 'EN9'])

    def test_a_missing_field_saves_nothing_and_shows_the_error(self):
        response = self.post([self.row(0), self.row(1, quantity='')])
        self.assertContains(response, 'This field is required')
        self.assertFalse(Query.objects.filter(source='portal').exists())

    def test_the_first_product_is_required(self):
        response = self.post([{f'item-0-{name}': '' for name in ('product_type', 'grade', 'width', 'thickness', 'quantity')}])
        self.assertContains(response, 'This field is required')
        self.assertFalse(Query.objects.filter(source='portal').exists())

    def test_a_drawing_can_be_attached_but_only_of_an_allowed_type(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        self.post([self.row(0, drawing=SimpleUploadedFile('shaft.pdf', b'%PDF-1.4 x', content_type='application/pdf'))])
        self.assertTrue(Query.objects.get(source='portal').items.get().drawing.name.endswith('.pdf'))
        Query.objects.all().delete()
        response = self.post([self.row(0, drawing=SimpleUploadedFile('virus.exe', b'x'))])
        self.assertContains(response, 'not allowed')
        self.assertFalse(Query.objects.filter(source='portal').exists())

    def test_the_new_query_then_shows_under_your_queries_as_coming_soon(self):
        self.post([self.row(0)])
        page = self.client.get(reverse('portal_home', args=[self.acme.portal_token]))
        self.assertContains(page, 'Quotation coming soon')

    def test_a_link_whose_company_never_ordered_is_not_found(self):
        stranger = Customer.objects.create(name='NEW CO')
        self.assertEqual(self.client.get(reverse('portal_query_new', args=[stranger.portal_token])).status_code, 404)
