"""Order-first coil picking: spec filtering, best-fit sort, ratios, scanning."""

from decimal import Decimal
from django.conf import settings
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from unittest.mock import patch

from ..models import (
    AllowedCoilSpec,
    Customer,
    Material,
    Order,
    OrderCoilPick,
    ProcessStep,
    ProductionJob,
    ProductType,
)


class OrderCoilPickCreationTests(TestCase):
    """Picking a coil must be all-or-nothing: never an OrderCoilPick with no job."""

    def setUp(self):
        self.client.post(reverse('employee_login'), {'pin': settings.EMPLOYEE_PIN})
        self.coil = Material.objects.create(
            date='2026-07-01', grade='EN8D', size='1.2',
            company='Tata Steel', vendor='ABC Traders', quantity=500, heat_no='H001',
        )
        self.product_type = ProductType.objects.create(item_code='Bar 1.2mm', grade='EN8D')
        ProcessStep.objects.create(product_type=self.product_type, name='Cutting', order=1)
        ProcessStep.objects.create(product_type=self.product_type, name='Heat treat', order=2)
        self.customer = Customer.objects.create(name='Pick Test Co')
        self.order = Order.objects.create(
            customer=self.customer, product_type=self.product_type, quantity=100, status='confirmed',
        )

    def test_order_without_product_type_creates_nothing(self):
        order = Order.objects.create(customer=self.customer, quantity=100, status='confirmed')
        response = self.client.post(
            reverse('pick_coil_for_order', kwargs={'order_pk': order.pk, 'coil_pk': self.coil.pk}),
            {'weight_allocated': '10'},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(OrderCoilPick.objects.filter(coil=self.coil).count(), 0)

    def test_valid_pick_creates_pick_and_job(self):
        response = self.client.post(
            reverse('pick_coil_for_order', kwargs={'order_pk': self.order.pk, 'coil_pk': self.coil.pk}),
            {'weight_allocated': '10'},
        )
        self.assertEqual(response.status_code, 302)
        pick = OrderCoilPick.objects.get(coil=self.coil)
        self.assertEqual(pick.weight_allocated, Decimal('10'))
        job = ProductionJob.objects.get(pick=pick)
        self.assertEqual(job.step_logs.count(), 2)

    def test_non_numeric_weight_shows_error_instead_of_crashing(self):
        response = self.client.post(
            reverse('pick_coil_for_order', kwargs={'order_pk': self.order.pk, 'coil_pk': self.coil.pk}),
            {'weight_allocated': 'not-a-number'},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Enter a valid weight to allocate.")
        self.assertEqual(OrderCoilPick.objects.filter(coil=self.coil).count(), 0)

    def test_weight_exceeding_remaining_shows_error_instead_of_crashing(self):
        response = self.client.post(
            reverse('pick_coil_for_order', kwargs={'order_pk': self.order.pk, 'coil_pk': self.coil.pk}),
            {'weight_allocated': '600'},  # coil is 500kg
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "exceeds the remaining coil weight")
        self.assertEqual(OrderCoilPick.objects.filter(coil=self.coil).count(), 0)

    def test_concurrent_pick_overshooting_remaining_weight_rolls_back(self):
        """Two requests can both pass the initial "remaining" check against a
        stale read before either has written anything. weight_used() is called
        again after the pick is inserted — simulate a second, already-committed
        pick showing up between those two calls and confirm the whole write
        (pick + job + step logs) rolls back instead of over-allocating the coil."""
        with patch.object(
            Material, 'weight_used',
            side_effect=[Decimal('50'), Decimal('600')],  # under, then over quantity=500
        ):
            response = self.client.post(
                reverse('pick_coil_for_order', kwargs={'order_pk': self.order.pk, 'coil_pk': self.coil.pk}),
                {'weight_allocated': '400'},
            )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Reload the page and try again")
        self.assertEqual(OrderCoilPick.objects.filter(coil=self.coil).count(), 0)
        self.assertEqual(ProductionJob.objects.count(), 0)


class SelectCoilForOrderSpecFilterTests(TestCase):
    """When a product type has AllowedCoilSpecs configured, only matching
    coils are offered — the earlier archiving/legacy tests only cover the
    no-specs-configured case."""

    def setUp(self):
        self.client.post(reverse('employee_login'), {'pin': settings.EMPLOYEE_PIN})
        self.customer = Customer.objects.create(name='Spec Test Co')
        self.product_type = ProductType.objects.create(item_code='Spec Bar', grade='EN8D')
        AllowedCoilSpec.objects.create(product_type=self.product_type, size='1.200')
        self.order = Order.objects.create(
            customer=self.customer, product_type=self.product_type, quantity=100, status='confirmed',
        )

    def test_only_matching_spec_coils_are_offered(self):
        matching = Material.objects.create(quantity=500, grade='EN8D', size='1.200')
        non_matching = Material.objects.create(quantity=500, grade='SAE1008', size='6.000')

        response = self.client.get(reverse('select_coil_for_order', kwargs={'order_pk': self.order.pk}))
        coil_ids = [c['coil'].pk for c in response.context['coils']]
        self.assertEqual(coil_ids, [matching.pk])
        self.assertNotIn(non_matching.pk, coil_ids)

    def test_browse_list_search_box_present_with_matching_data_attributes(self):
        Material.objects.create(quantity=500, grade='EN8D', size='1.200', vendor='ABC Traders')
        response = self.client.get(reverse('select_coil_for_order', kwargs={'order_pk': self.order.pk}))
        self.assertContains(response, 'id="coil-search"')
        self.assertContains(response, 'abc traders')


class SelectCoilForOrderBestFitSortTests(TestCase):
    """Coils closest to what the order still needs are listed first, not
    just the newest coils — a best-fit pick wastes less than always
    grabbing whichever coil was registered most recently."""

    def setUp(self):
        self.client.post(reverse('employee_login'), {'pin': settings.EMPLOYEE_PIN})
        self.customer = Customer.objects.create(name='Best Fit Co')
        self.product_type = ProductType.objects.create(item_code='Fit Bar', grade='EN8D')
        AllowedCoilSpec.objects.create(
            product_type=self.product_type, size='1.200',
            raw_material_ratio=Decimal('1.000'),
        )
        # Order needs 100kg of output — with a 1:1 ratio, closest coil to 100kg wins.
        self.order = Order.objects.create(
            customer=self.customer, product_type=self.product_type, quantity=100, status='confirmed',
        )

    def test_closest_remaining_weight_listed_first(self):
        far = Material.objects.create(quantity=500, grade='EN8D', size='1.200')       # 500 remaining, diff 400
        exact = Material.objects.create(quantity=100, grade='EN8D', size='1.200')     # 100 remaining, diff 0
        close = Material.objects.create(quantity=120, grade='EN8D', size='1.200')     # 120 remaining, diff 20

        response = self.client.get(reverse('select_coil_for_order', kwargs={'order_pk': self.order.pk}))
        coil_ids = [c['coil'].pk for c in response.context['coils']]
        self.assertEqual(coil_ids, [exact.pk, close.pk, far.pk])

    def test_ordering_accounts_for_partial_usage_not_just_total_quantity(self):
        """A big coil already mostly used up can be a closer match than a
        smaller untouched one — sorting must use remaining weight, not the
        coil's original total."""
        big_but_used = Material.objects.create(quantity=1000, grade='EN8D', size='1.200')
        OrderCoilPick.objects.create(order=self.order, coil=big_but_used, weight_allocated=910)  # 90 remaining, diff 10
        small_untouched = Material.objects.create(quantity=200, grade='EN8D', size='1.200')  # 200 remaining, diff 100

        response = self.client.get(reverse('select_coil_for_order', kwargs={'order_pk': self.order.pk}))
        coil_ids = [c['coil'].pk for c in response.context['coils']]
        self.assertEqual(coil_ids, [big_but_used.pk, small_untouched.pk])

    def test_ratio_is_applied_when_computing_best_fit(self):
        """A 1.5 ratio means the order needs 150kg of this raw material to
        produce its 100kg of output — best fit should target 150, not 100."""
        AllowedCoilSpec.objects.filter(product_type=self.product_type).update(raw_material_ratio=Decimal('1.500'))
        near_150 = Material.objects.create(quantity=150, grade='EN8D', size='1.200')  # remaining 150, diff 0
        near_100 = Material.objects.create(quantity=100, grade='EN8D', size='1.200')  # remaining 100, diff 50

        response = self.client.get(reverse('select_coil_for_order', kwargs={'order_pk': self.order.pk}))
        coil_ids = [c['coil'].pk for c in response.context['coils']]
        self.assertEqual(coil_ids, [near_150.pk, near_100.pk])


class OrderCoilPickRatioTests(TestCase):
    """A pick's output_equivalent() converts raw material weight into
    finished-product terms via the matching AllowedCoilSpec's ratio, and
    Order.picked_output_weight()/is_fully_picked() roll that up per order."""

    def setUp(self):
        self.customer = Customer.objects.create(name='Ratio Test Co')
        self.product_type = ProductType.objects.create(item_code='Ratio Bar', grade='EN8D')

    def _coil(self, **kwargs):
        """Freshly re-fetched so DecimalField values (size) come back as
        Decimal rather than the raw string just assigned in memory —
        matches how the real views always read coils (via a DB lookup)."""
        coil = Material.objects.create(**kwargs)
        return Material.objects.get(pk=coil.pk)

    def test_output_equivalent_uses_matching_spec_ratio(self):
        """1.100 ratio = 10% wastage: 110kg of raw material yields 100kg of output."""
        AllowedCoilSpec.objects.create(
            product_type=self.product_type, size='1.200',
            raw_material_ratio=Decimal('1.100'),
        )
        order = Order.objects.create(customer=self.customer, product_type=self.product_type, quantity=100)
        coil = self._coil(quantity=500, grade='EN8D', size='1.200')
        pick = OrderCoilPick.objects.create(order=order, coil=coil, weight_allocated=Decimal('110'))
        self.assertEqual(pick.output_equivalent(), Decimal('100'))

    def test_output_equivalent_falls_back_to_1to1_with_no_matching_spec(self):
        order = Order.objects.create(customer=self.customer, product_type=self.product_type, quantity=100)
        coil = self._coil(quantity=500, grade='UNLISTED', size='9.000')
        pick = OrderCoilPick.objects.create(order=order, coil=coil, weight_allocated=Decimal('50'))
        self.assertEqual(pick.output_equivalent(), Decimal('50'))

    def test_picked_output_weight_sums_across_picks_and_specs(self):
        AllowedCoilSpec.objects.create(
            product_type=self.product_type, size='1.200',
            raw_material_ratio=Decimal('1.100'),
        )
        AllowedCoilSpec.objects.create(
            product_type=self.product_type, size='6.000',
            raw_material_ratio=Decimal('1.000'),
        )
        order = Order.objects.create(customer=self.customer, product_type=self.product_type, quantity=150)
        coil_a = self._coil(quantity=500, grade='EN8D', size='1.200')
        coil_b = self._coil(quantity=500, grade='SAE1008', size='6.000')
        OrderCoilPick.objects.create(order=order, coil=coil_a, weight_allocated=Decimal('110'))  # → 100 output
        OrderCoilPick.objects.create(order=order, coil=coil_b, weight_allocated=Decimal('50'))   # → 50 output
        self.assertEqual(order.picked_output_weight(), Decimal('150'))
        self.assertTrue(order.is_fully_picked())

    def test_not_fully_picked_until_requirement_met(self):
        AllowedCoilSpec.objects.create(
            product_type=self.product_type, size='1.200',
            raw_material_ratio=Decimal('1.000'),
        )
        order = Order.objects.create(customer=self.customer, product_type=self.product_type, quantity=100)
        coil = self._coil(quantity=500, grade='EN8D', size='1.200')
        OrderCoilPick.objects.create(order=order, coil=coil, weight_allocated=Decimal('40'))
        self.assertFalse(order.is_fully_picked())


class ScanCoilForOrderTests(TestCase):
    """The picking hub accepts a scanned/typed coil number and either routes
    to the pick-confirm screen or shows an inline error — the same checks
    the browse list already filters coils by."""

    def setUp(self):
        self.client.post(reverse('employee_login'), {'pin': settings.EMPLOYEE_PIN})
        self.customer = Customer.objects.create(name='Scan Test Co')
        self.product_type = ProductType.objects.create(item_code='Scan Bar', grade='EN8D')
        AllowedCoilSpec.objects.create(product_type=self.product_type, size='1.200')
        self.order = Order.objects.create(
            customer=self.customer, product_type=self.product_type, quantity=100, status='confirmed',
        )

    def test_scanning_formatted_tag_text_redirects_to_pick_screen(self):
        coil = Material.objects.create(quantity=500, grade='EN8D', size='1.200')
        response = self.client.post(
            reverse('select_coil_for_order', kwargs={'order_pk': self.order.pk}),
            {'coil_no': coil.formatted_coil()},
        )
        self.assertRedirects(
            response, reverse('pick_coil_for_order', kwargs={'order_pk': self.order.pk, 'coil_pk': coil.pk}),
        )

    def test_scanning_bare_number_also_works(self):
        coil = Material.objects.create(quantity=500, grade='EN8D', size='1.200')
        response = self.client.post(
            reverse('select_coil_for_order', kwargs={'order_pk': self.order.pk}),
            {'coil_no': str(coil.pk)},
        )
        self.assertRedirects(
            response, reverse('pick_coil_for_order', kwargs={'order_pk': self.order.pk, 'coil_pk': coil.pk}),
        )

    def test_unknown_coil_number_shows_error(self):
        response = self.client.post(
            reverse('select_coil_for_order', kwargs={'order_pk': self.order.pk}),
            {'coil_no': 'COIL9999'},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Coil not found")

    def test_mismatched_spec_coil_shows_error(self):
        coil = Material.objects.create(quantity=500, grade='SAE1008', size='6.000')
        response = self.client.post(
            reverse('select_coil_for_order', kwargs={'order_pk': self.order.pk}),
            {'coil_no': coil.formatted_coil()},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "doesn&#x27;t match")

    def test_archived_coil_shows_error(self):
        coil = Material.objects.create(
            quantity=500, grade='EN8D', size='1.200', archived_at=timezone.now(),
        )
        response = self.client.post(
            reverse('select_coil_for_order', kwargs={'order_pk': self.order.pk}),
            {'coil_no': coil.formatted_coil()},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "archived")

    def test_exhausted_coil_shows_error(self):
        coil = Material.objects.create(quantity=100, grade='EN8D', size='1.200')
        OrderCoilPick.objects.create(order=self.order, coil=coil, weight_allocated=100)
        response = self.client.post(
            reverse('select_coil_for_order', kwargs={'order_pk': self.order.pk}),
            {'coil_no': coil.formatted_coil()},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "no weight remaining")


class PickingPagesShowSizeTests(TestCase):
    """The picking pages show the order's size as 16 x 8, not its product code."""

    def setUp(self):
        session = self.client.session
        session['employee_auth'] = True
        session.save()
        customer = Customer.objects.create(name='Size Co')
        self.code = ProductType.objects.create(item_code='ZZCODE9', grade='EN8D')
        self.order = Order.objects.create(customer=customer, product_type=self.code, grade='EN8D', width=Decimal('16'),
                                          thickness=Decimal('8.0'), quantity=Decimal('500'), status='confirmed')

    def test_size_text_is_width_x_thickness(self):
        self.assertEqual(self.order.size_text(), '16 x 8')
        self.order.thickness = None
        self.assertEqual(self.order.size_text(), '16')

    def test_the_pages_show_the_size_and_not_the_product_code(self):
        for url in (reverse('select_order'), reverse('select_coil_for_order', args=[self.order.pk])):
            page = self.client.get(url).content.decode()
            self.assertIn('16 x 8 mm', page, url)
            self.assertNotIn('ZZCODE9', page, url)


class SizeSpecificCoilSpecTests(TestCase):
    """An allowed coil spec can be made for one ordered size (width x thickness); picking then depends on the
    product type, grade and size together."""

    def setUp(self):
        session = self.client.session
        session['employee_auth'] = True
        session.save()
        customer = Customer.objects.create(name='Spec Co')
        self.code = ProductType.objects.create(item_code='SPEC1', grade='EN8D')
        self.coil_a = Material.objects.create(grade='EN8D', size=Decimal('12'), quantity=Decimal('1000'))
        self.coil_b = Material.objects.create(grade='EN8D', size=Decimal('14'), quantity=Decimal('1000'))
        AllowedCoilSpec.objects.create(product_type=self.code, size=Decimal('12'), raw_material_ratio=Decimal('1.1'),
                                       order_width=Decimal('16'), order_thickness=Decimal('8'))
        AllowedCoilSpec.objects.create(product_type=self.code, size=Decimal('14'), raw_material_ratio=Decimal('1.2'),
                                       order_width=Decimal('20'), order_thickness=Decimal('10'))
        self.customer = customer

    def _order(self, width, thickness):
        return Order.objects.create(customer=self.customer, product_type=self.code, grade='EN8D', width=Decimal(width),
                                    thickness=Decimal(thickness), quantity=Decimal('500'), status='confirmed')

    def test_only_the_spec_for_the_ordered_size_applies(self):
        from ..views.picking import _coil_matches_order_specs, _ratio_for_coil
        small, large = self._order('16', '8'), self._order('20', '10')
        self.assertTrue(_coil_matches_order_specs(self.coil_a, small))
        self.assertFalse(_coil_matches_order_specs(self.coil_b, small))
        self.assertTrue(_coil_matches_order_specs(self.coil_b, large))
        self.assertFalse(_coil_matches_order_specs(self.coil_a, large))
        self.assertEqual(_ratio_for_coil(small, self.coil_a), Decimal('1.1'))
        self.assertEqual(_ratio_for_coil(large, self.coil_b), Decimal('1.2'))

    def test_a_size_with_no_spec_gets_nothing_when_specs_exist_only_for_other_sizes(self):
        from ..views.picking import _coil_matches_order_specs
        other = self._order('30', '5')
        self.assertFalse(_coil_matches_order_specs(self.coil_a, other))
        page = self.client.get(reverse('select_coil_for_order', args=[other.pk])).content.decode()
        self.assertNotIn('COIL%04d' % self.coil_a.pk, page)
        self.assertEqual(other.available_raw_material_output(), Decimal('0'))

    def test_a_size_less_spec_is_the_fallback_for_other_sizes(self):
        from ..views.picking import _coil_matches_order_specs
        AllowedCoilSpec.objects.create(product_type=self.code, size=Decimal('14'))
        other = self._order('30', '5')
        self.assertTrue(_coil_matches_order_specs(self.coil_b, other))
        self.assertFalse(_coil_matches_order_specs(self.coil_a, other))
        self.assertTrue(_coil_matches_order_specs(self.coil_a, self._order('16', '8')))   # its own size-specific spec still wins


class CoilGradeMustMatchOrderTests(TestCase):
    """A raw coil's grade is always the order's grade; the specs only narrow the sizes."""

    def setUp(self):
        session = self.client.session
        session['employee_auth'] = True
        session.save()
        customer = Customer.objects.create(name='Grade Co')
        self.code = ProductType.objects.create(item_code='GRD1', grade='EN8D')
        self.order = Order.objects.create(customer=customer, product_type=self.code, grade='EN8D', quantity=Decimal('100'), status='confirmed')
        self.same = Material.objects.create(grade='EN8D', size=Decimal('12'), quantity=Decimal('500'))
        self.other = Material.objects.create(grade='SS304', size=Decimal('12'), quantity=Decimal('500'))

    def test_only_the_orders_grade_is_offered_with_no_specs_configured(self):
        from ..views.picking import _coil_matches_order_specs
        self.assertTrue(_coil_matches_order_specs(self.same, self.order))
        self.assertFalse(_coil_matches_order_specs(self.other, self.order))
        ids = [c['coil'].pk for c in self.client.get(reverse('select_coil_for_order', args=[self.order.pk])).context['coils']]
        self.assertEqual(ids, [self.same.pk])

    def test_the_grade_check_comes_from_the_order_even_when_a_spec_would_allow_the_size(self):
        from ..views.picking import _coil_matches_order_specs
        AllowedCoilSpec.objects.create(product_type=self.code, size=Decimal('12'))
        self.assertTrue(_coil_matches_order_specs(self.same, self.order))
        self.assertFalse(_coil_matches_order_specs(self.other, self.order))

    def test_stock_count_ignores_other_grades(self):
        self.assertEqual(self.order.available_raw_material_output(), Decimal('500'))

    def test_the_order_grade_wins_over_the_codes_grade(self):
        self.order.grade = 'SS304'
        self.assertEqual(self.order.required_grade(), 'SS304')
        self.order.grade = ''
        self.assertEqual(self.order.required_grade(), 'EN8D')   # falls back to the product code's grade
