"""The read-only REST API under /api/."""

from decimal import Decimal
from django.contrib.auth.models import User
from django.test import TestCase
from rest_framework.test import APIClient

from ..models import Customer, Material, Order, OrderCoilPick, ProcessStep, ProductionJob, ProductType


class OrderApiTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.staff = User.objects.create_user('api_staff', password='pw', is_staff=True)
        self.customer = Customer.objects.create(name='Acme Corp')
        self.product_type = ProductType.objects.create(item_code='Bar', grade='EN8D', size='1.200')
        self.order = Order.objects.create(
            customer=self.customer, product_type=self.product_type,
            quantity=250, status='in_production',
        )

    def test_anonymous_request_is_rejected(self):
        response = self.client.get('/api/orders/')
        self.assertEqual(response.status_code, 403)

    def test_non_staff_request_is_rejected(self):
        non_staff = User.objects.create_user('nobody', password='pw')
        self.client.force_authenticate(user=non_staff)
        response = self.client.get('/api/orders/')
        self.assertEqual(response.status_code, 403)

    def test_staff_can_list_orders(self):
        self.client.force_authenticate(user=self.staff)
        response = self.client.get('/api/orders/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['results'][0]['customer_name'], 'Acme Corp')

    def test_status_filter(self):
        Order.objects.create(customer=self.customer, quantity=10, status='pending')
        self.client.force_authenticate(user=self.staff)
        response = self.client.get('/api/orders/?status=in_production')
        ids = [row['id'] for row in response.data['results']]
        self.assertEqual(ids, [self.order.pk])

    def test_write_endpoints_do_not_exist(self):
        """This API is deliberately read-only — state changes go through the guarded web views."""
        self.client.force_authenticate(user=self.staff)
        response = self.client.post('/api/orders/', {'quantity': 5}, format='json')
        self.assertEqual(response.status_code, 405)

    def test_weight_cut_does_not_grow_query_count_with_more_orders(self):
        """weight_cut used to run a fresh aggregate per order (N+1) — the
        viewset now annotates it on the queryset instead. Query count for the
        list endpoint should stay flat as the number of orders grows."""
        job_product_type = ProductType.objects.create(item_code='Jobbed', grade='EN8D', size='2.5')
        for i in range(5):
            order = Order.objects.create(customer=self.customer, quantity=10, status='pending')
            coil = Material.objects.create(quantity=50)
            pick = OrderCoilPick.objects.create(order=order, coil=coil, weight_allocated=20)
            ProductionJob.objects.create(
                pick=pick, product_type=job_product_type, job_no=f'QJOB-{i}', order=order,
            )
        self.client.force_authenticate(user=self.staff)

        with self.assertNumQueries(2):  # pagination count + the annotated list query
            response = self.client.get('/api/orders/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data['results']), 6)  # 5 new + the one from setUp


class CoilApiTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.staff = User.objects.create_user('api_staff2', password='pw', is_staff=True)

    def test_remaining_filter_excludes_exhausted_coils_and_keeps_untouched_ones(self):
        customer = Customer.objects.create(name='Coil Api Co')
        order = Order.objects.create(customer=customer, quantity=100)
        untouched = Material.objects.create(quantity=500, grade='EN8D', size='1.2')
        exhausted = Material.objects.create(quantity=100, grade='EN8D', size='1.2')
        OrderCoilPick.objects.create(order=order, coil=exhausted, weight_allocated=100)

        self.client.force_authenticate(user=self.staff)
        response = self.client.get('/api/coils/?remaining=true')
        self.assertEqual(response.status_code, 200)
        ids = [row['coil_no'] for row in response.data['results']]
        self.assertIn(untouched.pk, ids)
        self.assertNotIn(exhausted.pk, ids)

    def test_weight_used_does_not_grow_query_count_with_more_coils(self):
        """weight_used()/weight_remaining() used to run a fresh aggregate per
        coil (N+1) even though the viewset prefetches order_picks —
        .aggregate() bypasses the prefetch cache. weight_used() now sums over
        the prefetched rows instead, so query count stays flat as coils grow."""
        customer = Customer.objects.create(name='Coil Api Co 2')
        order = Order.objects.create(customer=customer, quantity=100)
        for i in range(5):
            coil = Material.objects.create(quantity=100, heat_no=f'QCOUNT{i}')
            OrderCoilPick.objects.create(order=order, coil=coil, weight_allocated=30)
        self.client.force_authenticate(user=self.staff)

        with self.assertNumQueries(3):  # pagination count + the list query + one prefetch of all picks
            response = self.client.get('/api/coils/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data['results']), 5)


class ProductTypeAndJobApiTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.staff = User.objects.create_user('api_staff3', password='pw', is_staff=True)
        self.product_type = ProductType.objects.create(item_code='API Bar', grade='EN8D', size='1.200')
        ProcessStep.objects.create(product_type=self.product_type, name='Cutting', order=1)
        coil = Material.objects.create(quantity=500)
        self.order = Order.objects.create(
            customer=Customer.objects.create(name='API Job Co'), quantity=100, status='in_production',
        )
        self.pick = OrderCoilPick.objects.create(order=self.order, coil=coil, weight_allocated=50)
        self.job = ProductionJob.objects.create(
            pick=self.pick, product_type=self.product_type, job_no='API-JOB-0001',
            order=self.order, status='in_progress',
        )

    def test_product_type_list_includes_steps(self):
        self.client.force_authenticate(user=self.staff)
        response = self.client.get('/api/product-types/')
        self.assertEqual(response.status_code, 200)
        row = next(r for r in response.data['results'] if r['id'] == self.product_type.pk)
        self.assertEqual(row['steps'][0]['name'], 'Cutting')

    def test_job_status_filter(self):
        other_pick = OrderCoilPick.objects.create(order=self.order, coil=self.pick.coil, weight_allocated=50)
        other = ProductionJob.objects.create(
            pick=other_pick, product_type=self.product_type, job_no='API-JOB-0002',
            order=self.order, status='completed',
        )
        self.client.force_authenticate(user=self.staff)
        response = self.client.get('/api/jobs/?status=completed')
        ids = [row['id'] for row in response.data['results']]
        self.assertEqual(ids, [other.pk])

    def test_job_order_filter(self):
        other_order = Order.objects.create(
            customer=self.order.customer, quantity=10, status='in_production',
        )
        other_pick = OrderCoilPick.objects.create(order=other_order, coil=self.pick.coil, weight_allocated=50)
        ProductionJob.objects.create(
            pick=other_pick, product_type=self.product_type, job_no='API-JOB-0003', order=other_order,
        )
        self.client.force_authenticate(user=self.staff)
        response = self.client.get(f'/api/jobs/?order={self.order.pk}')
        ids = [row['id'] for row in response.data['results']]
        self.assertEqual(ids, [self.job.pk])

    def test_job_serializer_includes_coil_and_weight_info(self):
        self.client.force_authenticate(user=self.staff)
        response = self.client.get(f'/api/jobs/{self.job.pk}/')
        self.assertEqual(response.data['coil_no'], self.pick.coil.formatted_coil())
        self.assertEqual(Decimal(response.data['weight_allocated']), Decimal('50.000'))
