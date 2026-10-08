"""Production jobs: step unlock, status rollup, and the scan gate."""

from django.conf import settings
from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from ..models import (
    Customer,
    Material,
    Order,
    OrderCoilPick,
    ProcessStep,
    ProductionJob,
    ProductType,
    StepLog,
)


class JobStepUnlockTests(TestCase):
    """A step can only be advanced once every earlier step is completed."""

    def setUp(self):
        self.client.post(reverse('employee_login'), {'pin': settings.EMPLOYEE_PIN})
        coil = Material.objects.create(quantity=500)
        customer = Customer.objects.create(name='Job Step Unlock Co')
        order = Order.objects.create(customer=customer, quantity=10)
        pick = OrderCoilPick.objects.create(order=order, coil=coil, weight_allocated=10)
        product_type = ProductType.objects.create(item_code='Bar 1.2mm')
        self.step1 = ProcessStep.objects.create(product_type=product_type, name='Cutting', order=1)
        self.step2 = ProcessStep.objects.create(product_type=product_type, name='Heat treat', order=2)
        self.job = ProductionJob.objects.create(pick=pick, product_type=product_type, job_no='JOB-0001', order=order)

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


class JobStatusRollupTests(TestCase):
    """A job's overall status is a rollup of its steps' latest StepLog —
    recalculate_status() is the single place that computes it."""

    def setUp(self):
        coil = Material.objects.create(quantity=500)
        customer = Customer.objects.create(name='Job Status Rollup Co')
        order = Order.objects.create(customer=customer, quantity=10)
        pick = OrderCoilPick.objects.create(order=order, coil=coil, weight_allocated=10)
        self.product_type = ProductType.objects.create(item_code='Bar 1.2mm')
        self.step1 = ProcessStep.objects.create(product_type=self.product_type, name='Cutting', order=1)
        self.step2 = ProcessStep.objects.create(product_type=self.product_type, name='Heat treat', order=2)
        self.job = ProductionJob.objects.create(pick=pick, product_type=self.product_type, job_no='JOB-0001', order=order)

    def test_no_logs_is_pending(self):
        self.job.recalculate_status()
        self.assertEqual(self.job.status, 'pending')

    def test_one_step_in_progress(self):
        StepLog.objects.create(job=self.job, step=self.step1, status='in_progress')
        self.job.recalculate_status()
        self.assertEqual(self.job.status, 'in_progress')

    def test_all_steps_completed(self):
        StepLog.objects.create(job=self.job, step=self.step1, status='completed')
        StepLog.objects.create(job=self.job, step=self.step2, status='completed')
        self.job.recalculate_status()
        self.assertEqual(self.job.status, 'completed')

    def test_one_step_completed_not_all_is_not_marked_completed(self):
        """Only step1 has ever been logged — step2 has no log at all yet.
        Must not read as 'completed' just because the steps that do have
        logs all happen to be completed."""
        StepLog.objects.create(job=self.job, step=self.step1, status='completed')
        self.job.recalculate_status()
        self.assertNotEqual(self.job.status, 'completed')

    def test_failed_step_puts_job_on_hold(self):
        """A step can only be marked 'failed' via the admin/API — the
        employee portal only ever logs in_progress/completed — but wherever
        it comes from, the job must not silently stay pending/in_progress."""
        StepLog.objects.create(job=self.job, step=self.step1, status='completed')
        StepLog.objects.create(job=self.job, step=self.step2, status='failed')
        self.job.recalculate_status()
        self.assertEqual(self.job.status, 'on_hold')

    def test_failed_step_takes_priority_over_completed(self):
        """Even if every step has since been completed, a failed entry
        anywhere in a step's history — with nothing logged after it for that
        step — means that step's *latest* status is still 'failed', and the
        job should stay on hold rather than reading as done."""
        StepLog.objects.create(job=self.job, step=self.step1, status='completed')
        StepLog.objects.create(job=self.job, step=self.step2, status='failed')
        # No re-completion logged for step2 — its latest status is still 'failed'.
        self.job.recalculate_status()
        self.assertEqual(self.job.status, 'on_hold')

    def test_employee_starting_a_step_recalculates_status(self):
        self.client.post(reverse('employee_login'), {'pin': settings.EMPLOYEE_PIN})
        self.client.post(
            reverse('job_detail', kwargs={'pk': self.job.pk}),
            {'step_id': self.step1.pk, 'action': 'start'},
        )
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, 'in_progress')

    def test_employee_completing_the_only_started_step_does_not_read_as_done(self):
        """Only step1 (of two) has a log at all, and it's 'completed' — must
        not roll up to 'completed' just because every step *with* a log
        happens to be completed; step2 was never touched."""
        self.client.post(reverse('employee_login'), {'pin': settings.EMPLOYEE_PIN})
        self.client.post(
            reverse('job_detail', kwargs={'pk': self.job.pk}),
            {'step_id': self.step1.pk, 'action': 'complete'},
        )
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, 'pending')

    def test_admin_marking_a_step_failed_puts_job_on_hold(self):
        staff = User.objects.create_user('joblog_admin', password='pw', is_staff=True, is_superuser=True)
        self.client.force_login(staff)
        response = self.client.post('/admin/materials/steplog/add/', {
            'job': self.job.pk, 'step': self.step1.pk, 'status': 'failed',
            'notes': 'Bent on the die', 'updated_by': staff.pk,
        })
        self.assertEqual(response.status_code, 302)
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, 'on_hold')

    def test_admin_deleting_the_failing_log_recalculates_status(self):
        staff = User.objects.create_user('joblog_admin2', password='pw', is_staff=True, is_superuser=True)
        log = StepLog.objects.create(job=self.job, step=self.step1, status='failed')
        self.job.recalculate_status()
        self.assertEqual(self.job.status, 'on_hold')

        self.client.force_login(staff)
        self.client.post(f'/admin/materials/steplog/{log.pk}/delete/', {'post': 'yes'})
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, 'pending')  # back to no logs at all

    def test_admin_bulk_mark_completed_writes_real_steplogs(self):
        """The bulk action used to be a raw queryset.update(status=...) —
        it left step_logs untouched, so the very next StepLog change
        anywhere on the job (recalculate_status runs on every StepLog
        add/change/delete) would silently revert the status this action
        just set. It must instead write a real completed StepLog per step,
        the same as the employee portal does, so the status sticks."""
        staff = User.objects.create_user('bulk_admin', password='pw', is_staff=True, is_superuser=True)
        self.client.force_login(staff)
        self.client.post('/admin/materials/productionjob/', {
            'action': 'mark_completed', '_selected_action': [self.job.pk],
        })
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, 'completed')
        self.assertTrue(self.job.step_logs.filter(step=self.step1, status='completed').exists())
        self.assertTrue(self.job.step_logs.filter(step=self.step2, status='completed').exists())

        # An unrelated StepLog change elsewhere must not revert this.
        StepLog.objects.create(job=self.job, step=self.step1, status='completed')
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, 'completed')

    def test_admin_bulk_mark_on_hold_writes_a_failed_steplog(self):
        StepLog.objects.create(job=self.job, step=self.step1, status='completed')
        self.job.recalculate_status()
        staff = User.objects.create_user('bulk_admin2', password='pw', is_staff=True, is_superuser=True)
        self.client.force_login(staff)
        self.client.post('/admin/materials/productionjob/', {
            'action': 'mark_on_hold', '_selected_action': [self.job.pk],
        })
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, 'on_hold')
        self.assertTrue(self.job.step_logs.filter(status='failed').exists())

        # recalculate_status derives on_hold from the failed log itself, so
        # it survives an unrelated StepLog change instead of being silently
        # overwritten the next time recalculate_status runs.
        StepLog.objects.create(job=self.job, step=self.step2, status='in_progress')
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, 'on_hold')


class SelectJobForCoilTests(TestCase):
    """Updating a job's progress is gated behind scanning/typing the coil's
    own number — production_board is read-only, this is the only path in."""

    def setUp(self):
        self.client.post(reverse('employee_login'), {'pin': settings.EMPLOYEE_PIN})
        self.customer = Customer.objects.create(name='Job Scan Co')
        self.product_type = ProductType.objects.create(item_code='Job Scan Bar')
        self.order = Order.objects.create(customer=self.customer, quantity=100, status='in_production')

    def _make_job(self, coil, job_no='JOB-0001'):
        pick = OrderCoilPick.objects.create(order=self.order, coil=coil, weight_allocated=100)
        return ProductionJob.objects.create(
            pick=pick, product_type=self.product_type, job_no=job_no, order=self.order,
        )

    def test_scanning_coil_with_one_job_redirects_straight_to_it(self):
        coil = Material.objects.create(quantity=500)
        job = self._make_job(coil)
        response = self.client.post(reverse('select_job_for_coil'), {'coil_no': coil.formatted_coil()})
        self.assertRedirects(response, reverse('job_detail', kwargs={'pk': job.pk}))

    def test_scanning_bare_number_also_works(self):
        coil = Material.objects.create(quantity=500)
        job = self._make_job(coil)
        response = self.client.post(reverse('select_job_for_coil'), {'coil_no': str(coil.pk)})
        self.assertRedirects(response, reverse('job_detail', kwargs={'pk': job.pk}))

    def test_unknown_coil_shows_error(self):
        response = self.client.post(reverse('select_job_for_coil'), {'coil_no': 'COIL9999'})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Coil not found")

    def test_coil_never_picked_shows_error(self):
        coil = Material.objects.create(quantity=500)
        response = self.client.post(reverse('select_job_for_coil'), {'coil_no': coil.formatted_coil()})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "hasn&#x27;t been picked")

    def test_coil_with_multiple_jobs_lists_them_for_selection(self):
        coil = Material.objects.create(quantity=500)
        job1 = self._make_job(coil, job_no='JOB-0001')
        other_order = Order.objects.create(customer=self.customer, quantity=50, status='in_production')
        pick2 = OrderCoilPick.objects.create(order=other_order, coil=coil, weight_allocated=50)
        job2 = ProductionJob.objects.create(
            pick=pick2, product_type=self.product_type, job_no='JOB-0002', order=other_order,
        )
        response = self.client.post(reverse('select_job_for_coil'), {'coil_no': coil.formatted_coil()})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, job1.job_no)
        self.assertContains(response, job2.job_no)


class ProductionBoardScanLinkTests(TestCase):
    def test_scan_to_update_is_a_link_to_the_scan_page(self):
        from ..models import Order, ProcessStep, ProductionJob, StepLog
        self.client.post(reverse('employee_login'), {'pin': settings.EMPLOYEE_PIN})
        customer = Customer.objects.create(name='Board Co')
        code = ProductType.objects.create(item_code='BOARD1', grade='EN8D')
        ProcessStep.objects.create(product_type=code, name='Drawing', order=1)
        order = Order.objects.create(customer=customer, product_type=code, grade='EN8D', quantity=100, status='in_production')
        coil = Material.objects.create(grade='EN8D', quantity=500)
        pick = OrderCoilPick.objects.create(order=order, coil=coil, weight_allocated=100)
        job = ProductionJob.objects.create(pick=pick, order=order, product_type=code)
        step = ProcessStep.objects.get(product_type=code)
        StepLog.objects.create(job=job, step=step, status='pending')
        page = self.client.get(reverse('production_board')).content.decode()
        self.assertIn(f'<a href="{reverse("select_job_for_coil")}" class="btn-update">', page)
