"""The company portal: one permanent link per company, its orders, Order again, per-order tracking."""

import datetime
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from ..models import CompanyChangeRequest, Customer, Order, ProductCode, ProductType, Quotation, QuotationLineItem


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
        self.assertEqual(self.client.post(url, {'quantity': '5'}).status_code, 404)
        self.assertEqual(Order.objects.filter(customer=self.acme).count(), 2)

    def test_order_again_copies_everything_but_the_quantity_and_date(self):
        t = ProductType.objects.get_or_create(name='Flat Bright Bar', defaults={'code': 'FBX'})[0]
        code = ProductCode.objects.create(product_type=t, grade='EN8D')
        self.done.product_code, self.done.product_type = code, t
        self.done.width_tol_from, self.done.mill_make, self.done.delivery_form = Decimal('49.95'), 'SAIL', 'bar'
        self.done.bar_length = Decimal('3000')
        self.done.save()
        url = reverse('portal_reorder', args=[self.acme.portal_token, self.done.pk])
        self.assertContains(self.client.get(url), 'Flat Bright Bar')
        tomorrow = datetime.date.today() + datetime.timedelta(days=1)
        response = self.client.post(url, {'quantity': '250', 'delivery_date': tomorrow.isoformat()})
        self.assertContains(response, 'Order received')
        new = Order.objects.exclude(pk__in=[self.done.pk, self.live.pk, self.theirs.pk]).get()
        self.assertEqual((new.status, new.quantity, new.delivery_date), ('pending', Decimal('250'), tomorrow))
        self.assertEqual((new.customer, new.reordered_from, new.product_code, new.product_type), (self.acme, self.done, code, t))
        self.assertEqual((new.grade, new.width, new.mill_make, new.delivery_form, new.bar_length, new.width_tol_from),
                         ('EN8D', Decimal('50'), 'SAIL', 'bar', Decimal('3000'), Decimal('49.95')))
        self.assertFalse(new.customer_can_track)

    def test_a_bad_quantity_or_past_date_places_nothing(self):
        url = reverse('portal_reorder', args=[self.acme.portal_token, self.done.pk])
        yesterday = datetime.date.today() - datetime.timedelta(days=1)
        for data in ({'quantity': '0'}, {'quantity': ''}, {'quantity': '5', 'delivery_date': yesterday.isoformat()}):
            self.assertEqual(self.client.post(url, data).status_code, 200)
        self.assertEqual(Order.objects.filter(customer=self.acme).count(), 2)

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
        QuotationLineItem.objects.create(quotation=quote, description='Flat bar', grade='EN8D', width=Decimal('50'),
                                         thickness=Decimal('6'), quantity=Decimal('100'), rate_per_kg=Decimal('85'), discount_pct=Decimal('5'))
        self.quote = quote
        self.priced = make_order(self.acme, status='in_production')                      # matches the quoted line
        self.unpriced = make_order(self.acme, status='completed', grade='EN9')           # nothing quoted for it
        self.url = reverse('company_detail', args=[self.acme.pk])

    def test_the_page_shows_details_current_and_earlier_orders_and_the_quote(self):
        page = self.client.get(self.url)
        for text in ('ACME', 'a@acme.co', '27ABCDE1234F1Z5', '12 Mill Road', 'Current orders', 'Earlier orders',
                     'Quotations sent', self.quote.formatted_no(), '₹ 85'):
            self.assertContains(page, text)
        self.assertEqual([r['order'] for r in page.context['current']], [self.priced])
        self.assertEqual([r['order'] for r in page.context['past']], [self.unpriced])

    def test_each_order_shows_the_rate_quoted_for_its_size_and_a_dash_when_none_matches(self):
        page = self.client.get(self.url)
        priced, unpriced = page.context['current'][0], page.context['past'][0]
        self.assertEqual(priced['line'].rate_per_kg, Decimal('85'))
        self.assertIsNone(unpriced['line'])

    def test_a_draft_quotation_is_not_shown_and_the_companies_list_links_here(self):
        Quotation.objects.create(customer=self.acme, status='draft')
        self.assertEqual(len(self.client.get(self.url).context['quotations']), 1)
        self.assertContains(self.client.get(reverse('company_dashboard')), self.url)

    def test_only_staff_can_open_it(self):
        self.client.logout()
        self.assertEqual(self.client.get(self.url).status_code, 302)
