"""Model behaviour: coil usage/archiving, product-type uniqueness, gate entries and lots."""

from decimal import Decimal
from django.conf import settings
from django.contrib.auth.models import User
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from ..models import Customer, GateEntry, GateEntryLot, Material, Order, OrderCoilPick, ProductType


class MaterialUsedStatusTests(TestCase):
    """A coil is 'used' once every kg of it has been picked for orders."""

    def setUp(self):
        self.customer = Customer.objects.create(name='Used Status Co')
        self.order = Order.objects.create(customer=self.customer, quantity=1000)

    def test_untouched_coil_is_unused(self):
        coil = Material.objects.create(quantity=500)
        self.assertFalse(coil.is_used_up())
        self.assertEqual(coil.weight_used(), 0)
        self.assertEqual(coil.weight_remaining(), 500)

    def test_partially_cut_coil_is_still_unused(self):
        coil = Material.objects.create(quantity=500)
        OrderCoilPick.objects.create(order=self.order, coil=coil, weight_allocated=200)
        self.assertFalse(coil.is_used_up())
        self.assertEqual(coil.weight_remaining(), 300)

    def test_fully_cut_coil_is_used(self):
        coil = Material.objects.create(quantity=500)
        OrderCoilPick.objects.create(order=self.order, coil=coil, weight_allocated=300)
        OrderCoilPick.objects.create(order=self.order, coil=coil, weight_allocated=200)
        self.assertTrue(coil.is_used_up())
        self.assertEqual(coil.weight_remaining(), 0)

    def test_legacy_used_weight_counts_toward_usage(self):
        """Usage recorded before this coil was tracked in the app (imported
        from the spreadsheet's ISSUED QTY columns) counts the same as weight
        picked through the app."""
        coil = Material.objects.create(quantity=500, legacy_used_weight=200)
        self.assertEqual(coil.weight_used(), 200)
        self.assertEqual(coil.weight_remaining(), 300)
        self.assertFalse(coil.is_used_up())

        OrderCoilPick.objects.create(order=self.order, coil=coil, weight_allocated=300)
        self.assertEqual(coil.weight_used(), 500)
        self.assertTrue(coil.is_used_up())

    def test_coil_with_no_quantity_on_file_is_not_marked_used(self):
        """No quantity means unknown, not used — mirrors the existing
        'exhausted' check elsewhere in the app (pick_coil_for_order view)."""
        coil = Material.objects.create(quantity=None)
        self.assertFalse(coil.is_used_up())

    def test_admin_list_shows_correct_status_badge(self):
        staff = User.objects.create_user('used_status_admin', password='pw', is_staff=True, is_superuser=True)
        used = Material.objects.create(quantity=100, heat_no='USEDH01')
        OrderCoilPick.objects.create(order=self.order, coil=used, weight_allocated=100)
        unused = Material.objects.create(quantity=100, heat_no='UNUSEDH1')

        self.client.force_login(staff)
        response = self.client.get(f'/admin/materials/material/?q={used.heat_no}')
        self.assertContains(response, 'Used</span>')
        response = self.client.get(f'/admin/materials/material/?q={unused.heat_no}')
        self.assertContains(response, 'Unused</span>')

    def test_fully_legacy_used_coil_excluded_from_order_coil_selection(self):
        """A coil imported with legacy_used_weight already covering its full
        quantity has no weight left to offer, same as one used up via the app."""
        self.client.post(reverse('employee_login'), {'pin': settings.EMPLOYEE_PIN})
        customer = Customer.objects.create(name='Legacy Test Co')
        order = Order.objects.create(customer=customer, quantity=100, status='confirmed')
        Material.objects.create(quantity=500, legacy_used_weight=500)
        active = Material.objects.create(quantity=500)

        response = self.client.get(reverse('select_coil_for_order', kwargs={'order_pk': order.pk}))
        coil_ids = [c['coil'].pk for c in response.context['coils']]
        self.assertEqual(coil_ids, [active.pk])

    def test_admin_filter_by_used_status(self):
        staff = User.objects.create_user('used_status_admin2', password='pw', is_staff=True, is_superuser=True)
        used = Material.objects.create(quantity=100, heat_no='FILTUSED')
        OrderCoilPick.objects.create(order=self.order, coil=used, weight_allocated=100)
        unused = Material.objects.create(quantity=100, heat_no='FILTUNUSD')

        self.client.force_login(staff)
        response = self.client.get('/admin/materials/material/?used_status=used')
        self.assertContains(response, used.heat_no)
        self.assertNotContains(response, unused.heat_no)

        response = self.client.get('/admin/materials/material/?used_status=unused')
        self.assertContains(response, unused.heat_no)
        self.assertNotContains(response, used.heat_no)


class MaterialArchivingTests(TestCase):
    """Archiving hides a mistaken entry from normal use without ever
    renumbering coil_no — that number may already be on a printed QR tag."""

    def test_archiving_does_not_change_coil_no(self):
        coil = Material.objects.create(quantity=100)
        pk = coil.pk
        self.assertFalse(coil.is_archived())
        coil.archived_at = timezone.now()
        coil.save()
        coil.refresh_from_db()
        self.assertEqual(coil.pk, pk)
        self.assertTrue(coil.is_archived())

    def test_admin_hides_archived_coils_by_default(self):
        staff = User.objects.create_user('archive_admin', password='pw', is_staff=True, is_superuser=True)
        active = Material.objects.create(quantity=100, heat_no='ACTIVE01')
        archived = Material.objects.create(quantity=100, heat_no='ARCHIVED1', archived_at=timezone.now())

        self.client.force_login(staff)
        response = self.client.get('/admin/materials/material/')
        self.assertContains(response, active.heat_no)
        self.assertNotContains(response, archived.heat_no)

        response = self.client.get('/admin/materials/material/?archived=yes')
        self.assertContains(response, archived.heat_no)
        self.assertNotContains(response, active.heat_no)

        response = self.client.get('/admin/materials/material/?archived=all')
        self.assertContains(response, active.heat_no)
        self.assertContains(response, archived.heat_no)

    def test_admin_archive_and_unarchive_actions(self):
        staff = User.objects.create_user('archive_admin2', password='pw', is_staff=True, is_superuser=True)
        coil = Material.objects.create(quantity=100)
        self.client.force_login(staff)

        self.client.post('/admin/materials/material/', {
            'action': 'archive_coils', '_selected_action': [coil.pk],
        })
        coil.refresh_from_db()
        self.assertTrue(coil.is_archived())

        # Archived coils are hidden by default — has to be on the "archived" filter
        # to even see (and select) the checkbox in the first place, same as a real user.
        self.client.post('/admin/materials/material/?archived=yes', {
            'action': 'unarchive_coils', '_selected_action': [coil.pk],
        })
        coil.refresh_from_db()
        self.assertFalse(coil.is_archived())

    def test_archived_coil_excluded_from_order_coil_selection(self):
        self.client.post(reverse('employee_login'), {'pin': settings.EMPLOYEE_PIN})
        customer = Customer.objects.create(name='Archive Test Co')
        order = Order.objects.create(customer=customer, quantity=100, status='confirmed')
        Material.objects.create(quantity=500, archived_at=timezone.now())
        active = Material.objects.create(quantity=500)

        response = self.client.get(reverse('select_coil_for_order', kwargs={'order_pk': order.pk}))
        coil_ids = [c['coil'].pk for c in response.context['coils']]
        self.assertIn(active.pk, coil_ids)
        self.assertEqual(len(coil_ids), 1)

    def test_cannot_pick_an_archived_coil(self):
        self.client.post(reverse('employee_login'), {'pin': settings.EMPLOYEE_PIN})
        coil = Material.objects.create(quantity=500, archived_at=timezone.now())
        product_type = ProductType.objects.create(item_code='Bar', grade='EN8D', size='1.200')
        customer = Customer.objects.create(name='Archive Pick Co')
        order = Order.objects.create(
            customer=customer, product_type=product_type, quantity=100, status='confirmed',
        )

        response = self.client.post(
            reverse('pick_coil_for_order', kwargs={'order_pk': order.pk, 'coil_pk': coil.pk}),
            {'weight_allocated': '10'},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(OrderCoilPick.objects.filter(coil=coil).count(), 0)

    def test_api_excludes_archived_by_default(self):
        staff = User.objects.create_user('archive_api_staff', password='pw', is_staff=True)
        client = APIClient()
        client.force_authenticate(user=staff)
        active = Material.objects.create(quantity=100)
        archived = Material.objects.create(quantity=100, archived_at=timezone.now())

        response = client.get('/api/coils/')
        ids = [row['coil_no'] for row in response.data['results']]
        self.assertIn(active.pk, ids)
        self.assertNotIn(archived.pk, ids)

        response = client.get('/api/coils/?include_archived=true')
        ids = [row['coil_no'] for row in response.data['results']]
        self.assertIn(active.pk, ids)
        self.assertIn(archived.pk, ids)


class ProductTypeUniquenessTests(TestCase):
    """A grade/size combination identifies exactly one product type."""

    def test_duplicate_grade_and_size_rejected(self):
        ProductType.objects.create(item_code='Bar A', grade='EN8D', size='1.200')
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ProductType.objects.create(item_code='Bar B', grade='EN8D', size='1.200')

    def test_same_grade_different_size_allowed(self):
        ProductType.objects.create(item_code='Bar A', grade='EN8D', size='1.200')
        ProductType.objects.create(item_code='Bar B', grade='EN8D', size='1.500')
        self.assertEqual(ProductType.objects.filter(grade='EN8D').count(), 2)


class GateEntryModelTests(TestCase):
    def test_weight_per_coil_splits_evenly_across_all_lots(self):
        ge = GateEntry.objects.create(total_weight=2500)
        GateEntryLot.objects.create(gate_entry=ge, grade='EN8D', size='1.200', no_of_coils=3)
        GateEntryLot.objects.create(gate_entry=ge, grade='SAE1008', size='6.000', no_of_coils=2)
        self.assertEqual(ge.no_of_coils(), 5)
        self.assertEqual(ge.weight_per_coil(), Decimal('500.000'))

    def test_weight_per_coil_rounds_to_three_decimals(self):
        ge = GateEntry.objects.create(total_weight=1000)
        GateEntryLot.objects.create(gate_entry=ge, no_of_coils=3)
        self.assertEqual(ge.weight_per_coil(), Decimal('333.333'))

    def test_no_lots_means_zero_coils_and_not_complete(self):
        """An empty gate entry (no lots added yet) shouldn't read as 'done'."""
        ge = GateEntry.objects.create(total_weight=1000)
        self.assertEqual(ge.no_of_coils(), 0)
        self.assertEqual(ge.weight_per_coil(), Decimal('0'))
        self.assertFalse(ge.is_complete())

    def test_coils_remaining_and_is_complete_span_multiple_lots(self):
        ge = GateEntry.objects.create(total_weight=1000)
        lot1 = GateEntryLot.objects.create(gate_entry=ge, grade='EN8D', size='1.200', no_of_coils=1)
        lot2 = GateEntryLot.objects.create(gate_entry=ge, grade='SAE1008', size='6.000', no_of_coils=1)
        self.assertEqual(ge.coils_remaining(), 2)
        self.assertFalse(ge.is_complete())

        Material.objects.create(lot=lot1, quantity=500)
        self.assertEqual(ge.coils_remaining(), 1)
        self.assertFalse(ge.is_complete())

        Material.objects.create(lot=lot2, quantity=500)
        self.assertEqual(ge.coils_remaining(), 0)
        self.assertTrue(ge.is_complete())


class GateEntryLotModelTests(TestCase):
    def test_coils_remaining_and_is_complete(self):
        ge = GateEntry.objects.create(total_weight=1000)
        lot = GateEntryLot.objects.create(gate_entry=ge, grade='EN8D', size='1.200', no_of_coils=2)
        self.assertEqual(lot.coils_registered(), 0)
        self.assertEqual(lot.coils_remaining(), 2)
        self.assertFalse(lot.is_complete())

        Material.objects.create(lot=lot, quantity=500)
        self.assertEqual(lot.coils_remaining(), 1)
        self.assertFalse(lot.is_complete())

        Material.objects.create(lot=lot, quantity=500)
        self.assertEqual(lot.coils_remaining(), 0)
        self.assertTrue(lot.is_complete())
