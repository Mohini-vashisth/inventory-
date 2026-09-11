import tempfile
from decimal import Decimal
from unittest.mock import patch

import pandas as pd
from django.conf import settings
from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.core import mail
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from .forms import MaterialForm
from .models import (
    AllowedCoilSpec, CoilPart, Customer, GradeOption, Material, Order, ProcessStep,
    ProductionJob, ProductType, SizeOption, StepLog,
)


class ImportExcelTests(TestCase):
    """Uses a small synthetic spreadsheet rather than the real (gitignored) one,
    so this runs the same in CI as it does locally."""

    def _write_sheet(self, rows, extra_columns=None):
        columns = [
            'SR. NO.', 'COIL NO.', 'DATE', 'GRADE', 'SIZE', 'COMPANY', 'VENDOR',
            'QTY (KGS)', 'HEAT NO.',
        ]
        if extra_columns:
            columns += extra_columns
        df = pd.DataFrame(rows, columns=columns)
        tmp = tempfile.NamedTemporaryFile(suffix='.xlsx', delete=False)
        df.to_excel(tmp.name, index=False)
        return tmp.name

    def test_imports_real_rows_and_skips_blank_ones(self):
        path = self._write_sheet([
            [1, 'WR0001', '2024-01-15', 'SAE 1008', 6, 'VSP', 'ADITYA STEEL', 1250.0, 'H001'],
            [2, None, None, None, None, None, None, None, None],  # blank template row
            # unit-suffixed size, oversized heat_no (9 chars, model max is 8)
            [3, 'WR0003', '2024-02-01', 'EN8D', '16.3 MM', 'TATA', 'XYZ TRADERS', 500.0, 'B30855015'],
        ])
        call_command('import_excel', f'--file={path}', '--yes')

        self.assertEqual(Material.objects.count(), 2)
        first = Material.objects.order_by('coil_no').first()
        self.assertEqual(first.grade, 'SAE 1008')
        self.assertEqual(first.quantity, 1250)

        second = Material.objects.order_by('coil_no').last()
        self.assertEqual(second.size, Decimal('16.3'))  # ' MM' suffix stripped
        self.assertEqual(second.heat_no, 'B3085501')  # truncated to 8 chars

    def test_malformed_date_does_not_crash_the_whole_import(self):
        """Real data had a typo like '11/058/2023' (no such day) that used to
        crash the entire import partway through, leaving a partial DB state."""
        path = self._write_sheet([
            [1, 'WR0001', '11/058/2023', 'SAE 1008', 6, 'VSP', 'ADITYA STEEL', 1250.0, 'H001'],
            [2, 'WR0002', '2024-02-01', 'EN8D', 6, 'TATA', 'XYZ TRADERS', 500.0, 'H002'],
        ])
        call_command('import_excel', f'--file={path}', '--yes')

        self.assertEqual(Material.objects.count(), 2)
        bad_row = Material.objects.get(heat_no='H001')
        self.assertIsNone(bad_row.date)
        good_row = Material.objects.get(heat_no='H002')
        self.assertIsNotNone(good_row.date)

    def test_crash_partway_through_leaves_no_partial_data(self):
        """The whole delete+import runs in one transaction — a failure partway
        through must roll back completely, not leave some rows imported."""
        Material.objects.create(grade='EXISTING', quantity=1)
        path = self._write_sheet([
            [1, 'WR0001', '2024-01-15', 'SAE 1008', 6, 'VSP', 'ADITYA STEEL', 1250.0, 'H001'],
        ])
        with patch(
            'materials.management.commands.import_excel.Command._clean_decimal',
            side_effect=RuntimeError('simulated failure'),
        ):
            with self.assertRaises(RuntimeError):
                call_command('import_excel', f'--file={path}', '--yes')

        # Rolled back to exactly the pre-import state — the old row is still there.
        self.assertEqual(Material.objects.count(), 1)
        self.assertEqual(Material.objects.first().grade, 'EXISTING')

    def test_dry_run_touches_nothing(self):
        path = self._write_sheet([
            [1, 'WR0001', '2024-01-15', 'SAE 1008', 6, 'VSP', 'ADITYA STEEL', 1250.0, 'H001'],
        ])
        call_command('import_excel', f'--file={path}', '--dry-run')
        self.assertEqual(Material.objects.count(), 0)

    def test_prompts_before_deleting_existing_rows(self):
        Material.objects.create(grade='OLD', quantity=1)
        path = self._write_sheet([
            [1, 'WR0001', '2024-01-15', 'SAE 1008', 6, 'VSP', 'ADITYA STEEL', 1250.0, 'H001'],
        ])
        # Simulate answering "no" at the confirmation prompt.
        with patch('builtins.input', return_value='n'):
            call_command('import_excel', f'--file={path}')
        self.assertEqual(Material.objects.count(), 1)
        self.assertEqual(Material.objects.first().grade, 'OLD')

    def test_issued_qty_columns_become_legacy_used_weight(self):
        """ISSUED QTY 1/2/3 track weight already used before the app existed —
        summed into legacy_used_weight so status reflects real-world usage."""
        path = self._write_sheet(
            [
                [1, 'WR0001', '2024-01-15', 'SAE 1008', 6, 'VSP', 'ADITYA STEEL',
                 1250.0, 'H001', 500.0, 250.0, None],
                [2, 'WR0002', '2024-02-01', 'EN8D', 6, 'TATA', 'XYZ TRADERS',
                 500.0, 'H002', None, None, None],
            ],
            extra_columns=['ISSUED QTY 1', 'ISSUED QTY 2', 'ISSUED QTY 3'],
        )
        call_command('import_excel', f'--file={path}', '--yes')

        used = Material.objects.get(heat_no='H001')
        self.assertEqual(used.legacy_used_weight, Decimal('750'))
        self.assertEqual(used.weight_used(), Decimal('750'))

        untouched = Material.objects.get(heat_no='H002')
        self.assertEqual(untouched.legacy_used_weight, Decimal('0'))

    def test_reset_sequence_renumbers_from_one(self):
        """Without --reset-sequence, coil_no keeps counting up from wherever
        deleted rows left off (SQLite doesn't rewind AUTOINCREMENT on delete).
        With it, the next imported coil starts at 1."""
        old = Material.objects.create(grade='OLD', quantity=1)
        old.delete()  # pushes SQLite's autoincrement counter past 1
        path = self._write_sheet([
            [1, 'WR0001', '2024-01-15', 'SAE 1008', 6, 'VSP', 'ADITYA STEEL', 1250.0, 'H001'],
        ])
        call_command('import_excel', f'--file={path}', '--yes', '--reset-sequence')
        self.assertEqual(Material.objects.get().coil_no, 1)

    def _write_multi_sheet(self):
        tmp = tempfile.NamedTemporaryFile(suffix='.xlsx', delete=False)
        with pd.ExcelWriter(tmp.name) as writer:
            pd.DataFrame({'Notes': ['not real data']}).to_excel(writer, sheet_name='Cover Page', index=False)
            pd.DataFrame({
                'SR. NO.': [1], 'COIL NO.': ['WR0001'], 'DATE': ['2024-01-15'],
                'GRADE': ['SAE 1008'], 'SIZE': [6], 'COMPANY': ['VSP'],
                'VENDOR': ['ADITYA STEEL'], 'QTY (KGS)': [1250.0], 'HEAT NO.': ['H001'],
            }).to_excel(writer, sheet_name='Stock 2024', index=False)
        return tmp.name

    def test_list_sheets_imports_nothing(self):
        path = self._write_multi_sheet()
        call_command('import_excel', f'--file={path}', '--list-sheets')
        self.assertEqual(Material.objects.count(), 0)

    def test_wrong_default_sheet_is_rejected_clearly(self):
        path = self._write_multi_sheet()
        with self.assertRaises(CommandError):
            call_command('import_excel', f'--file={path}', '--dry-run')

    def test_can_target_sheet_by_name_or_index(self):
        path = self._write_multi_sheet()
        call_command('import_excel', f'--file={path}', '--sheet=Stock 2024', '--yes')
        self.assertEqual(Material.objects.count(), 1)

        Material.objects.all().delete()
        call_command('import_excel', f'--file={path}', '--sheet=1', '--yes')
        self.assertEqual(Material.objects.count(), 1)


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

    def test_weight_cut_does_not_grow_query_count_with_more_orders(self):
        """weight_cut used to run a fresh aggregate per order (N+1) — the
        viewset now annotates it on the queryset instead. Query count for the
        list endpoint should stay flat as the number of orders grows."""
        job_product_type = ProductType.objects.create(name='Jobbed', grade='EN8D', size='2.5')
        for i in range(5):
            order = Order.objects.create(customer=self.customer, quantity=10, status='pending')
            coil = Material.objects.create(quantity=50)
            part = CoilPart.objects.create(coil=coil, part_no=f'QCOUNT-{i}', weight=20)
            ProductionJob.objects.create(
                part=part, product_type=job_product_type, job_no=f'QJOB-{i}', order=order,
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
        untouched = Material.objects.create(quantity=500, grade='EN8D', size='1.2')
        exhausted = Material.objects.create(quantity=100, grade='EN8D', size='1.2')
        CoilPart.objects.create(coil=exhausted, part_no='EX-A', weight=100)

        self.client.force_authenticate(user=self.staff)
        response = self.client.get('/api/coils/?remaining=true')
        self.assertEqual(response.status_code, 200)
        ids = [row['coil_no'] for row in response.data['results']]
        self.assertIn(untouched.pk, ids)
        self.assertNotIn(exhausted.pk, ids)

    def test_weight_used_does_not_grow_query_count_with_more_coils(self):
        """weight_used()/weight_remaining() used to run a fresh aggregate per
        coil (N+1) even though the viewset prefetches parts — .aggregate()
        bypasses the prefetch cache. weight_used() now sums over the
        prefetched rows instead, so query count stays flat as coils grow."""
        for i in range(5):
            coil = Material.objects.create(quantity=100, heat_no=f'QCOUNT{i}')
            CoilPart.objects.create(coil=coil, part_no=f'QCOUNT-{i}-A', weight=30)
        self.client.force_authenticate(user=self.staff)

        with self.assertNumQueries(3):  # pagination count + the list query + one prefetch of all parts
            response = self.client.get('/api/coils/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data['results']), 5)


class ProductTypeAndJobApiTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.staff = User.objects.create_user('api_staff3', password='pw', is_staff=True)
        self.product_type = ProductType.objects.create(name='API Bar', grade='EN8D', size='1.200')
        ProcessStep.objects.create(product_type=self.product_type, name='Cutting', order=1)
        coil = Material.objects.create(quantity=500)
        self.order = Order.objects.create(
            customer=Customer.objects.create(name='API Job Co'), quantity=100, status='in_production',
        )
        self.part = CoilPart.objects.create(coil=coil, part_no='APIJOB-A', weight=50)
        self.job = ProductionJob.objects.create(
            part=self.part, product_type=self.product_type, job_no='API-JOB-0001',
            order=self.order, status='in_progress',
        )

    def test_product_type_list_includes_steps(self):
        self.client.force_authenticate(user=self.staff)
        response = self.client.get('/api/product-types/')
        self.assertEqual(response.status_code, 200)
        row = next(r for r in response.data['results'] if r['id'] == self.product_type.pk)
        self.assertEqual(row['steps'][0]['name'], 'Cutting')

    def test_job_status_filter(self):
        other_job_part = CoilPart.objects.create(coil=self.part.coil, part_no='APIJOB-B', weight=50)
        other = ProductionJob.objects.create(
            part=other_job_part, product_type=self.product_type, job_no='API-JOB-0002', status='completed',
        )
        self.client.force_authenticate(user=self.staff)
        response = self.client.get('/api/jobs/?status=completed')
        ids = [row['id'] for row in response.data['results']]
        self.assertEqual(ids, [other.pk])

    def test_job_order_filter(self):
        other_order = Order.objects.create(
            customer=self.order.customer, quantity=10, status='in_production',
        )
        other_job_part = CoilPart.objects.create(coil=self.part.coil, part_no='APIJOB-C', weight=50)
        ProductionJob.objects.create(
            part=other_job_part, product_type=self.product_type, job_no='API-JOB-0003', order=other_order,
        )
        self.client.force_authenticate(user=self.staff)
        response = self.client.get(f'/api/jobs/?order={self.order.pk}')
        ids = [row['id'] for row in response.data['results']]
        self.assertEqual(ids, [self.job.pk])

    def test_job_serializer_includes_coil_and_part_info(self):
        self.client.force_authenticate(user=self.staff)
        response = self.client.get(f'/api/jobs/{self.job.pk}/')
        self.assertEqual(response.data['part_no'], 'APIJOB-A')
        self.assertEqual(response.data['coil_no'], self.part.coil.formatted_coil())


class MaterialUsedStatusTests(TestCase):
    """A coil is 'used' once every kg of it has been cut into parts."""

    def test_untouched_coil_is_unused(self):
        coil = Material.objects.create(quantity=500)
        self.assertFalse(coil.is_used_up())
        self.assertEqual(coil.weight_used(), 0)
        self.assertEqual(coil.weight_remaining(), 500)

    def test_partially_cut_coil_is_still_unused(self):
        coil = Material.objects.create(quantity=500)
        CoilPart.objects.create(coil=coil, part_no='PARTIAL-A', weight=200)
        self.assertFalse(coil.is_used_up())
        self.assertEqual(coil.weight_remaining(), 300)

    def test_fully_cut_coil_is_used(self):
        coil = Material.objects.create(quantity=500)
        CoilPart.objects.create(coil=coil, part_no='FULL-A', weight=300)
        CoilPart.objects.create(coil=coil, part_no='FULL-B', weight=200)
        self.assertTrue(coil.is_used_up())
        self.assertEqual(coil.weight_remaining(), 0)

    def test_legacy_used_weight_counts_toward_usage(self):
        """Usage recorded before this coil was tracked in the app (imported
        from the spreadsheet's ISSUED QTY columns) counts the same as weight
        cut through the app."""
        coil = Material.objects.create(quantity=500, legacy_used_weight=200)
        self.assertEqual(coil.weight_used(), 200)
        self.assertEqual(coil.weight_remaining(), 300)
        self.assertFalse(coil.is_used_up())

        CoilPart.objects.create(coil=coil, part_no='LEGACY-A', weight=300)
        self.assertEqual(coil.weight_used(), 500)
        self.assertTrue(coil.is_used_up())

    def test_coil_with_no_quantity_on_file_is_not_marked_used(self):
        """No quantity means unknown, not used — mirrors the existing
        'exhausted' check elsewhere in the app (coil_parts view)."""
        coil = Material.objects.create(quantity=None)
        self.assertFalse(coil.is_used_up())

    def test_admin_list_shows_correct_status_badge(self):
        staff = User.objects.create_user('used_status_admin', password='pw', is_staff=True, is_superuser=True)
        used = Material.objects.create(quantity=100, heat_no='USEDH01')
        CoilPart.objects.create(coil=used, part_no='BADGE-A', weight=100)
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
        CoilPart.objects.create(coil=used, part_no='FILTER-A', weight=100)
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

    def test_cannot_cut_a_part_from_an_archived_coil(self):
        self.client.post(reverse('employee_login'), {'pin': settings.EMPLOYEE_PIN})
        coil = Material.objects.create(quantity=500, archived_at=timezone.now())
        product_type = ProductType.objects.create(name='Bar', grade='EN8D', size='1.200')

        response = self.client.post(
            reverse('coil_parts', kwargs={'coil_pk': coil.pk}),
            {'suffix': 'A', 'weight': '10', 'product_type': str(product_type.pk)},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(CoilPart.objects.filter(coil=coil).count(), 0)

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

    def test_weight_exceeding_remaining_shows_error_instead_of_crashing(self):
        response = self.client.post(
            reverse('coil_parts', kwargs={'coil_pk': self.coil.pk}),
            {'suffix': 'A', 'weight': '600', 'product_type': str(self.product_type.pk)},  # coil is 500kg
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "exceeds the remaining coil weight")
        self.assertEqual(CoilPart.objects.filter(coil=self.coil).count(), 0)

    def test_concurrent_cut_overshooting_remaining_weight_rolls_back(self):
        """Two requests can both pass the initial "remaining" check against a
        stale read before either has written anything. weight_used() is called
        again after the part is inserted — simulate a second, already-committed
        cut showing up between those two calls and confirm the whole write
        (part + job + step logs) rolls back instead of over-cutting the coil."""
        with patch.object(
            Material, 'weight_used',
            side_effect=[Decimal('50'), Decimal('600')],  # under, then over quantity=500
        ):
            response = self.client.post(
                reverse('coil_parts', kwargs={'coil_pk': self.coil.pk}),
                {'suffix': 'A', 'weight': '400', 'product_type': str(self.product_type.pk)},
            )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Reload the page and try again")
        self.assertEqual(CoilPart.objects.filter(coil=self.coil).count(), 0)
        self.assertEqual(ProductionJob.objects.count(), 0)


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


class JobStatusRollupTests(TestCase):
    """A job's overall status is a rollup of its steps' latest StepLog —
    recalculate_status() is the single place that computes it."""

    def setUp(self):
        coil = Material.objects.create(quantity=500)
        part = CoilPart.objects.create(coil=coil, part_no='COIL0001-A', weight=10)
        self.product_type = ProductType.objects.create(name='Bar 1.2mm')
        self.step1 = ProcessStep.objects.create(product_type=self.product_type, name='Cutting', order=1)
        self.step2 = ProcessStep.objects.create(product_type=self.product_type, name='Heat treat', order=2)
        self.job = ProductionJob.objects.create(part=part, product_type=self.product_type, job_no='JOB-0001')

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


class PublicPageTests(TestCase):
    """Pages with no auth guard at all: home and the admin login form."""

    def test_home_page_loads(self):
        response = self.client.get(reverse('home'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Management System')

    def test_admin_login_rejects_bad_credentials(self):
        User.objects.create_user('realstaff', password='correct-pw', is_staff=True)
        response = self.client.post(reverse('admin_login'), {
            'username': 'realstaff', 'password': 'wrong-pw',
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Invalid credentials')

    def test_admin_login_succeeds_and_redirects_to_next(self):
        User.objects.create_user('realstaff2', password='correct-pw', is_staff=True)
        response = self.client.post(
            reverse('admin_login') + '?next=' + reverse('order_dashboard'),
            {'username': 'realstaff2', 'password': 'correct-pw', 'next': reverse('order_dashboard')},
        )
        self.assertRedirects(response, reverse('order_dashboard'))

    def test_admin_login_ignores_unsafe_next_url(self):
        """An attacker-supplied next=//evil.com must not be followed — this is
        the open-redirect guard (_safe_next), exercised end-to-end here."""
        User.objects.create_user('realstaff3', password='correct-pw', is_staff=True)
        response = self.client.post(
            reverse('admin_login'),
            {'username': 'realstaff3', 'password': 'correct-pw', 'next': 'https://evil.example.com/'},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, '/admin/')  # falls back to the default, not the unsafe URL


class EmployeePortalPageTests(TestCase):
    """Simple read-only employee-portal pages: the landing page, order
    selection list, coil QR tag, and production board."""

    def setUp(self):
        self.client.post(reverse('employee_login'), {'pin': settings.EMPLOYEE_PIN})

    def test_employee_landing_requires_login(self):
        self.client.post(reverse('employee_logout'))
        response = self.client.get(reverse('employee'))
        self.assertRedirects(response, f"{reverse('employee_login')}?next={reverse('employee')}")

    def test_employee_landing_loads_when_logged_in(self):
        response = self.client.get(reverse('employee'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Employee Portal')

    def test_select_order_splits_confirmed_and_in_production(self):
        customer = Customer.objects.create(name='Select Order Co')
        confirmed = Order.objects.create(customer=customer, quantity=10, status='confirmed')
        in_prod = Order.objects.create(customer=customer, quantity=10, status='in_production')
        Order.objects.create(customer=customer, quantity=10, status='completed')  # excluded

        response = self.client.get(reverse('select_order'))
        self.assertEqual(response.status_code, 200)
        not_started_ids = [o.pk for o in response.context['not_started']]
        in_progress_ids = [o.pk for o in response.context['in_progress']]
        self.assertEqual(not_started_ids, [confirmed.pk])
        self.assertEqual(in_progress_ids, [in_prod.pk])

    def test_coil_tag_renders_qr_code(self):
        coil = Material.objects.create(quantity=500, heat_no='TAGME01')
        response = self.client.get(reverse('coil_tag', kwargs={'pk': coil.pk}))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, coil.formatted_coil())
        self.assertContains(response, 'data:image/png;base64,')

    def test_production_board_shows_only_in_production_orders(self):
        customer = Customer.objects.create(name='Board Co')
        product_type = ProductType.objects.create(name='Bar', grade='EN8D', size='1.200')
        step = ProcessStep.objects.create(product_type=product_type, name='Cutting', order=1)

        in_prod_order = Order.objects.create(customer=customer, quantity=10, status='in_production')
        coil = Material.objects.create(quantity=500)
        part = CoilPart.objects.create(coil=coil, part_no='BOARD-A', weight=10)
        ProductionJob.objects.create(
            part=part, product_type=product_type, job_no='BOARD-JOB-1', order=in_prod_order,
        )
        Order.objects.create(customer=customer, quantity=10, status='confirmed')  # not shown

        response = self.client.get(reverse('production_board'))
        self.assertEqual(response.status_code, 200)
        order_ids = [row['order'].pk for row in response.context['board']]
        self.assertEqual(order_ids, [in_prod_order.pk])


class SelectCoilForOrderSpecFilterTests(TestCase):
    """When a product type has AllowedCoilSpecs configured, only matching
    coils are offered — the earlier archiving/legacy tests only cover the
    no-specs-configured case."""

    def setUp(self):
        self.client.post(reverse('employee_login'), {'pin': settings.EMPLOYEE_PIN})
        self.customer = Customer.objects.create(name='Spec Test Co')
        self.product_type = ProductType.objects.create(name='Spec Bar', grade='X', size='9.999')
        AllowedCoilSpec.objects.create(product_type=self.product_type, grade='EN8D', size='1.200')
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


class QuoteEmailDispatchTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user('quote_email_staff', password='pw', is_staff=True)
        self.customer = Customer.objects.create(name='Email Test Co', email='client@example.com')

    def test_anonymous_cannot_send(self):
        response = self.client.post(reverse('send_quote_email', kwargs={'pk': self.customer.pk}))
        self.assertRedirects(response, reverse('home'))
        self.assertEqual(len(mail.outbox), 0)

    @override_settings(EMAIL_HOST_USER='sender@example.com', DEFAULT_FROM_EMAIL='sender@example.com')
    def test_staff_send_quote_email_delivers_with_the_link(self):
        self.client.force_login(self.staff)
        response = self.client.post(
            reverse('send_quote_email', kwargs={'pk': self.customer.pk}), follow=True,
        )
        self.assertContains(response, f"Quote form sent to {self.customer.email}")
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn(self.customer.email, mail.outbox[0].to)
        self.assertIn(str(self.customer.quote_token), mail.outbox[0].body)

    def test_send_quote_email_fails_gracefully_without_recipient_address(self):
        no_email_customer = Customer.objects.create(name='No Email Co')
        self.client.force_login(self.staff)
        response = self.client.post(
            reverse('send_quote_email', kwargs={'pk': no_email_customer.pk}), follow=True,
        )
        self.assertContains(response, f"No email address on file for {no_email_customer.name}")
        self.assertEqual(len(mail.outbox), 0)

    @override_settings(EMAIL_HOST_USER='')
    def test_send_quote_email_shows_error_when_email_not_configured(self):
        self.client.force_login(self.staff)
        response = self.client.post(
            reverse('send_quote_email', kwargs={'pk': self.customer.pk}), follow=True,
        )
        self.assertContains(response, "Email is not configured")
        self.assertEqual(len(mail.outbox), 0)

    @override_settings(EMAIL_HOST_USER='sender@example.com', DEFAULT_FROM_EMAIL='sender@example.com')
    def test_quick_send_quote_creates_customer_and_sends(self):
        self.client.force_login(self.staff)
        response = self.client.post(reverse('quick_send_quote'), {
            'name': 'Brand New Co', 'email': 'new@example.com', 'phone': '9999999999',
        }, follow=True)
        self.assertContains(response, "Quote form sent to new@example.com")
        customer = Customer.objects.get(name='Brand New Co')
        self.assertEqual(customer.email, 'new@example.com')
        self.assertEqual(len(mail.outbox), 1)

    def test_quick_send_quote_requires_email(self):
        self.client.force_login(self.staff)
        response = self.client.post(
            reverse('quick_send_quote'), {'name': 'No Email Provided Co'}, follow=True,
        )
        self.assertContains(response, "Email address is required")
        self.assertFalse(Customer.objects.filter(name='No Email Provided Co').exists())
        self.assertEqual(len(mail.outbox), 0)
