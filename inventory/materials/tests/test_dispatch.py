"""Dispatch: the owners plan shipments of orders in production and mark them dispatched; the plant and finance see the
list read-only, and nobody but the owners can change it. No prices anywhere."""

from decimal import Decimal

from django.conf import settings
from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from ..models import Customer, Dispatch, Order


def order(customer, **extra):
    data = dict(quantity=Decimal('1000'), status='in_production', grade='EN8D', width=Decimal('50'), thickness=Decimal('6'),
                rate_per_kg=Decimal('98765'))
    data.update(extra)
    return Order.objects.create(customer=customer, **data)


class DispatchTilesTests(TestCase):
    def test_the_owners_dashboard_has_a_dispatch_planning_tile_and_page(self):
        self.client.force_login(User.objects.create_user('boss', password='pw', is_staff=True))
        home = self.client.get(reverse('staff_home'))
        self.assertContains(home, 'Dispatch planning')
        self.assertContains(home, reverse('dispatch_planning'))
        self.assertContains(self.client.get(reverse('dispatch_planning')), reverse('staff_home'))   # the back link

    def test_the_employee_portal_has_a_dispatch_tile_and_page(self):
        self.client.post(reverse('login'), {'pin': settings.EMPLOYEE_PIN})
        self.assertContains(self.client.get(reverse('employee')), reverse('employee_dispatch'))
        self.assertContains(self.client.get(reverse('employee_dispatch')), reverse('employee'))

    def test_the_finance_page_has_a_dispatch_link_and_page(self):
        self.client.post(reverse('login'), {'pin': settings.FINANCE_CODE})
        self.assertContains(self.client.get(reverse('finance')), reverse('finance_dispatch'))
        self.assertContains(self.client.get(reverse('finance_dispatch')), reverse('finance'))

    def test_each_page_opens_only_for_its_own_role(self):
        self.client.post(reverse('login'), {'pin': settings.EMPLOYEE_PIN})
        for name in ('dispatch_planning', 'finance_dispatch'):
            self.assertEqual(self.client.get(reverse(name)).status_code, 302, name)
        self.client.logout()
        self.client.post(reverse('login'), {'pin': settings.FINANCE_CODE})
        for name in ('dispatch_planning', 'employee_dispatch'):
            self.assertEqual(self.client.get(reverse(name)).status_code, 302, name)


class DispatchPlanningTests(TestCase):
    def setUp(self):
        self.client.force_login(User.objects.create_user('boss', password='pw', is_staff=True))
        self.acme = Customer.objects.create(name='ACME')
        self.live = order(self.acme)
        self.pending = order(self.acme, status='pending')
        self.done = order(self.acme, status='completed')

    def plan(self, order_obj=None, **extra):
        order_obj = order_obj or self.live
        data = {'planned_date': '2026-12-01', 'weight': '400', 'vehicle_no': 'dl 1c 1234', 'lr_no': 'LR-77'}
        data.update(extra)
        return self.client.post(reverse('dispatch_add', args=[order_obj.pk]), {f'o{order_obj.pk}-{k}': v for k, v in data.items()}, follow=True)

    def test_only_orders_in_production_are_listed(self):
        page = self.client.get(reverse('dispatch_planning'))
        self.assertContains(page, f'ORD-{self.live.order_no:04d}')
        self.assertNotContains(page, f'ORD-{self.pending.order_no:04d} ·')
        self.assertNotContains(page, f'ORD-{self.done.order_no:04d} ·')

    def test_a_shipment_is_planned_with_a_clean_vehicle_number(self):
        self.assertContains(self.plan(), 'Shipment planned')
        shipment = Dispatch.objects.get()
        self.assertEqual((shipment.order, shipment.weight, shipment.vehicle_no, shipment.lr_no, shipment.dispatched_at),
                         (self.live, Decimal('400'), 'DL1C1234', 'LR-77', None))

    def test_the_page_shows_what_is_planned_dispatched_and_left(self):
        self.plan()
        self.plan(weight='100')
        Dispatch.objects.filter(weight=Decimal('100')).update(dispatched_at='2026-12-02T10:00:00Z')
        page = self.client.get(reverse('dispatch_planning'))
        self.assertContains(page, 'Dispatched 100 kg · planned 400 kg · not yet planned 500 kg')

    def test_a_bad_weight_or_date_adds_nothing(self):
        for bad in ({'weight': '0'}, {'weight': ''}, {'planned_date': ''}):
            self.assertContains(self.plan(**bad), 'Could not add the shipment')
        self.assertFalse(Dispatch.objects.exists())

    def test_a_shipment_cannot_be_added_to_an_order_not_in_production(self):
        self.assertEqual(self.plan(self.pending).status_code, 404)

    def test_marking_dispatched_stamps_the_time_once(self):
        self.plan()
        shipment = Dispatch.objects.get()
        self.client.post(reverse('dispatch_mark', args=[shipment.pk]))
        shipment.refresh_from_db()
        first = shipment.dispatched_at
        self.assertIsNotNone(first)
        self.client.post(reverse('dispatch_mark', args=[shipment.pk]))
        shipment.refresh_from_db()
        self.assertEqual(shipment.dispatched_at, first)

    def test_only_a_planned_shipment_can_be_removed(self):
        self.plan()
        self.plan(weight='100')
        gone, sent = Dispatch.objects.order_by('pk')
        sent.dispatched_at = gone.created_at
        sent.save()
        self.client.post(reverse('dispatch_delete', args=[gone.pk]))
        self.assertEqual(self.client.post(reverse('dispatch_delete', args=[sent.pk])).status_code, 404)
        self.assertEqual(list(Dispatch.objects.values_list('pk', flat=True)), [sent.pk])

    def test_the_changes_need_post(self):
        self.plan()
        shipment = Dispatch.objects.get()
        for name, args in (('dispatch_mark', [shipment.pk]), ('dispatch_delete', [shipment.pk]), ('dispatch_add', [self.live.pk])):
            self.assertEqual(self.client.get(reverse(name, args=args)).status_code, 405, name)


class DispatchReadOnlyTests(TestCase):
    def setUp(self):
        self.acme = Customer.objects.create(name='ACME')
        self.live = order(self.acme)
        self.planned = Dispatch.objects.create(order=self.live, planned_date='2026-12-01', weight=Decimal('400'), vehicle_no='DL1C1234', lr_no='LR-77')
        self.sent = Dispatch.objects.create(order=self.live, planned_date='2026-11-20', weight=Decimal('250'), dispatched_at='2026-11-21T09:30:00Z')

    def test_employee_and_finance_see_the_list_and_never_a_price(self):
        for pin, name in ((settings.EMPLOYEE_PIN, 'employee_dispatch'), (settings.FINANCE_CODE, 'finance_dispatch')):
            self.client.logout()
            self.client.post(reverse('login'), {'pin': pin})
            page = self.client.get(reverse(name))
            for text in ('ACME', 'DL1C1234', 'LR-77', '400', '250', f'ORD-{self.live.order_no:04d}'):
                self.assertContains(page, text, msg_prefix=name)
            self.assertNotContains(page, '98765', msg_prefix=name)

    def test_planned_shipments_come_before_the_ones_that_left(self):
        self.client.post(reverse('login'), {'pin': settings.EMPLOYEE_PIN})
        page = self.client.get(reverse('employee_dispatch')).content.decode()
        self.assertLess(page.index('Planned'), page.index('Dispatched (latest 30)'))
        self.assertLess(page.index('DL1C1234'), page.index('21 Nov 2026'))

    def test_the_read_only_pages_have_no_change_buttons(self):
        self.client.post(reverse('login'), {'pin': settings.EMPLOYEE_PIN})
        self.assertNotContains(self.client.get(reverse('employee_dispatch')), 'Mark dispatched')
