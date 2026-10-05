"""Orders: numbering, stock check, workflow, dashboard, autocomplete."""

import tempfile

from decimal import Decimal
from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from ..models import AllowedCoilSpec, Customer, Material, Order, OrderCoilPick, ProductType


class OrderNumberingTests(TestCase):
    """order_no is assigned sequentially and kept gap-free — deleting an
    order renumbers every order after it down by one, unlike coil_no/job_no
    which are never reused (see the post_delete receiver in models.py)."""

    def setUp(self):
        self.customer = Customer.objects.create(name='Numbering Test Co')

    def test_order_no_assigned_sequentially(self):
        first = Order.objects.create(customer=self.customer, quantity=10)
        second = Order.objects.create(customer=self.customer, quantity=10)
        third = Order.objects.create(customer=self.customer, quantity=10)
        self.assertEqual([first.order_no, second.order_no, third.order_no], [1, 2, 3])

    def test_deleting_middle_order_renumbers_later_ones_down(self):
        first = Order.objects.create(customer=self.customer, quantity=10)
        second = Order.objects.create(customer=self.customer, quantity=10)
        third = Order.objects.create(customer=self.customer, quantity=10)

        second.delete()

        first.refresh_from_db()
        third.refresh_from_db()
        self.assertEqual(first.order_no, 1)
        self.assertEqual(third.order_no, 2)  # was 3, shifted down to close the gap

    def test_deleting_last_order_leaves_earlier_ones_unchanged(self):
        first = Order.objects.create(customer=self.customer, quantity=10)
        second = Order.objects.create(customer=self.customer, quantity=10)

        second.delete()

        first.refresh_from_db()
        self.assertEqual(first.order_no, 1)

    def test_next_order_after_a_delete_continues_from_the_compacted_sequence(self):
        Order.objects.create(customer=self.customer, quantity=10)
        second = Order.objects.create(customer=self.customer, quantity=10)
        second.delete()

        third = Order.objects.create(customer=self.customer, quantity=10)
        self.assertEqual(third.order_no, 2)  # fills the slot vacated by the delete

    def test_bulk_delete_still_compacts_correctly(self):
        orders = [Order.objects.create(customer=self.customer, quantity=10) for _ in range(5)]
        Order.objects.filter(pk__in=[orders[1].pk, orders[3].pk]).delete()  # delete #2 and #4

        remaining_order_nos = sorted(
            Order.objects.filter(pk__in=[o.pk for o in orders if o.pk not in (orders[1].pk, orders[3].pk)])
            .values_list('order_no', flat=True)
        )
        self.assertEqual(remaining_order_nos, [1, 2, 3])

    def test_str_shows_order_no_not_pk(self):
        """A gap from an earlier delete means order_no and pk can diverge —
        the display string must use order_no."""
        first = Order.objects.create(customer=self.customer, quantity=10)
        first.delete()
        second = Order.objects.create(customer=self.customer, quantity=10)
        self.assertNotEqual(second.pk, second.order_no)
        self.assertIn(f'ORD-{second.order_no:04d}', str(second))


class RawMaterialAvailabilityTests(TestCase):
    """Order.available_raw_material_output()/has_sufficient_raw_material()
    tell an admin, at confirm time, whether there's enough matching raw
    material in stock — before committing an order to production."""

    def setUp(self):
        self.customer = Customer.objects.create(name='Stock Check Co')
        self.product_type = ProductType.objects.create(item_code='Stock Bar', grade='X', size='9.999')

    def test_none_without_a_product_type(self):
        order = Order.objects.create(customer=self.customer, quantity=100)
        self.assertIsNone(order.available_raw_material_output())
        self.assertIsNone(order.has_sufficient_raw_material())

    def test_sums_matching_coils_only(self):
        AllowedCoilSpec.objects.create(product_type=self.product_type, grade='EN8D', size='1.200')
        Material.objects.create(quantity=300, grade='EN8D', size='1.200')
        Material.objects.create(quantity=200, grade='EN8D', size='1.200')
        Material.objects.create(quantity=500, grade='SAE1008', size='6.000')  # doesn't match, excluded
        order = Order.objects.create(customer=self.customer, product_type=self.product_type, quantity=400)

        self.assertEqual(order.available_raw_material_output(), Decimal('500'))
        self.assertTrue(order.has_sufficient_raw_material())

    def test_insufficient_when_stock_falls_short(self):
        AllowedCoilSpec.objects.create(product_type=self.product_type, grade='EN8D', size='1.200')
        Material.objects.create(quantity=100, grade='EN8D', size='1.200')
        order = Order.objects.create(customer=self.customer, product_type=self.product_type, quantity=400)

        self.assertEqual(order.available_raw_material_output(), Decimal('100'))
        self.assertFalse(order.has_sufficient_raw_material())

    def test_ratio_reduces_available_output(self):
        """1.1 ratio means 110kg of raw material only yields 100kg of output."""
        AllowedCoilSpec.objects.create(
            product_type=self.product_type, grade='EN8D', size='1.200',
            raw_material_ratio=Decimal('1.100'),
        )
        Material.objects.create(quantity=110, grade='EN8D', size='1.200')
        order = Order.objects.create(customer=self.customer, product_type=self.product_type, quantity=100)

        self.assertEqual(order.available_raw_material_output(), Decimal('100'))
        self.assertTrue(order.has_sufficient_raw_material())

    def test_no_specs_configured_counts_any_coil(self):
        """Matches the wildcard fallback the picking flow already uses when
        a product type has no AllowedCoilSpecs configured."""
        Material.objects.create(quantity=250, grade='ANYTHING', size='3.000')
        order = Order.objects.create(customer=self.customer, product_type=self.product_type, quantity=100)

        self.assertEqual(order.available_raw_material_output(), Decimal('250'))

    def test_archived_coils_excluded(self):
        AllowedCoilSpec.objects.create(product_type=self.product_type, grade='EN8D', size='1.200')
        Material.objects.create(quantity=300, grade='EN8D', size='1.200', archived_at=timezone.now())
        order = Order.objects.create(customer=self.customer, product_type=self.product_type, quantity=100)

        self.assertEqual(order.available_raw_material_output(), Decimal('0'))
        self.assertFalse(order.has_sufficient_raw_material())

    def test_already_picked_weight_reduces_available_stock(self):
        AllowedCoilSpec.objects.create(product_type=self.product_type, grade='EN8D', size='1.200')
        coil = Material.objects.create(quantity=300, grade='EN8D', size='1.200')
        order = Order.objects.create(customer=self.customer, product_type=self.product_type, quantity=100)
        OrderCoilPick.objects.create(order=order, coil=coil, weight_allocated=250)

        self.assertEqual(order.available_raw_material_output(), Decimal('50'))


class OrderConfirmStockWarningTests(TestCase):
    """Confirming an order checks raw material availability and warns —
    on-screen only, no email — if stock looks short."""

    def setUp(self):
        self.staff = User.objects.create_user('stock_staff', password='pw', is_staff=True)
        self.customer = Customer.objects.create(name='Confirm Stock Co')
        self.product_type = ProductType.objects.create(item_code='Confirm Bar', grade='X', size='9.999')
        AllowedCoilSpec.objects.create(product_type=self.product_type, grade='EN8D', size='1.200')

    def test_confirming_with_insufficient_stock_shows_warning(self):
        Material.objects.create(quantity=50, grade='EN8D', size='1.200')
        order = Order.objects.create(
            customer=self.customer, product_type=self.product_type, quantity=400, status='pending',
        )
        self.client.force_login(self.staff)
        response = self.client.post(reverse('order_confirm', kwargs={'pk': order.pk}), follow=True)
        messages = [str(m) for m in response.context['messages']]
        self.assertTrue(any('looks short' in m for m in messages))

    def test_confirming_with_sufficient_stock_shows_no_warning(self):
        Material.objects.create(quantity=500, grade='EN8D', size='1.200')
        order = Order.objects.create(
            customer=self.customer, product_type=self.product_type, quantity=400, status='pending',
        )
        self.client.force_login(self.staff)
        response = self.client.post(reverse('order_confirm', kwargs={'pk': order.pk}), follow=True)
        messages = [str(m) for m in response.context['messages']]
        self.assertFalse(any('looks short' in m for m in messages))

    def test_dashboard_shows_persistent_low_stock_badge(self):
        Material.objects.create(quantity=50, grade='EN8D', size='1.200')
        Order.objects.create(
            customer=self.customer, product_type=self.product_type, quantity=400, status='confirmed',
        )
        self.client.force_login(self.staff)
        response = self.client.get(reverse('order_dashboard'))
        self.assertContains(response, 'Low stock')

    def test_dashboard_hides_badge_for_pending_orders(self):
        """The check is only actionable once an order is actually
        committed to production — a pending order hasn't been accepted yet."""
        Material.objects.create(quantity=50, grade='EN8D', size='1.200')
        Order.objects.create(
            customer=self.customer, product_type=self.product_type, quantity=400, status='pending',
        )
        self.client.force_login(self.staff)
        response = self.client.get(reverse('order_dashboard'))
        self.assertNotContains(response, 'Low stock')

    def test_dashboard_hides_badge_when_stock_is_sufficient(self):
        Material.objects.create(quantity=500, grade='EN8D', size='1.200')
        Order.objects.create(
            customer=self.customer, product_type=self.product_type, quantity=400, status='confirmed',
        )
        self.client.force_login(self.staff)
        response = self.client.get(reverse('order_dashboard'))
        self.assertNotContains(response, 'Low stock')


class OrderWorkflowTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user('staff', password='pw', is_staff=True)
        self.customer = Customer.objects.create(name='Acme Corp')

    def test_order_confirm_requires_product_type(self):
        order = Order.objects.create(customer=self.customer, quantity=100, status='pending')
        self.client.force_login(self.staff)
        self.client.post(reverse('order_confirm', kwargs={'pk': order.pk}))
        order.refresh_from_db()
        self.assertEqual(order.status, 'pending')

    def test_confirm_dispatch_reject_ignore_get_requests(self):
        """A bare GET must never confirm/dispatch/reject an order (CSRF via link/image)."""
        product_type = ProductType.objects.create(item_code='Bar', grade='EN8D', size='1.200')
        order = Order.objects.create(
            customer=self.customer, quantity=100, status='pending', product_type=product_type,
        )
        self.client.force_login(self.staff)

        self.client.get(reverse('order_confirm', kwargs={'pk': order.pk}))
        order.refresh_from_db()
        self.assertEqual(order.status, 'pending')

        order.status = 'in_production'
        order.save(update_fields=['status'])
        self.client.get(reverse('order_dispatch', kwargs={'pk': order.pk}))
        order.refresh_from_db()
        self.assertEqual(order.status, 'in_production')

        self.client.get(reverse('order_reject', kwargs={'pk': order.pk}))
        order.refresh_from_db()
        self.assertEqual(order.status, 'in_production')

        # the real POST path still works
        self.client.post(reverse('order_confirm', kwargs={'pk': order.pk}))
        order.refresh_from_db()
        self.assertEqual(order.status, 'in_production')  # already past 'confirmed', unaffected by re-confirm
        self.client.post(reverse('order_dispatch', kwargs={'pk': order.pk}))
        order.refresh_from_db()
        self.assertEqual(order.status, 'completed')

    def test_quote_token_regenerates_after_submission(self):
        old_token = self.customer.quote_token
        self.client.post(
            reverse('quote_form', kwargs={'token': old_token}),
            {'quantity': '250'},
        )
        self.customer.refresh_from_db()
        self.assertNotEqual(self.customer.quote_token, old_token)
        self.assertTrue(Order.objects.filter(customer=self.customer, status='pending').exists())

        response = self.client.get(reverse('quote_form', kwargs={'token': old_token}))
        self.assertEqual(response.status_code, 404)

    def test_quote_form_rejects_non_numeric_quantity(self):
        response = self.client.post(
            reverse('quote_form', kwargs={'token': self.customer.quote_token}),
            {'quantity': 'not-a-number'},
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Order.objects.filter(customer=self.customer).exists())

    def test_quote_form_saves_uploaded_purchase_order(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        with tempfile.TemporaryDirectory() as tmp_media_root:
            with override_settings(MEDIA_ROOT=tmp_media_root):
                po_file = SimpleUploadedFile('my_po.pdf', b'%PDF-1.4 fake po content', content_type='application/pdf')
                self.client.post(
                    reverse('quote_form', kwargs={'token': self.customer.quote_token}),
                    {'quantity': '250', 'purchase_order': po_file},
                )
                order = Order.objects.get(customer=self.customer)
                self.assertTrue(order.purchase_order)
                self.assertIn('my_po', order.purchase_order.name)

    def test_quote_form_purchase_order_is_optional(self):
        self.client.post(
            reverse('quote_form', kwargs={'token': self.customer.quote_token}),
            {'quantity': '250'},
        )
        order = Order.objects.get(customer=self.customer)
        self.assertFalse(order.purchase_order)


class OrderDashboardTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user('dash_staff', password='pw', is_staff=True)

    def test_anonymous_request_redirects_to_admin_login(self):
        response = self.client.get(reverse('order_dashboard'))
        self.assertRedirects(response, f"{reverse('admin_login')}?next={reverse('order_dashboard')}")

    def test_non_staff_request_redirects_to_admin_login(self):
        User.objects.create_user('not_staff', password='pw')
        self.client.login(username='not_staff', password='pw')
        response = self.client.get(reverse('order_dashboard'))
        self.assertRedirects(response, f"{reverse('admin_login')}?next={reverse('order_dashboard')}")

    def test_staff_can_create_order_directly_as_confirmed(self):
        """Orders entered by staff (not via the customer quote form) skip
        straight to 'confirmed' — no review step needed for their own entry."""
        self.client.force_login(self.staff)
        response = self.client.post(reverse('order_dashboard'), {
            'name': 'New Dashboard Co', 'email': 'contact@newdash.co', 'quantity': '150',
        })
        self.assertRedirects(response, reverse('order_dashboard'))
        order = Order.objects.get(customer__name='New Dashboard Co')
        self.assertEqual(order.status, 'confirmed')
        self.assertEqual(order.customer.email, 'contact@newdash.co')

    def test_missing_company_name_shows_error_instead_of_crashing(self):
        self.client.force_login(self.staff)
        response = self.client.post(reverse('order_dashboard'), {'quantity': '150'})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Company name is required.")
        self.assertFalse(Order.objects.exists())

    def test_missing_quantity_shows_form_error(self):
        self.client.force_login(self.staff)
        response = self.client.post(reverse('order_dashboard'), {'name': 'No Qty Co'})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Order.objects.exists())


class CustomerAutocompleteTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user('autocomplete_staff', password='pw', is_staff=True)
        Customer.objects.create(name='Acme Traders', email='a@acme.com')
        Customer.objects.create(name='Beta Industries', email='b@beta.com')

    def test_anonymous_request_gets_empty_list(self):
        response = self.client.get(reverse('customer_autocomplete'), {'q': 'Acme'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])

    def test_empty_query_returns_nothing(self):
        self.client.force_login(self.staff)
        response = self.client.get(reverse('customer_autocomplete'))
        self.assertEqual(response.json(), [])

    def test_matches_by_partial_name(self):
        self.client.force_login(self.staff)
        response = self.client.get(reverse('customer_autocomplete'), {'q': 'acme'})
        names = [r['name'] for r in response.json()]
        self.assertEqual(names, ['Acme Traders'])
