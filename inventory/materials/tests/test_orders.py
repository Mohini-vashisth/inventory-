"""Orders: numbering, stock check, workflow, dashboard, autocomplete."""

import datetime
import os
import re
import tempfile

from decimal import Decimal
from unittest.mock import patch
from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .helpers import create_query
from ..models import (
    AllowedCoilSpec, Customer, Material, Order, OrderCoilPick, ProductCategory, ProductType, Query, Quotation,
    QuotationLineItem,
)


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
        self.product_type = ProductType.objects.create(item_code='Stock Bar', grade='EN8D')

    def test_none_without_a_product_type(self):
        order = Order.objects.create(customer=self.customer, quantity=100)
        self.assertIsNone(order.available_raw_material_output())
        self.assertIsNone(order.has_sufficient_raw_material())

    def test_sums_matching_coils_only(self):
        AllowedCoilSpec.objects.create(product_type=self.product_type, size='1.200')
        Material.objects.create(quantity=300, grade='EN8D', size='1.200')
        Material.objects.create(quantity=200, grade='EN8D', size='1.200')
        Material.objects.create(quantity=500, grade='SAE1008', size='6.000')  # doesn't match, excluded
        order = Order.objects.create(customer=self.customer, product_type=self.product_type, quantity=400)

        self.assertEqual(order.available_raw_material_output(), Decimal('500'))
        self.assertTrue(order.has_sufficient_raw_material())

    def test_insufficient_when_stock_falls_short(self):
        AllowedCoilSpec.objects.create(product_type=self.product_type, size='1.200')
        Material.objects.create(quantity=100, grade='EN8D', size='1.200')
        order = Order.objects.create(customer=self.customer, product_type=self.product_type, quantity=400)

        self.assertEqual(order.available_raw_material_output(), Decimal('100'))
        self.assertFalse(order.has_sufficient_raw_material())

    def test_ratio_reduces_available_output(self):
        """1.1 ratio means 110kg of raw material only yields 100kg of output."""
        AllowedCoilSpec.objects.create(
            product_type=self.product_type, size='1.200',
            raw_material_ratio=Decimal('1.100'),
        )
        Material.objects.create(quantity=110, grade='EN8D', size='1.200')
        order = Order.objects.create(customer=self.customer, product_type=self.product_type, quantity=100)

        self.assertEqual(order.available_raw_material_output(), Decimal('100'))
        self.assertTrue(order.has_sufficient_raw_material())

    def test_no_specs_configured_counts_any_coil(self):
        """Matches the wildcard fallback the picking flow already uses when
        a product type has no AllowedCoilSpecs configured."""
        Material.objects.create(quantity=250, grade='EN8D', size='3.000')
        order = Order.objects.create(customer=self.customer, product_type=self.product_type, quantity=100)

        self.assertEqual(order.available_raw_material_output(), Decimal('250'))

    def test_archived_coils_excluded(self):
        AllowedCoilSpec.objects.create(product_type=self.product_type, size='1.200')
        Material.objects.create(quantity=300, grade='EN8D', size='1.200', archived_at=timezone.now())
        order = Order.objects.create(customer=self.customer, product_type=self.product_type, quantity=100)

        self.assertEqual(order.available_raw_material_output(), Decimal('0'))
        self.assertFalse(order.has_sufficient_raw_material())

    def test_already_picked_weight_reduces_available_stock(self):
        AllowedCoilSpec.objects.create(product_type=self.product_type, size='1.200')
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
        self.product_type = ProductType.objects.create(item_code='Confirm Bar', grade='EN8D')
        AllowedCoilSpec.objects.create(product_type=self.product_type, size='1.200')

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
        product_type = ProductType.objects.create(item_code='Bar', grade='EN8D')
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


class CustomerOrderFormTests(TestCase):
    """The customer's order form: product code, grade, width and thickness come from the
    quote and are locked; each quoted item becomes its own order."""

    def setUp(self):
        self.customer = Customer.objects.create(name='Order Form Co', email='of@example.com')
        self.code_a = ProductType.objects.create(item_code='CODE-A', grade='EN8D')
        self.code_b = ProductType.objects.create(item_code='CODE-B', grade='SS304')
        self.quotation = Quotation.objects.create(customer=self.customer, status='sent')
        self.item_a = QuotationLineItem.objects.create(
            quotation=self.quotation, order=1, description='Bar A', product_type=self.code_a,
            grade='EN8D', width=Decimal('50'), thickness=Decimal('6'), quantity=Decimal('500'), rate_per_kg=90)
        self.item_b = QuotationLineItem.objects.create(
            quotation=self.quotation, order=2, description='Bar B', product_type=self.code_b,
            grade='SS304', width=Decimal('60.5'), thickness=Decimal('8'), quantity=Decimal('300'), rate_per_kg=120)
        self.url = reverse('quote_form', kwargs={'token': self.customer.quote_token})

    def _post_data(self, items=None, **overrides):
        items = items if items is not None else [self.item_a, self.item_b]
        data = {'item-TOTAL_FORMS': str(len(items)), 'item-INITIAL_FORMS': str(len(items)),
                'item-MIN_NUM_FORMS': '0', 'item-MAX_NUM_FORMS': '1000'}
        for index, item in enumerate(items):
            data[f'item-{index}-line_item'] = str(item.pk)
            data[f'item-{index}-quantity'] = str(item.quantity)
        data.update(overrides)
        return data

    def test_each_quoted_item_is_shown_with_its_code_grade_width_and_thickness_locked(self):
        html = self.client.get(self.url).content.decode()
        for text in ('CODE-A', 'EN8D', '50 mm', '6 mm', 'CODE-B', 'SS304', '60.5 mm', '8 mm', 'Bar A', 'Bar B'):
            with self.subTest(text=text):
                self.assertIn(text, html)
        for name in ('product_type', 'grade', 'width', 'thickness'):
            with self.subTest(field=name):
                self.assertNotIn(f'name="{name}"', html)
                self.assertNotIn(f'-{name}"', html)  # no item-N-product_type / -grade / -width / -thickness inputs either

    def test_each_item_shows_its_product_type(self):
        flat = ProductCategory.objects.get(name='Flat Bright Bar')
        square = ProductCategory.objects.get(name='Square Bright Bar')
        self.item_a.category = flat
        self.item_a.save()
        self.code_b.category = square   # item B has no type of its own: it is shown from its product code
        self.code_b.save()
        html = self.client.get(self.url).content.decode()
        self.assertIn('<b>Product type</b> Flat Bright Bar', html)
        self.assertIn('<b>Product type</b> Square Bright Bar', html)

    def test_the_product_code_is_shown_even_when_the_quote_line_has_none_stored(self):
        flat = ProductCategory.objects.get(name='Flat Bright Bar')
        code = ProductType.objects.create(item_code='FBB009', category=flat, grade='EN8D')
        self.item_a.product_type = None
        self.item_a.category = flat
        self.item_a.save()
        html = self.client.get(self.url).content.decode()
        self.assertIn('<b>Product code</b> FBB009', html)
        self.client.post(self.url, self._post_data())
        self.assertEqual(Order.objects.filter(customer=self.customer).order_by('pk').first().product_type, code)

    def test_an_item_with_no_code_says_it_is_to_be_assigned(self):
        self.item_b.product_type = None
        self.item_b.grade = 'UNKNOWNGRADE'
        self.item_b.save()
        self.assertIn('<b>Product code</b> To be assigned', self.client.get(self.url).content.decode())

    def test_the_quantity_label_asks_for_kgs_only(self):
        self.assertIn('Required Quantity (kgs only)', self.client.get(self.url).content.decode())

    def test_quantity_is_prefilled_from_the_quote(self):
        html = self.client.get(self.url).content.decode()
        self.assertIn('value="500.000"', html)
        self.assertIn('value="300.000"', html)

    def test_submitting_creates_one_order_per_quoted_item_with_the_quoted_code_grade_and_dimensions(self):
        self.client.post(self.url, self._post_data())
        orders = Order.objects.filter(customer=self.customer).order_by('pk')
        self.assertEqual(orders.count(), 2)
        first, second = orders
        self.assertEqual((first.product_type, first.grade, first.width, first.thickness, first.quantity),
                         (self.code_a, 'EN8D', Decimal('50'), Decimal('6'), Decimal('500')))
        self.assertEqual((second.product_type, second.grade, second.width, second.thickness, second.quantity),
                         (self.code_b, 'SS304', Decimal('60.5'), Decimal('8'), Decimal('300')))
        self.assertTrue(all(o.status == 'pending' for o in orders))

    def test_posted_code_grade_width_and_thickness_are_ignored(self):
        data = self._post_data(**{
            'item-0-product_type': str(self.code_b.pk), 'item-0-grade': 'HACKED', 'item-0-width': '9.999', 'item-0-thickness': '9.999',
            'product_type': str(self.code_b.pk), 'grade': 'HACKED', 'width': '9.999', 'thickness': '9.999',
        })
        self.client.post(self.url, data)
        first = Order.objects.filter(customer=self.customer).order_by('pk').first()
        self.assertEqual((first.product_type, first.grade, first.width, first.thickness),
                         (self.code_a, 'EN8D', Decimal('50'), Decimal('6')))

    def test_customer_can_change_quantity_and_add_details_per_item(self):
        data = self._post_data(**{'item-0-quantity': '650', 'item-0-end_usage': 'shafts',
                                  'item-1-frequency': 'monthly', 'item-1-delivery_form': 'coil', 'item-1-coil_weight': '1500'})
        self.client.post(self.url, data)
        first, second = Order.objects.filter(customer=self.customer).order_by('pk')
        self.assertEqual((first.quantity, first.end_usage), (Decimal('650'), 'shafts'))
        self.assertEqual((second.frequency, second.delivery_form), ('monthly', 'coil'))

    def test_an_item_with_no_catalogue_code_gets_none_and_one_matching_its_spec_is_matched(self):
        self.item_a.product_type = None
        self.item_a.save()
        self.item_b.grade = 'UNKNOWN'
        self.item_b.product_type = None
        self.item_b.save()
        self.client.post(self.url, self._post_data())
        first, second = Order.objects.filter(customer=self.customer).order_by('pk')
        self.assertEqual(first.product_type, self.code_a)   # matched from its grade at order time
        self.assertIsNone(second.product_type)              # no catalogue code: assigned at confirmation

    def test_quantity_is_required_for_every_item_and_nothing_is_created_on_error(self):
        token_before = self.customer.quote_token
        response = self.client.post(self.url, self._post_data(**{'item-1-quantity': ''}))
        self.assertContains(response, 'error-msg')
        self.assertEqual(Order.objects.filter(customer=self.customer).count(), 0)
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.quote_token, token_before)  # a failed submission doesn't burn the link

    def test_an_out_of_date_or_tampered_form_is_rejected(self):
        other = Customer.objects.create(name='Someone Else')
        foreign_quote = Quotation.objects.create(customer=other, status='sent')
        foreign_item = QuotationLineItem.objects.create(
            quotation=foreign_quote, order=1, description='Not yours', quantity=1, rate_per_kg=1)
        cases = {
            'wrong item id': self._post_data(**{'item-1-line_item': str(foreign_item.pk)}),
            'items swapped': self._post_data(items=[self.item_b, self.item_a]),
            'item dropped': self._post_data(items=[self.item_a]),
            'blank forms': {'item-TOTAL_FORMS': '2', 'item-INITIAL_FORMS': '0',
                            'item-MIN_NUM_FORMS': '0', 'item-MAX_NUM_FORMS': '1000'},
        }
        for label, data in cases.items():
            with self.subTest(case=label):
                response = self.client.post(self.url, data)
                self.assertContains(response, 'error-msg')
                self.assertEqual(Order.objects.filter(customer=self.customer).count(), 0)

    def test_the_purchase_order_is_attached_to_every_order(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        with tempfile.TemporaryDirectory() as tmp, override_settings(MEDIA_ROOT=tmp):
            po = SimpleUploadedFile('one_po.pdf', b'%PDF-1.4 shared po', content_type='application/pdf')
            self.client.post(self.url, {**self._post_data(), 'purchase_order': po})
            orders = list(Order.objects.filter(customer=self.customer))
            self.assertEqual(len(orders), 2)
            for order in orders:
                with self.subTest(order=order.pk):
                    self.assertIn('one_po', order.purchase_order.name)
                    self.assertEqual(order.purchase_order.read(), b'%PDF-1.4 shared po')

    def test_submitting_converts_the_query_links_the_orders_and_burns_the_link(self):
        query = Query.objects.create(source='indiamart', company_name='Order Form Co',
                                     customer=self.customer, status='quote_sent')
        old_token = self.customer.quote_token
        self.client.post(self.url, self._post_data())
        query.refresh_from_db()
        self.customer.refresh_from_db()
        self.assertEqual(query.status, 'converted')
        self.assertTrue(all(o.source_query == query for o in Order.objects.filter(customer=self.customer)))
        self.assertNotEqual(self.customer.quote_token, old_token)
        self.assertEqual(self.client.get(self.url).status_code, 404)

    def test_the_latest_sent_quotation_is_used_and_drafts_are_ignored(self):
        newer = Quotation.objects.create(customer=self.customer, status='sent')
        QuotationLineItem.objects.create(quotation=newer, order=1, description='Newer bar', product_type=self.code_b,
                                         grade='SS304', width=Decimal('2.500'), quantity=Decimal('42'), rate_per_kg=1)
        draft = Quotation.objects.create(customer=self.customer, status='draft')
        QuotationLineItem.objects.create(quotation=draft, order=1, description='Draft bar', quantity=1, rate_per_kg=1)
        html = self.client.get(self.url).content.decode()
        self.assertIn('Newer bar', html)
        self.assertNotIn('Bar A', html)
        self.assertNotIn('Draft bar', html)

    def test_a_single_item_quote_shows_one_block_and_makes_one_order(self):
        single = Customer.objects.create(name='Single Co')
        quote = Quotation.objects.create(customer=single, status='sent')
        item = QuotationLineItem.objects.create(quotation=quote, order=1, description='Only bar',
                                                product_type=self.code_a, grade='EN8D', width=Decimal('50'), thickness=Decimal('6'),
                                                quantity=Decimal('100'), rate_per_kg=1)
        url = reverse('quote_form', kwargs={'token': single.quote_token})
        self.assertContains(self.client.get(url), 'Item 1', count=1)
        self.client.post(url, self._post_data(items=[item]))
        self.assertEqual(Order.objects.filter(customer=single).count(), 1)


class CustomerOrderFormWithoutAQuoteTests(TestCase):
    """An old link sent before quotes existed has nothing to read a code from:
    a single free-form order, still with no product-code dropdown."""

    def setUp(self):
        self.customer = Customer.objects.create(name='Old Link Co')
        self.url = reverse('quote_form', kwargs={'token': self.customer.quote_token})
        self.code = ProductType.objects.create(item_code='CODE-X', grade='EN8D')

    def test_there_is_no_product_code_dropdown(self):
        html = self.client.get(self.url).content.decode()
        self.assertNotIn('name="product_type"', html)
        self.assertIn('name="grade"', html)
        self.assertIn('name="width"', html)
        self.assertIn('name="thickness"', html)

    def test_the_code_is_matched_from_the_grade_typed(self):
        self.client.post(self.url, {'quantity': '250', 'grade': 'en8d', 'width': '50', 'thickness': '6'})
        self.assertEqual(Order.objects.get(customer=self.customer).product_type, self.code)

    def test_the_form_has_a_product_type_choice_that_helps_match_the_code(self):
        flat = ProductCategory.objects.get(name='Flat Bright Bar')
        square = ProductCategory.objects.get(name='Square Bright Bar')
        flat_code = ProductType.objects.create(item_code='FBB-X', category=flat, grade='SS304')
        ProductType.objects.create(item_code='SQB-X', category=square, grade='SS304')   # same grade, other type
        html = self.client.get(self.url).content.decode()
        self.assertIn('name="product_category"', html)
        self.assertIn('Flat Bright Bar', html)
        self.client.post(self.url, {'quantity': '10', 'grade': 'ss304', 'product_category': str(flat.pk)})
        self.assertEqual(Order.objects.get(customer=self.customer).product_type, flat_code)

    def test_no_match_leaves_the_code_to_be_assigned_at_confirmation(self):
        self.client.post(self.url, {'quantity': '250', 'grade': 'EN9', 'width': '30', 'thickness': '3'})
        self.assertIsNone(Order.objects.get(customer=self.customer).product_type)

    def test_a_posted_product_type_is_ignored(self):
        other = ProductType.objects.create(item_code='CODE-Y', grade='SS304')
        self.client.post(self.url, {'quantity': '250', 'product_type': str(other.pk)})
        self.assertIsNone(Order.objects.get(customer=self.customer).product_type)


class OrderFormPrefillFromQueryTests(TestCase):
    """What the customer already told the bot (end use, delivery form) is
    pre-filled on the order form instead of being asked again."""

    def setUp(self):
        self.customer = Customer.objects.create(name='Prefill Order Co')
        self.code = ProductType.objects.create(item_code='CODE-P', grade='EN8D')
        quotation = Quotation.objects.create(customer=self.customer, status='sent')
        self.item = QuotationLineItem.objects.create(
            quotation=quotation, order=1, description='Bar', product_type=self.code,
            grade='EN8D', width=Decimal('50'), thickness=Decimal('6'), quantity=Decimal('500'), rate_per_kg=90)
        self.query = create_query(
            source='whatsapp', contact_phone='9123456780', company_name='Prefill Order Co',
            customer=self.customer, status='quote_sent', end_use='automotive shafts', delivery_form='Coil')
        self.url = reverse('quote_form', kwargs={'token': self.customer.quote_token})

    def _post(self, **overrides):
        data = {'item-TOTAL_FORMS': '1', 'item-INITIAL_FORMS': '1', 'item-MIN_NUM_FORMS': '0',
                'item-MAX_NUM_FORMS': '1000', 'item-0-line_item': str(self.item.pk), 'item-0-quantity': '500',
                'item-0-end_usage': 'automotive shafts', 'item-0-delivery_form': 'coil', 'item-0-coil_weight': '2000'}
        data.update(overrides)
        return self.client.post(self.url, data)

    def test_end_use_and_delivery_form_are_prefilled_on_each_quoted_item(self):
        html = self.client.get(self.url).content.decode()
        self.assertIn('value="automotive shafts"', html)
        self.assertIn('<option value="coil" selected>Coil</option>', html)
        self.assertNotIn('<option value="bar" selected>', html)

    def test_bar_is_prefilled_when_that_was_the_answer(self):
        self.query.items.update(delivery_form='Bar')
        self.assertIn('<option value="bar" selected>Bar</option>', self.client.get(self.url).content.decode())

    def test_submitting_unchanged_saves_the_prefilled_values_on_the_order(self):
        self._post()
        order = Order.objects.get(customer=self.customer)
        self.assertEqual((order.end_usage, order.delivery_form), ('automotive shafts', 'coil'))

    def test_the_customer_can_still_change_them(self):
        self._post(**{'item-0-end_usage': 'gear shafts', 'item-0-delivery_form': 'bar', 'item-0-bar_length': '3000'})
        order = Order.objects.get(customer=self.customer)
        self.assertEqual((order.end_usage, order.delivery_form), ('gear shafts', 'bar'))

    def test_nothing_is_prefilled_without_a_query(self):
        Query.objects.all().delete()
        html = self.client.get(self.url).content.decode()
        self.assertNotIn('value="automotive shafts"', html)
        self.assertNotIn('<option value="coil" selected>', html)

    def test_the_no_quote_fallback_form_is_prefilled_too(self):
        other = Customer.objects.create(name='No Quote Co')
        create_query(source='whatsapp', contact_phone='9000000001', customer=other,
                             status='quote_sent', end_use='structural', delivery_form='Bar')
        html = self.client.get(reverse('quote_form', kwargs={'token': other.quote_token})).content.decode()
        self.assertIn('value="structural"', html)
        self.assertRegex(html, r'<option value="bar"\s+selected>')  # hand-written markup pads the attribute


class OrderFormShowsTheProductTypeTests(TestCase):
    def test_the_quoted_items_product_type_is_shown_with_its_code_locked(self):
        customer = Customer.objects.create(name='Type Show Co')
        category = ProductCategory.objects.get(name='Cold Rolled Strip')
        code = ProductType.objects.create(item_code='FW-1', category=category, grade='SS304')
        quotation = Quotation.objects.create(customer=customer, status='sent')
        QuotationLineItem.objects.create(quotation=quotation, order=1, description='Wire', category=category,
                                         product_type=code, grade='SS304', width=Decimal('2.000'),
                                         quantity=Decimal('100'), rate_per_kg=1)
        html = self.client.get(reverse('quote_form', kwargs={'token': customer.quote_token})).content.decode()
        self.assertIn('Product type', html)
        self.assertIn('Cold Rolled Strip', html)
        self.assertIn('FW-1', html)
        self.assertNotIn('name="item-0-category"', html)   # shown, not editable


class PublicQuoteFormCsrfTests(TestCase):
    """Tailscale Funnel forwards plain HTTP without X-Forwarded-Proto, while the
    browser's Origin is https:// — so unless the public origin is trusted, every
    customer submit on the order form is a 403 ("Origin checking failed")."""

    PUBLIC = 'https://mdw.tail2734e7.ts.net'

    def _post_like_funnel(self):
        from django.test import Client
        customer = Customer.objects.create(name='Funnel Co')
        client = Client(enforce_csrf_checks=True)
        url = reverse('quote_form', kwargs={'token': customer.quote_token})
        client.get(url, HTTP_HOST='mdw.tail2734e7.ts.net')   # sets the CSRF cookie
        token = client.cookies['csrftoken'].value
        return client.post(url, {'csrfmiddlewaretoken': token}, HTTP_HOST='mdw.tail2734e7.ts.net',
                           HTTP_ORIGIN=self.PUBLIC)

    def test_the_public_origin_is_trusted_automatically(self):
        from inventory.settings import trusted_csrf_origins
        self.assertEqual(trusted_csrf_origins('', self.PUBLIC + '/'), [self.PUBLIC])
        self.assertEqual(trusted_csrf_origins(f' https://a.example , {self.PUBLIC}', self.PUBLIC),
                         ['https://a.example', self.PUBLIC])   # no duplicate, blanks and spaces dropped
        self.assertEqual(trusted_csrf_origins('', ''), [])

    @override_settings(ALLOWED_HOSTS=['mdw.tail2734e7.ts.net'], CSRF_TRUSTED_ORIGINS=[])
    def test_without_the_trusted_origin_the_submit_is_a_403(self):
        self.assertEqual(self._post_like_funnel().status_code, 403)

    @override_settings(ALLOWED_HOSTS=['mdw.tail2734e7.ts.net'], CSRF_TRUSTED_ORIGINS=[PUBLIC])
    def test_with_the_trusted_origin_the_submit_gets_past_csrf(self):
        self.assertNotEqual(self._post_like_funnel().status_code, 403)


class OrderReceivedPageTests(TestCase):
    def test_the_confirmation_says_the_order_was_received_and_production_will_start(self):
        from django.template.loader import render_to_string
        html = render_to_string('materials/quote_submitted.html', {'customer': Customer(name='Rao Steel')})
        self.assertIn('Order Received!', html)
        self.assertIn('Your order has been received. Production will start shortly.', html)
        self.assertIn('Rao Steel', html)
        self.assertNotIn('under review', html)

    def test_the_order_form_is_called_an_order_form_with_a_place_order_button(self):
        customer = Customer.objects.create(name='Wording Co')
        html = self.client.get(reverse('quote_form', kwargs={'token': customer.quote_token})).content.decode()
        self.assertIn('<h1>Order Form</h1>', html)
        self.assertIn('Place Order →', html)
        self.assertNotIn('Quotation Request', html)
        self.assertNotIn('Submit Request', html)


class OrderDashboardShowsProductTypeTests(TestCase):
    def test_the_order_row_shows_the_product_type_under_the_code(self):
        staff = User.objects.create_user('dash_type_staff', password='pw', is_staff=True)
        self.client.force_login(staff)
        flat = ProductCategory.objects.get(name='Flat Bright Bar')
        code = ProductType.objects.create(item_code='FBB009', category=flat, grade='EN8D')
        Order.objects.create(customer=Customer.objects.create(name='Row Co'), product_type=code, quantity=10)
        html = self.client.get(reverse('order_dashboard')).content.decode()
        self.assertIn('FBB009', html)
        self.assertIn('Flat Bright Bar', html)


@override_settings(EMAIL_HOST_USER='sender@example.com', DEFAULT_FROM_EMAIL='sender@example.com',
                   COMPANY_NAME='Matta Drawing Works', COMPANY_PHONE='111-222', COMPANY_EMAIL='co@example.com')
class OrderConfirmationEmailTests(TestCase):
    """Staff confirming an order emails the customer what was ordered."""

    def setUp(self):
        from django.core import mail
        self.mail = mail
        self.staff = User.objects.create_user('confirm_staff', password='pw', is_staff=True)
        self.client.force_login(self.staff)
        flat = ProductCategory.objects.get(name='Flat Bright Bar')
        self.code = ProductType.objects.create(item_code='FBB009', category=flat, grade='EN8D')
        self.customer = Customer.objects.create(name='Confirm Co', email='buyer@example.com')

    def _order(self, **fields):
        defaults = dict(customer=self.customer, product_type=self.code, grade='EN8D', width=Decimal('50'),
                        thickness=Decimal('6.5'), quantity=Decimal('500'), status='pending')
        defaults.update(fields)
        return Order.objects.create(**defaults)

    def test_confirming_emails_the_customer_with_the_product_type_and_details(self):
        order = self._order(delivery_form='coil', delivery_date=datetime.date(2026, 11, 20))
        self.client.post(reverse('order_confirm', kwargs={'pk': order.pk}))
        order.refresh_from_db()
        self.assertEqual(order.status, 'confirmed')
        self.assertEqual(len(self.mail.outbox), 1)
        message = self.mail.outbox[0]
        self.assertEqual(message.to, ['buyer@example.com'])
        self.assertEqual(message.subject, 'Order confirmed — Matta Drawing Works')
        for text in ('Dear Confirm Co', 'Production will start shortly', 'Flat Bright Bar / EN8D / 50 x 6.5 mm — 500 kg',
                     'Product code: FBB009', 'Delivery form: Coil', 'Expected delivery: 20 Nov 2026', '111-222 or co@example.com'):
            with self.subTest(text=text):
                self.assertIn(text, message.body)

    def _pdf_text(self, message):
        attachment = message.attachments[0]
        self.assertEqual((attachment[0], attachment[2]), ('Order summary.pdf', 'application/pdf'))
        self.assertTrue(attachment[1].startswith(b'%PDF'))
        return attachment[1]

    def test_the_confirmation_email_carries_a_summary_pdf_of_what_was_ordered(self):
        import re
        from unittest.mock import patch as _patch
        from ..order_pdf import generate_order_summary_pdf
        order = self._order(delivery_form='bar', bar_length=Decimal('3000'), length_tol_from=Decimal('-5'), length_tol_to=Decimal('5'),
                            width_tol_from=Decimal('49.9'), width_tol_to=Decimal('50.1'), mechanical_properties='Tensile 700 MPa',
                            end_usage='gear shafts', delivery_date=datetime.date(2026, 11, 20))
        self.client.post(reverse('order_confirm', kwargs={'pk': order.pk}))
        message = self.mail.outbox[0]
        self.assertIn('A summary of your order is attached.', message.body)
        self._pdf_text(message)
        with _patch('reportlab.rl_config.pageCompression', 0):   # readable content streams
            pdf = generate_order_summary_pdf([order], self.customer, placed_at=order.created_at)
        text = b' '.join(re.findall(rb'\((.*?)\)\s*Tj', pdf)).decode('latin-1')
        for expected in ('ORDER SUMMARY', 'Confirm Co', 'Flat Bright Bar', 'FBB009', 'EN8D', '50 mm', '6.5 mm', '500 kg',
                         'Bar, 3000 mm long', '20 Nov 2026', 'Width 49.9 to 50.1 mm', 'Length -5 to 5 mm', 'Tensile 700 MPa',
                         'gear shafts', 'Not attached'):
            with self.subTest(expected=expected):
                self.assertIn(expected, text)

    def _text_of(self, order, **options):
        import re
        from unittest.mock import patch as _patch
        from ..order_pdf import generate_order_summary_pdf
        with _patch('reportlab.rl_config.pageCompression', 0):
            pdf = generate_order_summary_pdf([order], self.customer, **options)
        return b' '.join(re.findall(rb'\((.*?)\)\s*Tj', pdf)).decode('latin-1')

    def test_the_summary_pdf_is_the_complete_order_with_a_dash_where_nothing_was_entered(self):
        order = self._order()
        text = self._text_of(order, placed_at=order.created_at, confirmed_at=timezone.now())
        self.assertIn(f'ORD-{order.order_no:04d}', text)
        self.assertIn('Confirmed on', text)
        for label in ('Order no.', 'Status', 'Product type', 'Product code', 'Grade', 'Width', 'Thickness', 'Quantity', 'Delivery form',
                      'Expected delivery', 'Frequency', 'Tolerances', 'Mechanical properties', 'Processes', 'End usage', 'Mill make',
                      'Notes', 'Drawing', 'Purchase order'):
            with self.subTest(label=label):
                self.assertIn(label, text)
        self.assertNotIn('Dimensions (typed)', text)   # only older / staff-entered orders have it

    def test_the_summary_pdf_shows_the_quotation_and_the_gst_details_when_known(self):
        query = Query.objects.create(source='whatsapp', contact_phone='919876500070', company_name='Confirm Co',
                                     gst_number='22AAAAA0000A1Z5', gst_address='12 Industrial Area, Faridabad')
        quotation = Quotation.objects.create(customer=self.customer, source_query=query, status='sent')
        order = self._order(source_query=query)
        text = self._text_of(order, quotation=quotation)
        for expected in (quotation.formatted_no(), 'GST number', '22AAAAA0000A1Z5', '12 Industrial Area, Faridabad'):
            with self.subTest(expected=expected):
                self.assertIn(expected, text)

    def test_the_confirmation_email_pdf_is_stamped_and_quotes_the_source_quotation(self):
        import re
        from unittest.mock import patch as _patch
        query = Query.objects.create(source='whatsapp', contact_phone='919876500071', company_name='Confirm Co')
        quotation = Quotation.objects.create(customer=self.customer, source_query=query, status='sent')
        order = self._order(source_query=query)
        with _patch('reportlab.rl_config.pageCompression', 0):
            self.client.post(reverse('order_confirm', kwargs={'pk': order.pk}))
        pdf = self.mail.outbox[0].attachments[0][1]
        text = b' '.join(re.findall(rb'\((.*?)\)\s*Tj', pdf)).decode('latin-1')
        self.assertIn('Confirmed on', text)
        self.assertIn(quotation.formatted_no(), text)
        self.assertIn('Confirmed', text)   # the status row

    def test_nothing_is_emailed_to_us_when_an_order_is_placed(self):
        customer = Customer.objects.create(name='Place Mail Co', email='place@example.com')
        self.client.logout()
        self.client.post(reverse('quote_form', kwargs={'token': customer.quote_token}), {'quantity': '10', 'grade': 'EN8D'})
        self.assertEqual(len(self.mail.outbox), 0)   # the summary goes out when staff confirm, to the customer

    def test_the_staff_see_that_it_was_sent(self):
        order = self._order()
        response = self.client.post(reverse('order_confirm', kwargs={'pk': order.pk}), follow=True)
        self.assertContains(response, 'Confirmation email sent to buyer@example.com')

    def test_no_email_on_file_still_confirms_and_tells_staff(self):
        self.customer.email = ''
        self.customer.save()
        order = self._order()
        response = self.client.post(reverse('order_confirm', kwargs={'pk': order.pk}), follow=True)
        order.refresh_from_db()
        self.assertEqual(order.status, 'confirmed')
        self.assertEqual(len(self.mail.outbox), 0)
        self.assertContains(response, 'no confirmation email was sent')

    @patch('django.core.mail.EmailMessage.send', side_effect=OSError('smtp down'))
    def test_a_mail_failure_never_blocks_the_confirmation(self, _send):
        order = self._order()
        response = self.client.post(reverse('order_confirm', kwargs={'pk': order.pk}), follow=True)
        order.refresh_from_db()
        self.assertEqual(order.status, 'confirmed')
        self.assertContains(response, 'could not be sent')

    def test_an_order_that_cannot_be_confirmed_sends_nothing(self):
        order = self._order(product_type=None)
        self.client.post(reverse('order_confirm', kwargs={'pk': order.pk}))
        order.refresh_from_db()
        self.assertEqual(order.status, 'pending')
        self.assertEqual(len(self.mail.outbox), 0)

    def test_placing_an_order_on_the_quote_form_sends_no_email(self):
        customer = Customer.objects.create(name='Place Co', email='place@example.com')
        self.client.logout()
        self.client.post(reverse('quote_form', kwargs={'token': customer.quote_token}), {'quantity': '250', 'grade': 'EN8D'})
        self.assertEqual(Order.objects.filter(customer=customer).count(), 1)
        self.assertEqual(len(self.mail.outbox), 0)   # the email goes out when staff confirm


class OrderDrawingAndToleranceTests(TestCase):
    """The order form takes a drawing as an attachment (not typed text) and tolerances: a From and a To
    beside the width and the thickness, plus a box for any other tolerance."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        override = override_settings(MEDIA_ROOT=self._tmp.name)
        override.enable()
        self.addCleanup(override.disable)
        self.addCleanup(self._tmp.cleanup)
        self.customer = Customer.objects.create(name='Tol Co', email='tol@example.com')
        self.code = ProductType.objects.create(item_code='FBB009', category=ProductCategory.objects.get(name='Flat Bright Bar'), grade='EN8D')
        quotation = Quotation.objects.create(customer=self.customer, status='sent')
        self.item = QuotationLineItem.objects.create(
            quotation=quotation, order=1, description='Flat bar', product_type=self.code, grade='EN8D',
            width=Decimal('50'), thickness=Decimal('6.5'), quantity=Decimal('500'), rate_per_kg=90)
        self.url = reverse('quote_form', kwargs={'token': self.customer.quote_token})

    def _data(self, **extra):
        data = {'item-TOTAL_FORMS': '1', 'item-INITIAL_FORMS': '1', 'item-MIN_NUM_FORMS': '0', 'item-MAX_NUM_FORMS': '1000',
                'item-0-line_item': str(self.item.pk), 'item-0-quantity': '500'}
        data.update(extra)
        return data

    def _pdf(self, name='drawing.pdf'):
        from django.core.files.uploadedfile import SimpleUploadedFile
        return SimpleUploadedFile(name, b'%PDF-1.4 a drawing', content_type='application/pdf')

    def test_the_form_has_an_attachment_box_and_tolerance_rows_instead_of_a_typed_drawing_box(self):
        html = self.client.get(self.url).content.decode()
        self.assertIn('name="item-0-drawing_file"', html)
        self.assertIn('type="file"', html)
        self.assertNotIn('drawing_dimensions', html)
        self.assertNotIn('Drawing / Dimensions', html)
        self.assertIn('Width <b>50 mm</b>', html)          # the value is already written ...
        self.assertIn('Thickness <b>6.5 mm</b>', html)
        for name in ('width_tol_from', 'width_tol_to', 'thickness_tol_from', 'thickness_tol_to', 'other_tolerances'):
            self.assertIn(f'name="item-0-{name}"', html)    # ... with a From and a To in front of it
        self.assertIn('Tolerance for anything else', html)

    def test_the_drawing_and_tolerances_are_saved_on_the_order(self):
        self.client.post(self.url, self._data(**{
            'item-0-drawing_file': self._pdf(), 'item-0-width_tol_from': '49.95', 'item-0-width_tol_to': '50.05',
            'item-0-thickness_tol_from': '-0.02', 'item-0-thickness_tol_to': '0.02', 'item-0-other_tolerances': 'Straightness 1 mm per metre'}))
        order = Order.objects.get(customer=self.customer)
        self.assertTrue(order.drawing_file.name.startswith('order_drawings/'))
        self.assertEqual(order.drawing_file.read()[:4], b'%PDF')
        self.assertEqual((order.width_tol_from, order.width_tol_to), (Decimal('49.95'), Decimal('50.05')))
        self.assertEqual((order.thickness_tol_from, order.thickness_tol_to), (Decimal('-0.02'), Decimal('0.02')))
        self.assertEqual(order.tolerance_lines(), ['Width 49.95 to 50.05 mm', 'Thickness -0.02 to 0.02 mm', 'Other: Straightness 1 mm per metre'])

    def test_everything_here_is_optional(self):
        self.client.post(self.url, self._data())
        order = Order.objects.get(customer=self.customer)
        self.assertFalse(order.drawing_file)
        self.assertEqual(order.tolerance_lines(), [])

    def test_a_half_filled_tolerance_still_reads_sensibly(self):
        self.client.post(self.url, self._data(**{'item-0-width_tol_to': '0.1'}))
        self.assertEqual(Order.objects.get(customer=self.customer).tolerance_lines(), ['Width … to 0.1 mm'])

    def test_a_file_type_that_could_run_in_a_browser_is_refused_and_the_link_is_not_burned(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        token = self.customer.quote_token
        response = self.client.post(self.url, self._data(**{'item-0-drawing_file': SimpleUploadedFile('x.html', b'<script>1</script>')}))
        self.assertContains(response, 'error-msg')
        self.assertEqual(Order.objects.count(), 0)
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.quote_token, token)

    def test_a_drawing_over_the_size_limit_is_refused(self):
        with patch('materials.models.DRAWING_MAX_BYTES', 5):
            response = self.client.post(self.url, self._data(**{'item-0-drawing_file': self._pdf()}))
        self.assertContains(response, 'too large')
        self.assertEqual(Order.objects.count(), 0)

    def test_a_drawing_already_sent_on_whatsapp_is_used_when_none_is_attached(self):
        from django.core.files.base import ContentFile
        query = Query.objects.create(source='whatsapp', contact_phone='919876500020', customer=self.customer, status='quote_sent')
        query.drawing.save('wa.pdf', ContentFile(b'%PDF whatsapp drawing'), save=True)
        html = self.client.get(self.url).content.decode()
        self.assertIn('Please attach the final dimension drawing.', html)
        self.assertNotIn('WhatsApp', html)   # the customer is just asked for the final drawing
        self.client.post(self.url, self._data())
        self.assertEqual(Order.objects.get(customer=self.customer).drawing_file.read(), b'%PDF whatsapp drawing')

    def test_a_newly_attached_drawing_replaces_the_whatsapp_one(self):
        from django.core.files.base import ContentFile
        query = Query.objects.create(source='whatsapp', contact_phone='919876500021', customer=self.customer, status='quote_sent')
        query.drawing.save('wa.pdf', ContentFile(b'%PDF whatsapp drawing'), save=True)
        self.client.post(self.url, self._data(**{'item-0-drawing_file': self._pdf('new.pdf')}))
        self.assertEqual(Order.objects.get(customer=self.customer).drawing_file.read(), b'%PDF-1.4 a drawing')

    def test_the_form_without_a_quote_has_them_too(self):
        plain = Customer.objects.create(name='Plain Co')
        url = reverse('quote_form', kwargs={'token': plain.quote_token})
        html = self.client.get(url).content.decode()
        self.assertIn('name="drawing_file"', html)
        self.assertNotIn('drawing_dimensions', html)
        self.assertIn('name="width_tol_from"', html)
        self.assertIn('name="other_tolerances"', html)
        self.client.post(url, {'quantity': '10', 'grade': 'EN8D', 'width': '40', 'thickness': '4', 'drawing_file': self._pdf(),
                               'thickness_tol_from': '3.9', 'thickness_tol_to': '4.1'})
        order = Order.objects.get(customer=plain)
        self.assertTrue(order.drawing_file)
        self.assertEqual(order.tolerance_lines(), ['Thickness 3.9 to 4.1 mm'])

    def test_staff_see_the_drawing_and_tolerances_on_the_order(self):
        staff = User.objects.create_user('tol_staff', password='pw', is_staff=True)
        self.client.post(self.url, self._data(**{'item-0-drawing_file': self._pdf(), 'item-0-width_tol_from': '49.9', 'item-0-width_tol_to': '50.1'}))
        self.client.force_login(staff)
        order = Order.objects.get(customer=self.customer)
        html = self.client.get(reverse('order_detail', kwargs={'pk': order.pk})).content.decode()
        self.assertIn('Open the attached drawing', html)
        self.assertIn('Width 49.9 to 50.1 mm', html)

    @override_settings(EMAIL_HOST_USER='sender@example.com', DEFAULT_FROM_EMAIL='sender@example.com')
    def test_the_confirmation_email_mentions_them(self):
        from django.core import mail
        staff = User.objects.create_user('tol_staff2', password='pw', is_staff=True)
        self.client.post(self.url, self._data(**{'item-0-drawing_file': self._pdf(), 'item-0-width_tol_from': '49.9', 'item-0-width_tol_to': '50.1'}))
        order = Order.objects.get(customer=self.customer)
        self.client.force_login(staff)
        self.client.post(reverse('order_confirm', kwargs={'pk': order.pk}))
        body = mail.outbox[0].body
        self.assertIn('Tolerance: Width 49.9 to 50.1 mm', body)
        self.assertIn('Drawing: received', body)


class BarLengthAndCoilWeightTests(TestCase):
    """Bar delivery asks for the length (with a tolerance); coil delivery asks for an approximate weight."""

    def setUp(self):
        self.customer = Customer.objects.create(name='Delivery Co', email='d@example.com')
        self.code = ProductType.objects.create(item_code='FBB009', category=ProductCategory.objects.get(name='Flat Bright Bar'), grade='EN8D')
        quotation = Quotation.objects.create(customer=self.customer, status='sent')
        self.items = [QuotationLineItem.objects.create(
            quotation=quotation, order=n, description=f'Bar {n}', product_type=self.code, grade='EN8D',
            width=Decimal('50'), thickness=Decimal('6'), quantity=Decimal('500'), rate_per_kg=90) for n in (1, 2)]
        self.url = reverse('quote_form', kwargs={'token': self.customer.quote_token})

    def _data(self, **extra):
        data = {'item-TOTAL_FORMS': '2', 'item-INITIAL_FORMS': '2', 'item-MIN_NUM_FORMS': '0', 'item-MAX_NUM_FORMS': '1000'}
        for n, item in enumerate(self.items):
            data[f'item-{n}-line_item'] = str(item.pk)
            data[f'item-{n}-quantity'] = '500'
        data.update(extra)
        return data

    def _orders(self):
        return list(Order.objects.filter(customer=self.customer).order_by('pk'))

    def test_the_form_has_both_blocks_per_item_and_shows_the_one_for_the_chosen_form(self):
        html = self.client.get(self.url).content.decode()
        for n in (0, 1):
            for name in ('bar_length', 'length_tol_from', 'length_tol_to', 'coil_weight'):
                self.assertIn(f'name="item-{n}-{name}"', html)
        self.assertIn('Length (mm) *', html)
        self.assertIn('Approx. Coil Weight (kg) *', html)
        self.assertIn('data-for="bar" hidden', html)    # nothing chosen yet: both hidden until a choice
        self.assertIn('data-for="coil" hidden', html)
        self.assertIn('<noscript>', html)               # and visible if the browser has no JavaScript

    def test_a_form_opened_with_bar_chosen_shows_the_length_block(self):
        create_query(source='whatsapp', contact_phone='919876500050', customer=self.customer, status='quote_sent', delivery_form='Bar')
        html = self.client.get(self.url).content.decode()
        # the query's one product prefills the first quoted line (Bar): its length block is open, the second line's is not
        self.assertEqual(html.count('data-for="bar" hidden'), 1)
        self.assertEqual(html.count('data-for="coil" hidden'), 2)

    def test_a_bar_needs_its_length_and_keeps_the_length_tolerance(self):
        self.client.post(self.url, self._data(**{'item-0-delivery_form': 'bar', 'item-0-bar_length': '3000',
                                                  'item-0-length_tol_from': '-0', 'item-0-length_tol_to': '25'}))
        first = self._orders()[0]
        self.assertEqual((first.delivery_form, first.bar_length, first.length_tol_to), ('bar', Decimal('3000'), Decimal('25')))
        self.assertIsNone(first.coil_weight)
        self.assertEqual(first.delivery_detail_text(), 'Bar, 3000 mm long')
        self.assertIn('Length 0 to 25 mm', first.tolerance_lines())

    def test_a_coil_needs_its_approximate_weight(self):
        self.client.post(self.url, self._data(**{'item-1-delivery_form': 'coil', 'item-1-coil_weight': '1800.5'}))
        second = self._orders()[1]
        self.assertEqual((second.delivery_form, second.coil_weight), ('coil', Decimal('1800.5')))
        self.assertIsNone(second.bar_length)
        self.assertEqual(second.delivery_detail_text(), 'Coil, approx. 1800.5 kg')

    def test_choosing_a_form_without_what_goes_with_it_is_refused_and_says_which_item(self):
        token = self.customer.quote_token
        response = self.client.post(self.url, self._data(**{'item-1-delivery_form': 'bar'}))
        self.assertContains(response, 'Item 2: Please enter the bar length in mm.')
        response = self.client.post(self.url, self._data(**{'item-0-delivery_form': 'coil'}))
        self.assertContains(response, 'Item 1: Please enter the approximate coil weight in kg.')
        self.assertEqual(Order.objects.count(), 0)
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.quote_token, token)   # the link is not used up

    def test_zero_is_refused(self):
        response = self.client.post(self.url, self._data(**{'item-0-delivery_form': 'bar', 'item-0-bar_length': '0'}))
        self.assertContains(response, 'must be more than zero')

    def test_no_delivery_form_asks_for_nothing_and_stray_values_are_dropped(self):
        self.client.post(self.url, self._data(**{'item-0-bar_length': '3000', 'item-0-coil_weight': '900'}))
        first = self._orders()[0]
        self.assertEqual((first.delivery_form, first.bar_length, first.coil_weight), ('', None, None))

    def test_switching_the_choice_drops_the_other_values(self):
        self.client.post(self.url, self._data(**{'item-0-delivery_form': 'coil', 'item-0-coil_weight': '900',
                                                  'item-0-bar_length': '3000', 'item-0-length_tol_to': '10'}))
        first = self._orders()[0]
        self.assertEqual((first.coil_weight, first.bar_length, first.length_tol_to), (Decimal('900'), None, None))

    def test_the_form_without_a_quote_asks_the_same_things(self):
        plain = Customer.objects.create(name='Plain Delivery Co')
        url = reverse('quote_form', kwargs={'token': plain.quote_token})
        html = self.client.get(url).content.decode()
        for name in ('bar_length', 'length_tol_from', 'length_tol_to', 'coil_weight'):
            self.assertIn(f'name="{name}"', html)
        response = self.client.post(url, {'quantity': '10', 'grade': 'EN8D', 'delivery_form': 'bar'})
        self.assertContains(response, 'Please enter the bar length in mm.')
        self.client.post(url, {'quantity': '10', 'grade': 'EN8D', 'delivery_form': 'coil', 'coil_weight': '700'})
        self.assertEqual(Order.objects.get(customer=plain).coil_weight, Decimal('700'))

    def test_staff_see_it_in_the_order_details_and_the_confirmation_email(self):
        from django.core import mail
        staff = User.objects.create_user('delivery_staff', password='pw', is_staff=True)
        self.client.post(self.url, self._data(**{'item-0-delivery_form': 'bar', 'item-0-bar_length': '3000', 'item-0-length_tol_from': '-5', 'item-0-length_tol_to': '5',
                                                  'item-1-delivery_form': 'coil', 'item-1-coil_weight': '1200'}))
        self.client.force_login(staff)
        orders = self._orders()
        html = self.client.get(reverse('order_detail', kwargs={'pk': orders[0].pk})).content.decode()
        self.assertIn('Bar, 3000 mm long', html)
        self.assertIn('Coil, approx. 1200 kg', self.client.get(reverse('order_detail', kwargs={'pk': orders[1].pk})).content.decode())
        with override_settings(EMAIL_HOST_USER='s@example.com', DEFAULT_FROM_EMAIL='s@example.com'):
            self.client.post(reverse('order_confirm', kwargs={'pk': self._orders()[0].pk}))
        body = mail.outbox[0].body
        self.assertIn('Delivery form: Bar, 3000 mm long', body)
        self.assertIn('Length -5 to 5 mm', body)

    def test_a_script_shows_and_hides_the_blocks(self):
        import shutil
        import subprocess
        import tempfile
        if not shutil.which('node'):
            self.skipTest('node is not installed')
        html = self.client.get(self.url).content.decode()
        script = re.findall(r'<script>(.*?)</script>', html, re.S)[-1]
        harness = """
          const blocks = ['bar', 'coil'].map(f => ({dataset: {for: f}, hidden: true,
            querySelectorAll: () => [{disabled: false}], querySelector: () => ({required: false})}));
          const select = {value: 'bar', closest: () => ({querySelectorAll: () => blocks}), addEventListener: (e, fn) => { select.onchange = fn; }};
          global.document = {querySelectorAll: (q) => q.startsWith('select') ? [select] : []};
          %s
          const out = [blocks.map(b => b.hidden)];
          select.value = 'coil'; select.onchange();
          out.push(blocks.map(b => b.hidden));
          select.value = ''; select.onchange();
          out.push(blocks.map(b => b.hidden));
          console.log(JSON.stringify(out));
        """ % script
        with tempfile.NamedTemporaryFile('w', suffix='.js', delete=False) as handle:
            handle.write(harness)
        result = subprocess.run(['node', handle.name], capture_output=True, text=True, timeout=20)
        os.unlink(handle.name)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), '[[false,true],[true,false],[true,true]]')   # bar, coil, nothing


class OrderDetailPageTests(TestCase):
    """Clicking an order opens the whole order, like a query does."""

    def setUp(self):
        self.staff = User.objects.create_user('detail_staff', password='pw', is_staff=True)
        self.client.force_login(self.staff)
        self.customer = Customer.objects.create(name='Detail Co', email='detail@example.com', phone='9990001111')
        flat = ProductCategory.objects.get(name='Flat Bright Bar')
        self.code = ProductType.objects.create(item_code='FBB009', category=flat, grade='EN8D')
        self.query = Query.objects.create(source='whatsapp', contact_phone='919876500060', company_name='Detail Co', customer=self.customer)
        quotation = Quotation.objects.create(customer=self.customer, source_query=self.query, status='sent')
        self.quotation = quotation
        self.order = Order.objects.create(
            customer=self.customer, source_query=self.query, product_type=self.code, grade='EN8D', width=Decimal('50'),
            thickness=Decimal('6.5'), quantity=Decimal('500'), status='pending', delivery_form='bar', bar_length=Decimal('3000'),
            length_tol_from=Decimal('-5'), length_tol_to=Decimal('5'), width_tol_from=Decimal('49.9'), width_tol_to=Decimal('50.1'),
            other_tolerances='Straightness 1 mm/m', delivery_date=datetime.date(2026, 11, 20), frequency='monthly',
            mechanical_properties='Tensile 700 MPa', processes='Drilling', end_usage='gear shafts', mill_make='Tata', notes='Call first')
        self.url = reverse('order_detail', kwargs={'pk': self.order.pk})

    def test_the_page_shows_the_whole_order(self):
        html = self.client.get(self.url).content.decode()
        for text in ('Detail Co', 'detail@example.com', '9990001111', f'ORD-{self.order.order_no:04d}', 'Pending', 'Flat Bright Bar', 'FBB009',
                     'EN8D', '50 mm', '6.5 mm', 'Width 49.9 to 50.1 mm', 'Length -5 to 5 mm', 'Other: Straightness 1 mm/m', '500 kg',
                     'Bar, 3000 mm long', '20 Nov 2026', 'Monthly', 'Tensile 700 MPa', 'Drilling', 'gear shafts', 'Tata', 'Call first',
                     'No coils picked yet'):
            with self.subTest(text=text):
                self.assertIn(text, html)

    def test_it_links_to_the_query_and_the_quotation(self):
        html = self.client.get(self.url).content.decode()
        self.assertIn(reverse('query_detail', kwargs={'pk': self.query.pk}), html)
        self.assertIn(reverse('quotation_pdf', kwargs={'pk': self.quotation.pk}), html)

    def test_attachments_are_links_only_when_they_exist(self):
        from django.core.files.base import ContentFile
        html = self.client.get(self.url).content.decode()
        self.assertEqual(html.count('Not attached'), 2)
        with tempfile.TemporaryDirectory() as tmp, override_settings(MEDIA_ROOT=tmp):
            self.order.drawing_file.save('d.pdf', ContentFile(b'%PDF'), save=False)
            self.order.purchase_order.save('po.pdf', ContentFile(b'%PDF'), save=True)
            html = self.client.get(self.url).content.decode()
        self.assertIn('Open the attached drawing', html)
        self.assertIn('Open the purchase order', html)

    def test_actions_follow_the_status(self):
        confirm = reverse('order_confirm', kwargs={'pk': self.order.pk})
        self.assertIn(confirm, self.client.get(self.url).content.decode())   # pending: confirm / reject
        self.client.post(confirm)
        html = self.client.get(self.url).content.decode()
        self.assertNotIn(confirm, html)
        self.assertIn('Confirmed', html)
        Order.objects.filter(pk=self.order.pk).update(status='in_production')
        self.assertIn(reverse('order_dispatch', kwargs={'pk': self.order.pk}), self.client.get(self.url).content.decode())

    def test_an_order_without_a_product_code_says_why_it_cannot_be_confirmed(self):
        Order.objects.filter(pk=self.order.pk).update(product_type=None)
        self.assertIn("can't be confirmed yet", self.client.get(self.url).content.decode())

    def test_picked_coils_are_listed(self):
        coil = Material.objects.create(grade='EN8D', quantity=Decimal('900'))
        OrderCoilPick.objects.create(order=self.order, coil=coil, weight_allocated=Decimal('500'))
        html = self.client.get(self.url).content.decode()
        self.assertIn(coil.formatted_coil(), html)
        self.assertIn('500 / 500 kg picked', html)
        self.assertIn('Fulfilled', html)

    def test_the_list_links_to_it_and_no_longer_expands_in_place(self):
        html = self.client.get(reverse('order_dashboard')).content.decode()
        self.assertEqual(html.count(f'href="{self.url}"'), 3)   # order number, customer name, Details button
        self.assertNotIn('toggleDetail', html)
        self.assertNotIn('detail-row', html.split('<tbody>', 2)[1] if '<tbody>' in html else html)

    def test_a_missing_order_is_a_404_and_anonymous_users_are_sent_to_log_in(self):
        self.assertEqual(self.client.get(reverse('order_detail', kwargs={'pk': 99999})).status_code, 404)
        self.client.logout()
        self.assertEqual(self.client.get(self.url).status_code, 302)
