"""Splitting a coil's material into parts that move through the steps on their own."""

from decimal import Decimal

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from .. import parts as parts_service
from ..models import (
    Customer, Material, Order, OrderCoilPick, ProcessStep, ProductionJob, ProductionPart, ProductType, StepLog,
)
from ..parts import PartError


class PartsTestBase(TestCase):
    """A 2000 kg coil in a job whose product code has five steps: Drawing, Cutting (splits), Polishing,
    Cutting again (splits), and Packing (all parts together)."""

    def setUp(self):
        self.customer = Customer.objects.create(name='Parts Co')
        self.code = ProductType.objects.create(item_code='PRT1', grade='EN8D')
        self.drawing = ProcessStep.objects.create(product_type=self.code, name='Drawing', order=1)
        self.cut1 = ProcessStep.objects.create(product_type=self.code, name='Cutting', order=2, splits_material=True)
        self.polish = ProcessStep.objects.create(product_type=self.code, name='Polishing', order=3)
        self.cut2 = ProcessStep.objects.create(product_type=self.code, name='Cutting again', order=4, splits_material=True)
        self.pack = ProcessStep.objects.create(product_type=self.code, name='Packing', order=5, joins_parts=True)
        self.order = Order.objects.create(customer=self.customer, product_type=self.code, grade='EN8D',
                                          quantity=Decimal('1800'), status='in_production')
        self.coil = Material.objects.create(grade='EN8D', quantity=Decimal('2000'))
        self.pick = OrderCoilPick.objects.create(order=self.order, coil=self.coil, weight_allocated=Decimal('2000'))
        self.job = ProductionJob.objects.create(pick=self.pick, product_type=self.code, order=self.order, job_no='JOB-0001')
        self.root = self.job.root_part()

    def complete(self, part, *steps):
        for step in steps:
            parts_service.apply_step_action(part, step, 'complete')

    def split_first(self, weights=('1000', '1000')):
        self.complete(self.root, self.drawing)
        return parts_service.split_part(self.root, self.cut1, [Decimal(w) for w in weights])


class SplitTests(PartsTestBase):
    def test_a_job_starts_as_one_whole_coil_part(self):
        self.assertEqual((self.root.label, self.root.weight, self.root.parent), ('JOB-0001', Decimal('2000.000'), None))
        self.assertEqual([p.label for p in self.job.active_parts()], ['JOB-0001'])

    def test_splitting_makes_labelled_children_that_inherit_the_finished_steps(self):
        a, b = self.split_first()
        self.assertEqual((a.label, b.label), ('JOB-0001-A', 'JOB-0001-B'))
        self.assertEqual((a.weight, b.weight), (Decimal('1000'), Decimal('1000')))
        self.assertEqual(self.root.scrap_weight, Decimal('0'))
        self.root.refresh_from_db()
        self.assertTrue(self.root.is_split())
        self.assertEqual(self.root.split_at_step, self.cut1)
        self.assertEqual(parts_service.completed_step_ids(a), {self.drawing.id, self.cut1.id})
        self.assertEqual([p.label for p in self.job.active_parts()], ['JOB-0001-A', 'JOB-0001-B'])
        # each child is now at the next step, independently
        self.assertEqual(parts_service.unlocked_step_ids(a), {self.drawing.id, self.cut1.id, self.polish.id})

    def test_what_the_parts_do_not_add_up_to_is_recorded_as_scrap(self):
        self.split_first(('950', '950'))
        self.root.refresh_from_db()
        self.assertEqual(self.root.scrap_weight, Decimal('100'))
        self.assertEqual(self.job.scrap_weight(), Decimal('100'))

    def test_a_second_split_labels_the_grandchildren_and_totals_stay_consistent(self):
        a, b = self.split_first()
        self.complete(a, self.polish)
        a1, a2 = parts_service.split_part(a, self.cut2, [Decimal('500'), Decimal('500')])
        self.assertEqual((a1.label, a2.label), ('JOB-0001-A1', 'JOB-0001-A2'))
        self.assertEqual(sorted(p.label for p in self.job.active_parts()), ['JOB-0001-A1', 'JOB-0001-A2', 'JOB-0001-B'])
        self.assertEqual(sum(p.weight for p in self.job.active_parts()), Decimal('2000'))

    def test_splits_that_cannot_happen_are_refused(self):
        self.complete(self.root, self.drawing)
        cases = [
            ([Decimal('1500'), Decimal('1000')], 'more than|only 2000'),
            ([Decimal('2000')], 'between 2 and'),
            ([Decimal('100')] * 11, 'between 2 and'),
        ]
        for weights, _ in cases:
            with self.subTest(weights=len(weights)):
                with self.assertRaises(PartError):
                    parts_service.split_part(self.root, self.cut1, weights)
        with self.assertRaises(PartError):   # a step that doesn't split
            parts_service.split_part(self.root, self.polish, [Decimal('1000'), Decimal('1000')])
        with self.assertRaises(PartError):   # the second splitting step before the first is done
            parts_service.split_part(self.root, self.cut2, [Decimal('1000'), Decimal('1000')])
        self.assertEqual(ProductionPart.objects.filter(job=self.job).count(), 1)   # nothing half-made

    def test_a_part_cannot_be_split_twice_or_worked_on_after_it_was_split(self):
        self.split_first()
        with self.assertRaises(PartError):
            parts_service.split_part(self.root, self.cut1, [Decimal('1000'), Decimal('1000')])
        with self.assertRaises(PartError):
            parts_service.apply_step_action(self.root, self.polish, 'complete')

    def test_weights_are_parsed_from_the_form(self):
        self.assertEqual(parts_service.parse_weights(['1000', ' 999,5 ', '']), [Decimal('1000.000'), Decimal('999.500')])
        for bad in (['abc'], ['0'], ['-5']):
            with self.assertRaises(PartError):
                parts_service.parse_weights(bad)


class ProgressTests(PartsTestBase):
    def test_parts_move_independently_and_the_job_is_done_only_when_every_part_is(self):
        a, b = self.split_first()
        self.complete(a, self.polish)
        self.assertNotIn(self.polish.id, parts_service.completed_step_ids(b))
        self.complete(b, self.polish)
        a1, a2 = parts_service.split_part(a, self.cut2, [Decimal('500'), Decimal('500')])
        b1, b2 = parts_service.split_part(b, self.cut2, [Decimal('500'), Decimal('500')])
        self.assertEqual(len(self.job.active_parts()), 4)
        self.job.refresh_from_db()
        self.assertNotEqual(self.job.status, 'completed')   # four parts, none packed yet

    def test_the_all_parts_together_step_waits_for_every_part_then_completes_for_all(self):
        a, b = self.split_first()
        self.complete(a, self.polish)
        a1, a2 = parts_service.split_part(a, self.cut2, [Decimal('500'), Decimal('500')])
        # B has not even polished yet: packing is refused, naming what it waits on
        with self.assertRaises(PartError) as blocked:
            parts_service.apply_step_action(a1, self.pack, 'complete')
        self.assertIn('JOB-0001-B', str(blocked.exception))
        self.assertEqual(parts_service.joined_step_waiting_on(self.job, self.pack), ['JOB-0001-B'])
        self.complete(b, self.polish)
        b1, b2 = parts_service.split_part(b, self.cut2, [Decimal('500'), Decimal('500')])
        parts_service.apply_step_action(a1, self.pack, 'complete')   # one tap, all four parts
        self.assertEqual(parts_service.joined_step_waiting_on(self.job, self.pack), [])
        for part in (a1, a2, b1, b2):
            self.assertIn(self.pack.id, parts_service.completed_step_ids(part))
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, 'completed')
        self.assertEqual(self.job.finished_weight(), Decimal('2000'))

    def test_a_failed_step_on_any_part_puts_the_whole_job_on_hold(self):
        a, b = self.split_first()
        StepLog.objects.create(job=self.job, part=b, step=self.polish, status='failed')
        self.job.recalculate_status()
        self.assertEqual(self.job.status, 'on_hold')

    def test_a_log_made_without_a_part_belongs_to_the_whole_coil_part(self):
        log = StepLog.objects.create(job=self.job, step=self.drawing, status='completed')
        self.assertEqual(log.part, self.root)


class PartViewTests(PartsTestBase):
    def setUp(self):
        super().setUp()
        self.client.post(reverse('employee_login'), {'pin': settings.EMPLOYEE_PIN})

    def test_an_unsplit_job_shows_the_steps_and_offers_the_split_on_a_splitting_step(self):
        self.complete(self.root, self.drawing)
        page = self.client.get(reverse('job_detail', args=[self.job.pk])).content.decode()
        self.assertIn('splits the material', page)
        self.assertIn('Complete without splitting', page)
        self.assertIn('Complete &amp; split', page)
        self.assertIn('name="action" value="split"', page)

    def test_splitting_from_the_page_makes_the_parts_and_goes_to_their_tags(self):
        self.complete(self.root, self.drawing)
        response = self.client.post(reverse('job_detail', args=[self.job.pk]),
                                    {'step_id': self.cut1.pk, 'action': 'split', 'weight': ['1000', '950']})
        children = list(ProductionPart.objects.filter(parent=self.root).order_by('label'))
        self.assertEqual([c.label for c in children], ['JOB-0001-A', 'JOB-0001-B'])
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse('part_tags', args=[self.job.pk]), response['Location'])
        self.assertIn(f"parts={children[0].pk},{children[1].pk}", response['Location'])
        self.root.refresh_from_db()
        self.assertEqual(self.root.scrap_weight, Decimal('50'))

    def test_a_bad_split_shows_why_and_changes_nothing(self):
        self.complete(self.root, self.drawing)
        response = self.client.post(reverse('job_detail', args=[self.job.pk]),
                                    {'step_id': self.cut1.pk, 'action': 'split', 'weight': ['1500', '1500']})
        self.assertContains(response, 'only 2000')
        self.assertFalse(ProductionPart.objects.filter(parent__isnull=False).exists())

    def test_completing_a_splitting_step_without_splitting_just_completes_it(self):
        self.complete(self.root, self.drawing)
        self.client.post(reverse('job_detail', args=[self.job.pk]), {'step_id': self.cut1.pk, 'action': 'complete'})
        self.assertIn(self.cut1.id, parts_service.completed_step_ids(self.root))
        self.assertFalse(ProductionPart.objects.filter(parent__isnull=False).exists())

    def test_a_split_job_opens_on_an_overview_of_its_parts_with_the_totals(self):
        self.split_first(('1000', '950'))
        page = self.client.get(reverse('job_detail', args=[self.job.pk])).content.decode()
        for text in ('JOB-0001-A', 'JOB-0001-B', 'Scrap at splits', '50.000 kg', 'Print tags for all parts'):
            self.assertIn(text, page)
        self.assertEqual(self.client.get(reverse('part_detail', args=[self.root.pk])).status_code, 200)

    def test_the_tag_page_prints_a_qr_tag_per_new_part(self):
        a, b = self.split_first()
        page = self.client.get(reverse('part_tags', args=[self.job.pk]), {'parts': f'{a.pk},{b.pk}'}).content.decode()
        self.assertEqual(page.count('class="tag-qr"'), 2)
        for text in ('JOB-0001-A', 'JOB-0001-B', '1000.000 kg', 'COIL', 'Parts Co'):
            self.assertIn(text, page)
        every = self.client.get(reverse('part_tags', args=[self.job.pk])).content.decode()   # no ?parts: all active parts
        self.assertEqual(every.count('class="tag-qr"'), 2)

    def test_staff_can_open_the_tags_without_the_pin(self):
        self.split_first()
        staff_client = self.client_class()
        get_user_model().objects.create_user('tagger', password='pw', is_staff=True)
        staff_client.login(username='tagger', password='pw')
        self.assertEqual(staff_client.get(reverse('part_tags', args=[self.job.pk])).status_code, 200)

    def test_scanning_a_part_tag_opens_that_part(self):
        a, _ = self.split_first()
        response = self.client.post(reverse('select_job_for_coil'), {'coil_no': 'job-0001-a'})
        self.assertRedirects(response, reverse('part_detail', args=[a.pk]))

    def test_scanning_the_coil_of_a_split_job_opens_the_overview(self):
        self.split_first()
        response = self.client.post(reverse('select_job_for_coil'), {'coil_no': self.coil.formatted_coil()})
        self.assertRedirects(response, reverse('job_detail', args=[self.job.pk]), fetch_redirect_response=False)
        self.assertContains(self.client.get(response['Location']), 'Parts being worked on')

    def test_an_all_parts_together_step_shows_who_it_waits_on(self):
        a, b = self.split_first()
        self.complete(a, self.polish)
        a1, _ = parts_service.split_part(a, self.cut2, [Decimal('500'), Decimal('500')])
        page = self.client.get(reverse('part_detail', args=[a1.pk])).content.decode()
        self.assertIn('Waiting for JOB-0001-B', page)

    def test_the_board_lists_each_part_of_a_split_job(self):
        self.split_first()
        page = self.client.get(reverse('production_board')).content.decode()
        self.assertIn('JOB-0001-A', page)
        self.assertIn('JOB-0001-B', page)

    def test_picking_a_coil_creates_the_whole_coil_part_with_its_steps(self):
        order = Order.objects.create(customer=self.customer, product_type=self.code, grade='EN8D', quantity=Decimal('100'), status='confirmed')
        coil = Material.objects.create(grade='EN8D', quantity=Decimal('500'))
        self.client.post(reverse('pick_coil_for_order', args=[order.pk, coil.pk]), {'weight_allocated': '300'})
        job = ProductionJob.objects.get(order=order)
        root = job.parts.get()
        self.assertEqual((root.label, root.weight, root.parent), (job.job_no, Decimal('300.000'), None))
        self.assertEqual(root.step_logs.count(), 5)


class PartsAdminAndApiTests(PartsTestBase):
    def test_admin_shows_the_step_flags_the_parts_and_the_api_lists_them(self):
        get_user_model().objects.create_superuser('root', 'r@example.com', 'pw')
        self.client.login(username='root', password='pw')
        change = self.client.get(reverse('admin:materials_producttype_change', args=[self.code.pk])).content.decode()
        self.assertIn('splits_material', change)
        self.assertIn('joins_parts', change)
        self.split_first()
        job_page = self.client.get(reverse('admin:materials_productionjob_change', args=[self.job.pk])).content.decode()
        self.assertIn('JOB-0001-A', job_page)
        data = self.client.get('/api/jobs/').json()
        results = data['results'] if isinstance(data, dict) else data
        labels = {part['label'] for part in results[0]['parts']}
        self.assertEqual(labels, {'JOB-0001', 'JOB-0001-A', 'JOB-0001-B'})

    def test_admin_bulk_complete_finishes_every_part(self):
        self.split_first()
        self.job.refresh_from_db()
        admin_user = get_user_model().objects.create_superuser('root2', 'r2@example.com', 'pw')
        self.client.force_login(admin_user)
        self.client.post(reverse('admin:materials_productionjob_changelist'),
                         {'action': 'mark_completed', '_selected_action': [self.job.pk]})
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, 'completed')
