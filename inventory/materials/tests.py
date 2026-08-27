from django.conf import settings
from django.contrib.auth.models import User
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from .forms import MaterialForm
from .models import (
    CoilPart, Customer, GradeOption, Material, Order, ProcessStep,
    ProductionJob, ProductType, SizeOption,
)


class OrderApiTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.staff = User.objects.create_user('api_staff', password='pw', is_staff=True)
        self.customer = Customer.objects.create(name='Acme Corp')
        self.product_type = ProductType.objects.create(name='Bar', grade='EN8D', size='1.200')
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


class CoilApiTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.staff = User.objects.create_user('api_staff2', password='pw', is_staff=True)

    def test_remaining_filter_excludes_exhausted_coils_and_keeps_untouched_ones(self):
        untouched = Material.objects.create(quantity=500, grade='EN8D', size='1.2')
        exhausted = Material.objects.create(quantity=100, grade='EN8D', size='1.2')
        CoilPart.objects.create(coil=exhausted, part_no='EX-A', weight=100)

        self.client.force_authenticate(user=self.staff)
        response = self.client.get('/api/coils/?remaining=true')
        ids = [row['coil_no'] for row in response.data['results']]
        self.assertIn(untouched.pk, ids)
        self.assertNotIn(exhausted.pk, ids)


class ProductTypeUniquenessTests(TestCase):
    """A grade/size combination identifies exactly one product type."""

    def test_duplicate_grade_and_size_rejected(self):
        ProductType.objects.create(name='Bar A', grade='EN8D', size='1.200')
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ProductType.objects.create(name='Bar B', grade='EN8D', size='1.200')

    def test_same_grade_different_size_allowed(self):
        ProductType.objects.create(name='Bar A', grade='EN8D', size='1.200')
        ProductType.objects.create(name='Bar B', grade='EN8D', size='1.500')
        self.assertEqual(ProductType.objects.filter(grade='EN8D').count(), 2)


class MaterialFormValidationTests(TestCase):
    """Grade/size must come from the admin-curated lists, even on a raw POST."""

    def setUp(self):
        GradeOption.objects.get_or_create(name='EN8D')
        SizeOption.objects.get_or_create(value='1.200')

    def base_data(self, **overrides):
        data = {
            'date': '2026-07-06', 'grade': 'EN8D', 'size': '1.200',
            'company': 'Tata Steel', 'vendor': 'ABC Traders',
            'quantity': '500.000', 'heat_no': 'H001',
        }
        data.update(overrides)
        return data

    def test_grade_not_in_gradeoption_rejected(self):
        form = MaterialForm(self.base_data(grade='MADE-UP'))
        self.assertFalse(form.is_valid())
        self.assertIn('grade', form.errors)

    def test_size_not_in_sizeoption_rejected(self):
        form = MaterialForm(self.base_data(size='9.999'))
        self.assertFalse(form.is_valid())
        self.assertIn('size', form.errors)

    def test_known_grade_and_size_accepted(self):
        form = MaterialForm(self.base_data())
        self.assertTrue(form.is_valid(), form.errors)


class MaterialFormViewErrorDisplayTests(TestCase):
    """A rejected submission must show why, and not force the employee to retype everything."""

    def setUp(self):
        GradeOption.objects.get_or_create(name='EN8D')
        SizeOption.objects.get_or_create(value='1.200')
        self.client.post(reverse('employee_login'), {'pin': settings.EMPLOYEE_PIN})

    def test_invalid_grade_shows_error_and_repopulates_fields(self):
        response = self.client.post(reverse('material_form'), {
            'date': '2026-07-06', 'grade': 'MADE-UP', 'size': '1.200',
            'company': 'Tata Steel', 'vendor': 'ABC Traders',
            'quantity': '500.000', 'heat_no': 'H001',
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Select a grade from the list.')
        self.assertContains(response, 'H001')
        self.assertContains(response, 'Tata Steel')
        self.assertEqual(Material.objects.count(), 0)


class EmployeeLogoutTests(TestCase):
    def test_post_clears_session_and_requires_relogin(self):
        self.client.post(reverse('employee_login'), {'pin': settings.EMPLOYEE_PIN})
        self.client.post(reverse('employee_logout'))
        response = self.client.get(reverse('employee'))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse('employee_login'), response.url)

    def test_get_does_not_log_out(self):
        self.client.post(reverse('employee_login'), {'pin': settings.EMPLOYEE_PIN})
        self.client.get(reverse('employee_logout'))
        response = self.client.get(reverse('employee'))
        self.assertEqual(response.status_code, 200)


class EmployeeLoginRedirectTests(TestCase):
    """The `next` param must never send an authenticated session off-site."""

    def test_offsite_next_is_ignored(self):
        response = self.client.post(
            reverse('employee_login'),
            {'pin': settings.EMPLOYEE_PIN, 'next': 'https://evil.example.com/phish'},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse('employee'))

    def test_internal_next_is_followed(self):
        response = self.client.post(
            reverse('employee_login'),
            {'pin': settings.EMPLOYEE_PIN, 'next': reverse('material_form')},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse('material_form'))


class CoilPartsCreationTests(TestCase):
    """Creating a part must be all-or-nothing: never a CoilPart with no job."""

    def setUp(self):
        self.client.post(reverse('employee_login'), {'pin': settings.EMPLOYEE_PIN})
        self.coil = Material.objects.create(
            date='2026-07-01', grade='EN8D', size='1.2',
            company='Tata Steel', vendor='ABC Traders', quantity=500, heat_no='H001',
        )
        self.product_type = ProductType.objects.create(name='Bar 1.2mm', grade='EN8D', size='1.2')
        ProcessStep.objects.create(product_type=self.product_type, name='Cutting', order=1)
        ProcessStep.objects.create(product_type=self.product_type, name='Heat treat', order=2)

    def test_invalid_product_type_creates_nothing(self):
        response = self.client.post(
            reverse('coil_parts', kwargs={'coil_pk': self.coil.pk}),
            {'suffix': 'A', 'weight': '10', 'product_type': '9999'},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(CoilPart.objects.filter(coil=self.coil).count(), 0)

    def test_valid_product_type_creates_part_and_job(self):
        response = self.client.post(
            reverse('coil_parts', kwargs={'coil_pk': self.coil.pk}),
            {'suffix': 'A', 'weight': '10', 'product_type': str(self.product_type.pk)},
        )
        self.assertEqual(response.status_code, 302)
        part = CoilPart.objects.get(coil=self.coil)
        job = ProductionJob.objects.get(part=part)
        self.assertEqual(job.step_logs.count(), 2)

    def test_empty_product_type_shows_error_instead_of_crashing(self):
        """An unselected <select> submits product_type='' — must not raise ValueError."""
        response = self.client.post(
            reverse('coil_parts', kwargs={'coil_pk': self.coil.pk}),
            {'suffix': 'A', 'weight': '10', 'product_type': ''},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Please select a valid product type.")
        self.assertEqual(CoilPart.objects.filter(coil=self.coil).count(), 0)

    def test_non_numeric_weight_shows_error_instead_of_crashing(self):
        response = self.client.post(
            reverse('coil_parts', kwargs={'coil_pk': self.coil.pk}),
            {'suffix': 'A', 'weight': 'not-a-number', 'product_type': str(self.product_type.pk)},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Weight must be a number.")
        self.assertEqual(CoilPart.objects.filter(coil=self.coil).count(), 0)


class JobStepUnlockTests(TestCase):
    """A step can only be advanced once every earlier step is completed."""

    def setUp(self):
        self.client.post(reverse('employee_login'), {'pin': settings.EMPLOYEE_PIN})
        coil = Material.objects.create(quantity=500)
        part = CoilPart.objects.create(coil=coil, part_no='COIL0001-A', weight=10)
        product_type = ProductType.objects.create(name='Bar 1.2mm')
        self.step1 = ProcessStep.objects.create(product_type=product_type, name='Cutting', order=1)
        self.step2 = ProcessStep.objects.create(product_type=product_type, name='Heat treat', order=2)
        self.job = ProductionJob.objects.create(part=part, product_type=product_type, job_no='JOB-0001')

    def test_cannot_complete_step2_before_step1(self):
        self.client.post(
            reverse('job_detail', kwargs={'pk': self.job.pk}),
            {'step_id': self.step2.pk, 'action': 'complete'},
        )
        self.assertFalse(self.job.step_logs.filter(step=self.step2).exists())

    def test_unknown_action_is_ignored(self):
        response = self.client.post(
            reverse('job_detail', kwargs={'pk': self.job.pk}),
            {'step_id': self.step1.pk, 'action': 'delete-everything'},
        )
        self.assertEqual(response.status_code, 302)
        self.assertFalse(self.job.step_logs.filter(step=self.step1).exists())

    def test_malformed_step_id_does_not_crash(self):
        response = self.client.post(
            reverse('job_detail', kwargs={'pk': self.job.pk}),
            {'step_id': 'not-a-number', 'action': 'start'},
        )
        self.assertEqual(response.status_code, 302)


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
        product_type = ProductType.objects.create(name='Bar', grade='EN8D', size='1.200')
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
