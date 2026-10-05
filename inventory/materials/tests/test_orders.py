"""Orders: numbering, stock check, workflow, dashboard, autocomplete."""

import tempfile

from decimal import Decimal
from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from ..models import (
    AllowedCoilSpec, Customer, Material, Order, OrderCoilPick, ProductType, Query, Quotation, QuotationLineItem,
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


class CustomerOrderFormTests(TestCase):
    """The customer's order form: product code, grade and size come from the
    quote and are locked; each quoted item becomes its own order."""

    def setUp(self):
        self.customer = Customer.objects.create(name='Order Form Co', email='of@example.com')
        self.code_a = ProductType.objects.create(item_code='CODE-A', grade='EN8D', size='1.200')
        self.code_b = ProductType.objects.create(item_code='CODE-B', grade='SS304', size='2.500')
        self.quotation = Quotation.objects.create(customer=self.customer, status='sent')
        self.item_a = QuotationLineItem.objects.create(
            quotation=self.quotation, order=1, description='Bar A', product_type=self.code_a,
            grade='EN8D', size=Decimal('1.200'), quantity=Decimal('500'), rate_per_kg=90)
        self.item_b = QuotationLineItem.objects.create(
            quotation=self.quotation, order=2, description='Bar B', product_type=self.code_b,
            grade='SS304', size=Decimal('2.500'), quantity=Decimal('300'), rate_per_kg=120)
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

    def test_each_quoted_item_is_shown_with_its_code_grade_and_size_locked(self):
        html = self.client.get(self.url).content.decode()
        for text in ('CODE-A', 'EN8D', '1.200', 'CODE-B', 'SS304', '2.500', 'Bar A', 'Bar B'):
            with self.subTest(text=text):
                self.assertIn(text, html)
        for name in ('product_type', 'grade', 'size'):
            with self.subTest(field=name):
                self.assertNotIn(f'name="{name}"', html)
                self.assertNotIn(f'-{name}"', html)  # no item-N-product_type / -grade / -size inputs either

    def test_quantity_is_prefilled_from_the_quote(self):
        html = self.client.get(self.url).content.decode()
        self.assertIn('value="500.000"', html)
        self.assertIn('value="300.000"', html)

    def test_submitting_creates_one_order_per_quoted_item_with_the_quoted_code_grade_size(self):
        self.client.post(self.url, self._post_data())
        orders = Order.objects.filter(customer=self.customer).order_by('pk')
        self.assertEqual(orders.count(), 2)
        first, second = orders
        self.assertEqual((first.product_type, first.grade, first.size, first.quantity),
                         (self.code_a, 'EN8D', Decimal('1.200'), Decimal('500')))
        self.assertEqual((second.product_type, second.grade, second.size, second.quantity),
                         (self.code_b, 'SS304', Decimal('2.500'), Decimal('300')))
        self.assertTrue(all(o.status == 'pending' for o in orders))

    def test_posted_code_grade_and_size_are_ignored(self):
        data = self._post_data(**{
            'item-0-product_type': str(self.code_b.pk), 'item-0-grade': 'HACKED', 'item-0-size': '9.999',
            'product_type': str(self.code_b.pk), 'grade': 'HACKED', 'size': '9.999',
        })
        self.client.post(self.url, data)
        first = Order.objects.filter(customer=self.customer).order_by('pk').first()
        self.assertEqual((first.product_type, first.grade, first.size), (self.code_a, 'EN8D', Decimal('1.200')))

    def test_customer_can_change_quantity_and_add_details_per_item(self):
        data = self._post_data(**{'item-0-quantity': '650', 'item-0-end_usage': 'shafts',
                                  'item-1-frequency': 'monthly', 'item-1-delivery_form': 'coil'})
        self.client.post(self.url, data)
        first, second = Order.objects.filter(customer=self.customer).order_by('pk')
        self.assertEqual((first.quantity, first.end_usage), (Decimal('650'), 'shafts'))
        self.assertEqual((second.frequency, second.delivery_form), ('monthly', 'coil'))

    def test_an_item_with_no_catalogue_code_gets_none_and_one_matching_its_spec_is_matched(self):
        self.item_a.product_type = None
        self.item_a.save()
        self.item_b.grade, self.item_b.size = 'UNKNOWN', Decimal('7.000')
        self.item_b.product_type = None
        self.item_b.save()
        self.client.post(self.url, self._post_data())
        first, second = Order.objects.filter(customer=self.customer).order_by('pk')
        self.assertEqual(first.product_type, self.code_a)   # matched from EN8D / 1.200 at order time
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
                                         grade='SS304', size=Decimal('2.500'), quantity=Decimal('42'), rate_per_kg=1)
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
                                                product_type=self.code_a, grade='EN8D', size=Decimal('1.200'),
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
        self.code = ProductType.objects.create(item_code='CODE-X', grade='EN8D', size='1.200')

    def test_there_is_no_product_code_dropdown(self):
        html = self.client.get(self.url).content.decode()
        self.assertNotIn('name="product_type"', html)
        self.assertIn('name="grade"', html)
        self.assertIn('name="size"', html)

    def test_the_code_is_matched_from_the_grade_and_size_typed(self):
        self.client.post(self.url, {'quantity': '250', 'grade': 'en8d', 'size': '1.2'})
        self.assertEqual(Order.objects.get(customer=self.customer).product_type, self.code)

    def test_no_match_leaves_the_code_to_be_assigned_at_confirmation(self):
        self.client.post(self.url, {'quantity': '250', 'grade': 'EN9', 'size': '3'})
        self.assertIsNone(Order.objects.get(customer=self.customer).product_type)

    def test_a_posted_product_type_is_ignored(self):
        other = ProductType.objects.create(item_code='CODE-Y', grade='SS304', size='2.500')
        self.client.post(self.url, {'quantity': '250', 'product_type': str(other.pk)})
        self.assertIsNone(Order.objects.get(customer=self.customer).product_type)


class OrderFormPrefillFromQueryTests(TestCase):
    """What the customer already told the bot (end use, delivery form) is
    pre-filled on the order form instead of being asked again."""

    def setUp(self):
        self.customer = Customer.objects.create(name='Prefill Order Co')
        self.code = ProductType.objects.create(item_code='CODE-P', grade='EN8D', size='1.200')
        quotation = Quotation.objects.create(customer=self.customer, status='sent')
        self.item = QuotationLineItem.objects.create(
            quotation=quotation, order=1, description='Bar', product_type=self.code,
            grade='EN8D', size=Decimal('1.200'), quantity=Decimal('500'), rate_per_kg=90)
        self.query = Query.objects.create(
            source='whatsapp', contact_phone='9123456780', company_name='Prefill Order Co',
            customer=self.customer, status='quote_sent', end_use='automotive shafts', delivery_form='Coil')
        self.url = reverse('quote_form', kwargs={'token': self.customer.quote_token})

    def _post(self, **overrides):
        data = {'item-TOTAL_FORMS': '1', 'item-INITIAL_FORMS': '1', 'item-MIN_NUM_FORMS': '0',
                'item-MAX_NUM_FORMS': '1000', 'item-0-line_item': str(self.item.pk), 'item-0-quantity': '500',
                'item-0-end_usage': 'automotive shafts', 'item-0-delivery_form': 'coil'}
        data.update(overrides)
        return self.client.post(self.url, data)

    def test_end_use_and_delivery_form_are_prefilled_on_each_quoted_item(self):
        html = self.client.get(self.url).content.decode()
        self.assertIn('value="automotive shafts"', html)
        self.assertIn('<option value="coil" selected>Coil</option>', html)
        self.assertNotIn('<option value="bar" selected>', html)

    def test_bar_is_prefilled_when_that_was_the_answer(self):
        self.query.delivery_form = 'Bar'
        self.query.save(update_fields=['delivery_form'])
        self.assertIn('<option value="bar" selected>Bar</option>', self.client.get(self.url).content.decode())

    def test_submitting_unchanged_saves_the_prefilled_values_on_the_order(self):
        self._post()
        order = Order.objects.get(customer=self.customer)
        self.assertEqual((order.end_usage, order.delivery_form), ('automotive shafts', 'coil'))

    def test_the_customer_can_still_change_them(self):
        self._post(**{'item-0-end_usage': 'gear shafts', 'item-0-delivery_form': 'bar'})
        order = Order.objects.get(customer=self.customer)
        self.assertEqual((order.end_usage, order.delivery_form), ('gear shafts', 'bar'))

    def test_nothing_is_prefilled_without_a_query(self):
        Query.objects.all().delete()
        html = self.client.get(self.url).content.decode()
        self.assertNotIn('value="automotive shafts"', html)
        self.assertNotIn('<option value="coil" selected>', html)

    def test_the_no_quote_fallback_form_is_prefilled_too(self):
        other = Customer.objects.create(name='No Quote Co')
        Query.objects.create(source='whatsapp', contact_phone='9000000001', customer=other,
                             status='quote_sent', end_use='structural', delivery_form='Bar')
        html = self.client.get(reverse('quote_form', kwargs={'token': other.quote_token})).content.decode()
        self.assertIn('value="structural"', html)
        self.assertRegex(html, r'<option value="bar"\s+selected>')  # hand-written markup pads the attribute
