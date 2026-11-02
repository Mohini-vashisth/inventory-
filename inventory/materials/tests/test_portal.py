"""The company portal: one permanent link per company, its orders, Order again, per-order tracking."""

import datetime
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from ..models import Customer, Order, ProductCode, ProductType


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
        page = self.client.get(reverse('order_dashboard'))
        self.assertContains(page, reverse('portal_home', args=[self.acme.portal_token]))
        self.client.post(reverse('order_detail', args=[self.order.pk]), {'quantity': '100', 'customer_can_track': 'on'})
        self.order.refresh_from_db()
        self.assertTrue(self.order.customer_can_track)
