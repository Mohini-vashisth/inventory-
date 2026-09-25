import hashlib
import hmac
import json
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

from . import views
from .forms import MaterialForm
from .models import (
    AllowedCoilSpec, Customer, GateEntry, GateEntryLot, GradeOption, Material, Order,
    OrderCoilPick, ProcessStep, ProductionJob, ProductType, Query, Quotation, SizeOption, StepLog,
)
from .pdf import generate_quotation_pdf
from .views import (
    WhatsAppSendError, WHATSAPP_QUERY_INTAKE_TEMPLATE, WHATSAPP_QUERY_INTAKE_TEMPLATE_LANGUAGE,
    WHATSAPP_QUERY_QUESTIONS, WHATSAPP_CLOSING_MESSAGE,
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

    def test_negative_quantity_rejected(self):
        form = MaterialForm(self.base_data(quantity='-500.000'))
        self.assertFalse(form.is_valid())
        self.assertIn('quantity', form.errors)

    def test_zero_quantity_rejected(self):
        form = MaterialForm(self.base_data(quantity='0'))
        self.assertFalse(form.is_valid())
        self.assertIn('quantity', form.errors)


class GateEntryFormViewErrorDisplayTests(TestCase):
    """A rejected gate entry submission must show why, and not force the
    employee to retype everything."""

    def setUp(self):
        GradeOption.objects.get_or_create(name='EN8D')
        SizeOption.objects.get_or_create(value='1.200')
        self.client.post(reverse('employee_login'), {'pin': settings.EMPLOYEE_PIN})

    def test_non_numeric_total_weight_shows_error_and_repopulates_fields(self):
        response = self.client.post(reverse('gate_entry_form'), {
            'date': '2026-07-06', 'vendor': 'ABC Traders',
            'vehicle_no': 'AP16TA1234', 'total_weight': 'not-a-number',
            'lot-TOTAL_FORMS': '1', 'lot-INITIAL_FORMS': '0',
            'lot-MIN_NUM_FORMS': '0', 'lot-MAX_NUM_FORMS': '1000',
            'lot-0-company': 'Tata Steel', 'lot-0-grade': 'EN8D',
            'lot-0-size': '1.200', 'lot-0-no_of_coils': '3',
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Enter a number')
        self.assertContains(response, 'ABC Traders')
        self.assertEqual(GateEntry.objects.count(), 0)

    def test_negative_total_weight_rejected(self):
        response = self.client.post(reverse('gate_entry_form'), {
            'date': '2026-07-06', 'vendor': 'ABC Traders',
            'vehicle_no': 'AP16TA1234', 'total_weight': '-2500.000',
            'lot-TOTAL_FORMS': '1', 'lot-INITIAL_FORMS': '0',
            'lot-MIN_NUM_FORMS': '0', 'lot-MAX_NUM_FORMS': '1000',
            'lot-0-company': 'Tata Steel', 'lot-0-grade': 'EN8D',
            'lot-0-size': '1.200', 'lot-0-no_of_coils': '3',
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "greater than zero")
        self.assertEqual(GateEntry.objects.count(), 0)


class GateEntryLotFormViewErrorDisplayTests(TestCase):
    """Grade/size validation against GradeOption/SizeOption lives here, not
    in material_form — a lot's grade/size is locked in once created and
    inherited by every coil registered against it."""

    def setUp(self):
        GradeOption.objects.get_or_create(name='EN8D')
        SizeOption.objects.get_or_create(value='1.200')
        self.client.post(reverse('employee_login'), {'pin': settings.EMPLOYEE_PIN})
        self.gate_entry = GateEntry.objects.create(
            vendor='ABC Traders', vehicle_no='AP16TA1234', total_weight=2500,
        )

    def test_invalid_grade_shows_error_and_repopulates_fields(self):
        response = self.client.post(reverse('gate_entry_lot_form', args=[self.gate_entry.pk]), {
            'grade': 'MADE-UP', 'size': '1.200', 'no_of_coils': '5',
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Select a grade from the list.')
        self.assertEqual(GateEntryLot.objects.count(), 0)


class MaterialFormViewErrorDisplayTests(TestCase):
    """A rejected coil submission must show why, and not force the employee
    to retype everything."""

    def setUp(self):
        GradeOption.objects.get_or_create(name='EN8D')
        SizeOption.objects.get_or_create(value='1.200')
        self.client.post(reverse('employee_login'), {'pin': settings.EMPLOYEE_PIN})
        gate_entry = GateEntry.objects.create(
            vendor='ABC Traders', vehicle_no='AP16TA1234', total_weight=2500,
        )
        self.lot = GateEntryLot.objects.create(gate_entry=gate_entry, company='Tata Steel', grade='EN8D', size='1.200', no_of_coils=5)

    def test_non_numeric_quantity_shows_error_and_repopulates_fields(self):
        response = self.client.post(reverse('material_form', args=[self.lot.pk]), {
            'date': '2026-07-06', 'quantity': 'not-a-number', 'heat_no': 'H001',
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Enter a number')
        self.assertContains(response, 'H001')
        self.assertEqual(Material.objects.count(), 0)


class BackfillOptionsCommandTests(TestCase):
    """Backfills GradeOption/SizeOption from whatever distinct grade/size
    values already exist in Material, as-is — duplicate spellings included.
    A migration (0013) seeds a small baseline set of options into every
    fresh database, so assertions check for specific values rather than
    the table being empty beforehand."""

    def test_adds_new_grades_and_sizes_found_in_material(self):
        Material.objects.create(grade='EN-8D', size='6.500', quantity=500)
        Material.objects.create(grade='EN-8D', size='6.500', quantity=500)  # duplicate, not double-added
        Material.objects.create(grade='SAE 1008', size='9.000', quantity=500)

        call_command('backfill_options')

        self.assertEqual(GradeOption.objects.filter(name='EN-8D').count(), 1)
        self.assertEqual(GradeOption.objects.filter(name='SAE 1008').count(), 1)
        self.assertEqual(SizeOption.objects.filter(value=Decimal('6.500')).count(), 1)
        self.assertEqual(SizeOption.objects.filter(value=Decimal('9.000')).count(), 1)

    def test_does_not_duplicate_existing_options(self):
        Material.objects.create(grade='EN8D', size='1.200', quantity=500)  # already seeded by 0013

        call_command('backfill_options')

        self.assertEqual(GradeOption.objects.filter(name='EN8D').count(), 1)
        self.assertEqual(SizeOption.objects.filter(value=Decimal('1.200')).count(), 1)

    def test_blank_and_null_grades_are_ignored(self):
        Material.objects.create(grade=None, size=None, quantity=500)
        Material.objects.create(grade='', size='6.000', quantity=500)

        call_command('backfill_options')

        self.assertFalse(GradeOption.objects.filter(name='').exists())
        self.assertEqual(SizeOption.objects.filter(value=Decimal('6.000')).count(), 1)

    def test_dry_run_changes_nothing(self):
        Material.objects.create(grade='EN-8D', size='6.500', quantity=500)
        call_command('backfill_options', '--dry-run')
        self.assertFalse(GradeOption.objects.filter(name='EN-8D').exists())
        self.assertFalse(SizeOption.objects.filter(value=Decimal('6.500')).exists())


class MaterialFieldAutocompleteTests(TestCase):
    def setUp(self):
        self.client.post(reverse('employee_login'), {'pin': settings.EMPLOYEE_PIN})
        Material.objects.create(company='Tata Steel', vendor='ABC Traders', quantity=500)
        Material.objects.create(company='Tata Sons', vendor='XYZ Traders', quantity=500)

    def test_matches_company_by_partial_name(self):
        response = self.client.get(reverse('material_field_autocomplete'), {'field': 'company', 'q': 'tata'})
        self.assertEqual(set(response.json()), {'Tata Steel', 'Tata Sons'})

    def test_matches_vendor_by_partial_name(self):
        response = self.client.get(reverse('material_field_autocomplete'), {'field': 'vendor', 'q': 'abc'})
        self.assertEqual(response.json(), ['ABC Traders'])

    def test_unknown_field_returns_empty(self):
        response = self.client.get(reverse('material_field_autocomplete'), {'field': 'heat_no', 'q': 'H'})
        self.assertEqual(response.json(), [])

    def test_empty_query_returns_empty(self):
        response = self.client.get(reverse('material_field_autocomplete'), {'field': 'company'})
        self.assertEqual(response.json(), [])

    def test_requires_employee_login(self):
        self.client.post(reverse('employee_logout'))
        url = reverse('material_field_autocomplete')
        response = self.client.get(url, {'field': 'company', 'q': 'tata'})
        self.assertRedirects(response, f"{reverse('employee_login')}?next={url}")


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


class GateEntryFormTests(TestCase):
    """gate_entry_form creates the GateEntry and all of its lots together,
    in one submission — the single-page form with a repeatable, collapsible
    lot section described by the user."""

    def setUp(self):
        GradeOption.objects.get_or_create(name='EN8D')
        SizeOption.objects.get_or_create(value='1.200')
        self.client.post(reverse('employee_login'), {'pin': settings.EMPLOYEE_PIN})

    def _post_data(self, lots, **overrides):
        data = {
            'date': '2026-07-06', 'vendor': 'ABC Traders',
            'vehicle_no': 'AP16TA1234', 'total_weight': '2500.000',
            'lot-TOTAL_FORMS': str(len(lots)), 'lot-INITIAL_FORMS': '0',
            'lot-MIN_NUM_FORMS': '0', 'lot-MAX_NUM_FORMS': '1000',
        }
        for i, lot in enumerate(lots):
            for key, value in lot.items():
                data[f'lot-{i}-{key}'] = value
        data.update(overrides)
        return data

    def test_valid_submission_creates_gate_entry_and_lot_then_redirects_to_detail(self):
        data = self._post_data([
            {'company': 'Tata Steel', 'grade': 'EN8D', 'size': '1.200', 'no_of_coils': '3'},
        ])
        response = self.client.post(reverse('gate_entry_form'), data)
        ge = GateEntry.objects.get()
        self.assertRedirects(response, reverse('gate_entry_detail', args=[ge.pk]))
        self.assertEqual(ge.vendor, 'ABC Traders')
        self.assertEqual(ge.total_weight, Decimal('2500.000'))
        lot = GateEntryLot.objects.get()
        self.assertEqual(lot.gate_entry, ge)
        self.assertEqual(lot.company, 'Tata Steel')
        self.assertEqual(lot.no_of_coils, 3)

    def test_multiple_lots_created_together(self):
        data = self._post_data([
            {'company': 'Tata Steel', 'grade': 'EN8D', 'size': '1.200', 'no_of_coils': '3'},
            {'company': 'JSW', 'grade': 'EN8D', 'size': '1.200', 'no_of_coils': '2'},
        ])
        self.client.post(reverse('gate_entry_form'), data)
        ge = GateEntry.objects.get()
        self.assertEqual(GateEntryLot.objects.filter(gate_entry=ge).count(), 2)
        self.assertEqual(ge.no_of_coils(), 5)
        companies = set(GateEntryLot.objects.filter(gate_entry=ge).values_list('company', flat=True))
        self.assertEqual(companies, {'Tata Steel', 'JSW'})

    def test_invalid_lot_rolls_back_the_whole_submission(self):
        """All-or-nothing: an invalid second lot must not leave a gate entry
        or a valid first lot behind."""
        data = self._post_data([
            {'company': 'Tata Steel', 'grade': 'EN8D', 'size': '1.200', 'no_of_coils': '3'},
            {'company': 'JSW', 'grade': 'MADE-UP', 'size': '1.200', 'no_of_coils': '2'},
        ])
        response = self.client.post(reverse('gate_entry_form'), data)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(GateEntry.objects.count(), 0)
        self.assertEqual(GateEntryLot.objects.count(), 0)

    def test_requires_employee_login(self):
        self.client.post(reverse('employee_logout'))
        response = self.client.get(reverse('gate_entry_form'))
        self.assertRedirects(response, f"{reverse('employee_login')}?next={reverse('gate_entry_form')}")

    def test_vehicle_no_is_uppercased_even_if_submitted_lowercase(self):
        """The form's own JS already forces uppercase as the employee types —
        this covers anything submitted without it (a direct API call, JS
        disabled, etc.)."""
        data = self._post_data(
            [{'company': 'Tata Steel', 'grade': 'EN8D', 'size': '1.200', 'no_of_coils': '3'}],
            vehicle_no='ap16ta1234',
        )
        self.client.post(reverse('gate_entry_form'), data)
        ge = GateEntry.objects.get()
        self.assertEqual(ge.vehicle_no, 'AP16TA1234')


class GateEntryLotFormTests(TestCase):
    def setUp(self):
        GradeOption.objects.get_or_create(name='EN8D')
        SizeOption.objects.get_or_create(value='1.200')
        self.client.post(reverse('employee_login'), {'pin': settings.EMPLOYEE_PIN})
        self.gate_entry = GateEntry.objects.create(
            vendor='ABC Traders', vehicle_no='AP16TA1234', total_weight=2500,
        )

    def test_valid_submission_creates_lot_and_redirects_to_gate_entry_detail(self):
        response = self.client.post(reverse('gate_entry_lot_form', args=[self.gate_entry.pk]), {
            'grade': 'EN8D', 'size': '1.200', 'no_of_coils': '5',
        })
        lot = GateEntryLot.objects.get()
        self.assertRedirects(response, reverse('gate_entry_detail', args=[self.gate_entry.pk]))
        self.assertEqual(lot.gate_entry, self.gate_entry)
        self.assertEqual(lot.no_of_coils, 5)

    def test_zero_coils_rejected(self):
        response = self.client.post(reverse('gate_entry_lot_form', args=[self.gate_entry.pk]), {
            'grade': 'EN8D', 'size': '1.200', 'no_of_coils': '0',
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(GateEntryLot.objects.count(), 0)

    def test_unknown_gate_entry_404s(self):
        response = self.client.get(reverse('gate_entry_lot_form', args=[99999]))
        self.assertEqual(response.status_code, 404)


class GateEntryDetailTests(TestCase):
    def setUp(self):
        self.client.post(reverse('employee_login'), {'pin': settings.EMPLOYEE_PIN})
        self.gate_entry = GateEntry.objects.create(
            vendor='ABC Traders', vehicle_no='AP16TA1234', total_weight=1000,
        )

    def test_lists_lots_added_so_far(self):
        lot = GateEntryLot.objects.create(gate_entry=self.gate_entry, company='Tata Steel', grade='EN8D', size='1.200', no_of_coils=3)
        response = self.client.get(reverse('gate_entry_detail', args=[self.gate_entry.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual([row.pk for row in response.context['lots']], [lot.pk])
        self.assertContains(response, 'EN8D')

    def test_no_lots_shows_empty_state(self):
        response = self.client.get(reverse('gate_entry_detail', args=[self.gate_entry.pk]))
        self.assertContains(response, "No lots added yet")

    def test_invoice_no_shown_when_present(self):
        self.gate_entry.invoice_no = 'INV-0042'
        self.gate_entry.save()
        response = self.client.get(reverse('gate_entry_detail', args=[self.gate_entry.pk]))
        self.assertContains(response, 'INV-0042')

    def test_edit_link_shown(self):
        response = self.client.get(reverse('gate_entry_detail', args=[self.gate_entry.pk]))
        self.assertContains(response, reverse('gate_entry_edit', args=[self.gate_entry.pk]))


class GateEntryEditTests(TestCase):
    """Fixing a mistake in a gate entry's top-level details after it's
    already been saved — allowed regardless of whether coils have already
    been registered against its lots, since these fields are just
    paper/reference details."""

    def setUp(self):
        self.client.post(reverse('employee_login'), {'pin': settings.EMPLOYEE_PIN})
        self.gate_entry = GateEntry.objects.create(
            date='2026-07-01', vendor='ABC Traders', vehicle_no='AP16TA1234',
            invoice_no='INV-0001', total_weight=1000,
        )

    def test_requires_employee_login(self):
        self.client.post(reverse('employee_logout'))
        response = self.client.get(reverse('gate_entry_edit', args=[self.gate_entry.pk]))
        self.assertRedirects(
            response,
            f"{reverse('employee_login')}?next={reverse('gate_entry_edit', args=[self.gate_entry.pk])}",
        )

    def test_get_prefills_existing_values(self):
        response = self.client.get(reverse('gate_entry_edit', args=[self.gate_entry.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'ABC Traders')
        self.assertContains(response, 'AP16TA1234')
        self.assertContains(response, 'INV-0001')

    def test_valid_edit_updates_and_redirects_to_detail(self):
        response = self.client.post(
            reverse('gate_entry_edit', args=[self.gate_entry.pk]),
            {
                'date': '2026-07-02', 'vendor': 'XYZ Traders', 'vehicle_no': 'ka1a1234',
                'invoice_no': 'INV-0002', 'total_weight': '1500.000',
            },
        )
        self.assertRedirects(response, reverse('gate_entry_detail', args=[self.gate_entry.pk]))
        self.gate_entry.refresh_from_db()
        self.assertEqual(self.gate_entry.vendor, 'XYZ Traders')
        self.assertEqual(self.gate_entry.vehicle_no, 'KA1A1234')  # server-side uppercase safety net
        self.assertEqual(self.gate_entry.invoice_no, 'INV-0002')
        self.assertEqual(self.gate_entry.total_weight, Decimal('1500.000'))

    def test_edit_allowed_even_after_coils_registered(self):
        lot = GateEntryLot.objects.create(
            gate_entry=self.gate_entry, company='Tata Steel', grade='EN8D', size='1.200', no_of_coils=1,
        )
        Material.objects.create(lot=lot, quantity=500)
        response = self.client.post(
            reverse('gate_entry_edit', args=[self.gate_entry.pk]),
            {
                'date': '2026-07-02', 'vendor': 'XYZ Traders', 'vehicle_no': 'AP16TA1234',
                'invoice_no': 'INV-0002', 'total_weight': '1500.000',
            },
        )
        self.assertRedirects(response, reverse('gate_entry_detail', args=[self.gate_entry.pk]))
        self.gate_entry.refresh_from_db()
        self.assertEqual(self.gate_entry.vendor, 'XYZ Traders')

    def test_invalid_edit_shows_error_and_does_not_save(self):
        response = self.client.post(
            reverse('gate_entry_edit', args=[self.gate_entry.pk]),
            {'date': '2026-07-02', 'vendor': 'XYZ Traders', 'total_weight': 'not-a-number'},
        )
        self.assertEqual(response.status_code, 200)
        self.gate_entry.refresh_from_db()
        self.assertEqual(self.gate_entry.vendor, 'ABC Traders')  # unchanged


class GateEntryLotDeleteTests(TestCase):
    """Removing a lot added by mistake — only while it has no coils
    registered against it yet."""

    def setUp(self):
        self.client.post(reverse('employee_login'), {'pin': settings.EMPLOYEE_PIN})
        self.gate_entry = GateEntry.objects.create(
            vendor='ABC Traders', vehicle_no='AP16TA1234', total_weight=1000,
        )
        self.lot = GateEntryLot.objects.create(
            gate_entry=self.gate_entry, company='Tata Steel', grade='EN8D', size='1.200', no_of_coils=3,
        )

    def test_empty_lot_is_removed_and_redirects_to_detail(self):
        response = self.client.post(reverse('gate_entry_lot_delete', args=[self.lot.pk]))
        self.assertRedirects(response, reverse('gate_entry_detail', args=[self.gate_entry.pk]))
        self.assertFalse(GateEntryLot.objects.filter(pk=self.lot.pk).exists())

    def test_lot_with_registered_coils_is_not_removed(self):
        Material.objects.create(lot=self.lot, quantity=500)
        self.client.post(reverse('gate_entry_lot_delete', args=[self.lot.pk]))
        self.assertTrue(GateEntryLot.objects.filter(pk=self.lot.pk).exists())

    def test_get_does_not_delete(self):
        self.client.get(reverse('gate_entry_lot_delete', args=[self.lot.pk]))
        self.assertTrue(GateEntryLot.objects.filter(pk=self.lot.pk).exists())

    def test_requires_employee_login(self):
        self.client.post(reverse('employee_logout'))
        url = reverse('gate_entry_lot_delete', args=[self.lot.pk])
        response = self.client.post(url)
        self.assertRedirects(response, f"{reverse('employee_login')}?next={url}")


class SelectGateEntryTests(TestCase):
    def setUp(self):
        self.client.post(reverse('employee_login'), {'pin': settings.EMPLOYEE_PIN})

    def test_only_open_lots_are_listed(self):
        open_ge = GateEntry.objects.create(total_weight=1000, vehicle_no='OPEN1')
        open_lot = GateEntryLot.objects.create(gate_entry=open_ge, grade='EN8D', size='1.200', no_of_coils=2)

        complete_ge = GateEntry.objects.create(total_weight=1000, vehicle_no='DONE1')
        complete_lot = GateEntryLot.objects.create(gate_entry=complete_ge, grade='EN8D', size='1.200', no_of_coils=1)
        Material.objects.create(lot=complete_lot, quantity=1000)

        response = self.client.get(reverse('select_gate_entry'))
        listed_ids = [row['lot'].pk for row in response.context['lots']]
        self.assertEqual(listed_ids, [open_lot.pk])

    def test_no_open_lots_shows_empty_state(self):
        response = self.client.get(reverse('select_gate_entry'))
        self.assertContains(response, "No lot has coils left")


class MaterialFormGateEntryTests(TestCase):
    """New Coil Entry is always scoped to a lot: vendor comes from the gate
    entry, company/grade/size from the lot, all locked; invoice_weight is
    computed from the gate entry; and the lot caps how many coils can be
    registered against it."""

    def setUp(self):
        GradeOption.objects.get_or_create(name='EN8D')
        SizeOption.objects.get_or_create(value='1.200')
        self.client.post(reverse('employee_login'), {'pin': settings.EMPLOYEE_PIN})
        self.gate_entry = GateEntry.objects.create(
            vendor='ABC Traders', vehicle_no='AP16TA1234', total_weight=1000,
        )
        self.lot = GateEntryLot.objects.create(
            gate_entry=self.gate_entry, company='Tata Steel', grade='EN8D', size='1.200', no_of_coils=2,
        )

    def _post_coil(self, lot=None, **overrides):
        lot = lot or self.lot
        data = {'date': '2026-07-06', 'heat_no': 'H001', 'quantity': '480.500'}
        data.update(overrides)
        return self.client.post(reverse('material_form', args=[lot.pk]), data)

    def test_requires_employee_login(self):
        self.client.post(reverse('employee_logout'))
        url = reverse('material_form', args=[self.lot.pk])
        response = self.client.get(url)
        self.assertRedirects(response, f"{reverse('employee_login')}?next={url}")

    def test_unknown_lot_404s(self):
        response = self.client.get(reverse('material_form', args=[99999]))
        self.assertEqual(response.status_code, 404)

    def test_fields_are_locked_even_if_tampered(self):
        """company/vendor/grade/size are never read from the submitted form —
        an attacker (or a stale cached page) posting different values has no effect."""
        self._post_coil(company='SPOOFED', vendor='SPOOFED', grade='SPOOFED', size='9.999')
        coil = Material.objects.get()
        self.assertEqual(coil.company, 'Tata Steel')
        self.assertEqual(coil.vendor, 'ABC Traders')
        self.assertEqual(coil.grade, 'EN8D')
        self.assertEqual(coil.size, Decimal('1.200'))

    def test_archived_at_and_legacy_used_weight_cannot_be_posted(self):
        """These aren't read from trusted sources like company/vendor/grade/
        size (there's no legitimate way to set them from this form at all —
        archived_at only ever comes from the admin's archive action,
        legacy_used_weight only from import_excel) — MaterialForm.Meta must
        exclude both, or a raw/scripted POST could pre-archive a brand-new
        coil or corrupt its weight_used() math with a fake legacy figure."""
        self._post_coil(archived_at='2020-01-01T00:00:00Z', legacy_used_weight='999999')
        coil = Material.objects.get()
        self.assertIsNone(coil.archived_at)
        self.assertEqual(coil.legacy_used_weight, Decimal('0'))

    def test_invoice_weight_computed_from_gate_entry_average(self):
        self._post_coil()
        coil = Material.objects.get()
        self.assertEqual(coil.invoice_weight, Decimal('500.000'))  # 1000 / 2
        self.assertEqual(coil.quantity, Decimal('480.500'))  # the actual measured weight, unaffected

    def test_registering_exactly_no_of_coils_succeeds_then_blocks_further(self):
        self._post_coil(heat_no='H001')
        self._post_coil(heat_no='H002')
        self.assertEqual(Material.objects.filter(lot=self.lot).count(), 2)

        response = self._post_coil(heat_no='H003')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "already been registered")
        self.assertEqual(Material.objects.filter(lot=self.lot).count(), 2)

    def test_complete_lot_shows_banner_on_get(self):
        self._post_coil(heat_no='H001')
        self._post_coil(heat_no='H002')
        response = self.client.get(reverse('material_form', args=[self.lot.pk]))
        self.assertContains(response, "have already been registered")

    def test_concurrent_registration_of_the_last_slot_rolls_back(self):
        """Two requests can both pass the `complete` check against a stale
        read before either has written anything. coils_registered() is
        called again after the coil is inserted — simulate a second,
        already-committed registration showing up between those two calls
        and confirm the write rolls back instead of exceeding no_of_coils."""
        self._post_coil(heat_no='H001')  # 1 of 2 used

        # First call is the pre-check (real value: 1 registered, 1 slot left,
        # not complete) — second is the post-insert re-check, mocked to look
        # as if a second, concurrent registration had already landed too.
        with patch.object(GateEntryLot, 'coils_registered', side_effect=[1, 3]):
            response = self._post_coil(heat_no='H002')

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "reload and check with the office")
        self.assertEqual(Material.objects.filter(lot=self.lot).count(), 1)


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
            {'pin': settings.EMPLOYEE_PIN, 'next': reverse('select_gate_entry')},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse('select_gate_entry'))


class OrderCoilPickCreationTests(TestCase):
    """Picking a coil must be all-or-nothing: never an OrderCoilPick with no job."""

    def setUp(self):
        self.client.post(reverse('employee_login'), {'pin': settings.EMPLOYEE_PIN})
        self.coil = Material.objects.create(
            date='2026-07-01', grade='EN8D', size='1.2',
            company='Tata Steel', vendor='ABC Traders', quantity=500, heat_no='H001',
        )
        self.product_type = ProductType.objects.create(item_code='Bar 1.2mm', grade='EN8D', size='1.2')
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
        first = Order.objects.create(customer=self.customer, quantity=10)
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
        order = Order.objects.create(
            customer=self.customer, product_type=self.product_type, quantity=400, status='confirmed',
        )
        self.client.force_login(self.staff)
        response = self.client.get(reverse('order_dashboard'))
        self.assertContains(response, 'Low stock')

    def test_dashboard_hides_badge_for_pending_orders(self):
        """The check is only actionable once an order is actually
        committed to production — a pending order hasn't been accepted yet."""
        Material.objects.create(quantity=50, grade='EN8D', size='1.200')
        order = Order.objects.create(
            customer=self.customer, product_type=self.product_type, quantity=400, status='pending',
        )
        self.client.force_login(self.staff)
        response = self.client.get(reverse('order_dashboard'))
        self.assertNotContains(response, 'Low stock')

    def test_dashboard_hides_badge_when_stock_is_sufficient(self):
        Material.objects.create(quantity=500, grade='EN8D', size='1.200')
        order = Order.objects.create(
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

    def test_employee_landing_links_to_gate_entry(self):
        response = self.client.get(reverse('employee'))
        self.assertContains(response, reverse('gate_entry_form'))

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

    def test_add_another_coil_continues_same_lot_if_slots_remain(self):
        ge = GateEntry.objects.create(total_weight=1000)
        lot = GateEntryLot.objects.create(gate_entry=ge, grade='EN8D', size='1.200', no_of_coils=2)
        coil = Material.objects.create(lot=lot, quantity=500, heat_no='TAG02')
        response = self.client.get(reverse('coil_tag', kwargs={'pk': coil.pk}))
        self.assertContains(response, reverse('material_form', args=[lot.pk]))

    def test_add_another_coil_goes_to_select_gate_entry_once_complete(self):
        ge = GateEntry.objects.create(total_weight=1000)
        lot = GateEntryLot.objects.create(gate_entry=ge, grade='EN8D', size='1.200', no_of_coils=1)
        coil = Material.objects.create(lot=lot, quantity=1000, heat_no='TAG03')
        response = self.client.get(reverse('coil_tag', kwargs={'pk': coil.pk}))
        self.assertContains(response, reverse('select_gate_entry'))
        self.assertNotContains(response, reverse('material_form', args=[lot.pk]))

    def test_production_board_shows_only_in_production_orders(self):
        customer = Customer.objects.create(name='Board Co')
        product_type = ProductType.objects.create(item_code='Bar', grade='EN8D', size='1.200')
        step = ProcessStep.objects.create(product_type=product_type, name='Cutting', order=1)

        in_prod_order = Order.objects.create(customer=customer, quantity=10, status='in_production')
        coil = Material.objects.create(quantity=500)
        pick = OrderCoilPick.objects.create(order=in_prod_order, coil=coil, weight_allocated=10)
        ProductionJob.objects.create(
            pick=pick, product_type=product_type, job_no='BOARD-JOB-1', order=in_prod_order,
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
        self.product_type = ProductType.objects.create(item_code='Spec Bar', grade='X', size='9.999')
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
        self.product_type = ProductType.objects.create(item_code='Fit Bar', grade='X', size='9.999')
        AllowedCoilSpec.objects.create(
            product_type=self.product_type, grade='EN8D', size='1.200',
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
        self.product_type = ProductType.objects.create(item_code='Ratio Bar', grade='X', size='9.999')

    def _coil(self, **kwargs):
        """Freshly re-fetched so DecimalField values (size) come back as
        Decimal rather than the raw string just assigned in memory —
        matches how the real views always read coils (via a DB lookup)."""
        coil = Material.objects.create(**kwargs)
        return Material.objects.get(pk=coil.pk)

    def test_output_equivalent_uses_matching_spec_ratio(self):
        """1.100 ratio = 10% wastage: 110kg of raw material yields 100kg of output."""
        AllowedCoilSpec.objects.create(
            product_type=self.product_type, grade='EN8D', size='1.200',
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
            product_type=self.product_type, grade='EN8D', size='1.200',
            raw_material_ratio=Decimal('1.100'),
        )
        AllowedCoilSpec.objects.create(
            product_type=self.product_type, grade='SAE1008', size='6.000',
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
            product_type=self.product_type, grade='EN8D', size='1.200',
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
        self.product_type = ProductType.objects.create(item_code='Scan Bar', grade='X', size='9.999')
        AllowedCoilSpec.objects.create(product_type=self.product_type, grade='EN8D', size='1.200')
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
            reverse('send_quote_email', kwargs={'pk': self.customer.pk}),
            {'rate_per_kg': '85.50'}, follow=True,
        )
        quotation = Quotation.objects.get(customer=self.customer)
        self.assertContains(response, f"Quotation {quotation.formatted_no()} sent to {self.customer.email}")
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn(self.customer.email, mail.outbox[0].to)
        self.assertIn(str(self.customer.quote_token), mail.outbox[0].body)
        self.assertEqual(quotation.rate_per_kg, Decimal('85.50'))
        self.assertEqual(len(mail.outbox[0].attachments), 1)
        filename, content, mimetype = mail.outbox[0].attachments[0]
        self.assertEqual(filename, f"{quotation.formatted_no()}.pdf")
        self.assertEqual(mimetype, 'application/pdf')
        self.assertTrue(content.startswith(b'%PDF'))

    def test_send_quote_email_rejects_missing_rate(self):
        self.client.force_login(self.staff)
        response = self.client.post(
            reverse('send_quote_email', kwargs={'pk': self.customer.pk}), follow=True,
        )
        self.assertContains(response, "Enter a valid rate per kg")
        self.assertEqual(Quotation.objects.count(), 0)
        self.assertEqual(len(mail.outbox), 0)

    def test_send_quote_email_rejects_zero_or_negative_rate(self):
        self.client.force_login(self.staff)
        for bad_rate in ('0', '-5'):
            response = self.client.post(
                reverse('send_quote_email', kwargs={'pk': self.customer.pk}),
                {'rate_per_kg': bad_rate}, follow=True,
            )
            self.assertContains(response, "Enter a valid rate per kg")
        self.assertEqual(Quotation.objects.count(), 0)
        self.assertEqual(len(mail.outbox), 0)

    @override_settings(EMAIL_HOST_USER='sender@example.com', DEFAULT_FROM_EMAIL='sender@example.com')
    def test_quotation_numbers_are_sequential(self):
        self.client.force_login(self.staff)
        self.client.post(reverse('send_quote_email', kwargs={'pk': self.customer.pk}), {'rate_per_kg': '10'})
        self.client.post(reverse('send_quote_email', kwargs={'pk': self.customer.pk}), {'rate_per_kg': '20'})
        numbers = list(Quotation.objects.order_by('quotation_no').values_list('quotation_no', flat=True))
        self.assertEqual(numbers, [numbers[0], numbers[0] + 1])

    @override_settings(
        EMAIL_HOST_USER='sender@example.com', DEFAULT_FROM_EMAIL='sender@example.com',
        PUBLIC_QUOTE_BASE_URL='https://quote.mattadrawing.com',
        ALLOWED_HOSTS=['mdw.tail2734e7.ts.net', 'testserver'],
    )
    def test_quote_link_uses_public_base_url_not_the_admin_request_host(self):
        """Admins only ever reach this app over Tailscale — the email must
        not link to that private address, which a real customer can't open."""
        self.client.force_login(self.staff)
        self.client.post(
            reverse('send_quote_email', kwargs={'pk': self.customer.pk}),
            {'rate_per_kg': '85.50'},
            HTTP_HOST='mdw.tail2734e7.ts.net',
        )
        self.assertEqual(len(mail.outbox), 1)
        body = mail.outbox[0].body
        self.assertIn(f"https://quote.mattadrawing.com/quote/{self.customer.quote_token}/", body)
        self.assertNotIn('tail2734e7', body)

    @override_settings(EMAIL_HOST_USER='sender@example.com', DEFAULT_FROM_EMAIL='sender@example.com')
    def test_quote_link_falls_back_to_request_host_when_public_base_url_unset(self):
        self.client.force_login(self.staff)
        self.client.post(reverse('send_quote_email', kwargs={'pk': self.customer.pk}), {'rate_per_kg': '85.50'})
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn(f"testserver/quote/{self.customer.quote_token}/", mail.outbox[0].body)

    def test_send_quote_email_fails_gracefully_without_recipient_address(self):
        no_email_customer = Customer.objects.create(name='No Email Co')
        self.client.force_login(self.staff)
        response = self.client.post(
            reverse('send_quote_email', kwargs={'pk': no_email_customer.pk}),
            {'rate_per_kg': '85.50'}, follow=True,
        )
        self.assertContains(response, f"No email address on file for {no_email_customer.name}")
        self.assertEqual(len(mail.outbox), 0)

    @override_settings(EMAIL_HOST_USER='')
    def test_send_quote_email_shows_error_when_email_not_configured(self):
        self.client.force_login(self.staff)
        response = self.client.post(
            reverse('send_quote_email', kwargs={'pk': self.customer.pk}),
            {'rate_per_kg': '85.50'}, follow=True,
        )
        self.assertContains(response, "Email is not configured")
        self.assertEqual(len(mail.outbox), 0)

    @override_settings(EMAIL_HOST_USER='sender@example.com', DEFAULT_FROM_EMAIL='sender@example.com')
    def test_quick_send_quote_creates_customer_and_sends(self):
        self.client.force_login(self.staff)
        response = self.client.post(reverse('quick_send_quote'), {
            'name': 'Brand New Co', 'email': 'new@example.com', 'phone': '9999999999',
            'rate_per_kg': '85.50',
        }, follow=True)
        customer = Customer.objects.get(name='Brand New Co')
        quotation = Quotation.objects.get(customer=customer)
        self.assertContains(response, f"Quotation {quotation.formatted_no()} sent to new@example.com")
        self.assertEqual(customer.email, 'new@example.com')
        self.assertEqual(len(mail.outbox), 1)

    def test_quick_send_quote_requires_email(self):
        self.client.force_login(self.staff)
        response = self.client.post(
            reverse('quick_send_quote'), {'name': 'No Email Provided Co', 'rate_per_kg': '85.50'}, follow=True,
        )
        self.assertContains(response, "Email address is required")
        self.assertFalse(Customer.objects.filter(name='No Email Provided Co').exists())
        self.assertEqual(len(mail.outbox), 0)

    def test_quick_send_quote_requires_rate(self):
        self.client.force_login(self.staff)
        response = self.client.post(
            reverse('quick_send_quote'),
            {'name': 'Rateless Co', 'email': 'rateless@example.com'}, follow=True,
        )
        self.assertContains(response, "Enter a valid rate per kg")
        self.assertFalse(Customer.objects.filter(name='Rateless Co').exists())
        self.assertEqual(len(mail.outbox), 0)


class QuotationPdfTests(TestCase):
    def test_generate_quotation_pdf_returns_a_real_pdf(self):
        customer = Customer.objects.create(name='PDF Test Co', email='pdf@example.com')
        quotation = Quotation.objects.create(customer=customer, rate_per_kg=Decimal('99.99'), grade='EN8D', size=Decimal('1.200'))
        pdf_bytes = generate_quotation_pdf(quotation)
        self.assertTrue(pdf_bytes.startswith(b'%PDF'))
        self.assertGreater(len(pdf_bytes), 0)

    def test_anonymous_cannot_download_quotation_pdf(self):
        customer = Customer.objects.create(name='PDF Guard Co')
        quotation = Quotation.objects.create(customer=customer, rate_per_kg=Decimal('50'))
        response = self.client.get(reverse('quotation_pdf', kwargs={'pk': quotation.pk}))
        self.assertRedirects(response, reverse('home'))

    def test_staff_can_download_quotation_pdf(self):
        staff = User.objects.create_user('pdf_staff', password='pw', is_staff=True)
        customer = Customer.objects.create(name='PDF Download Co')
        quotation = Quotation.objects.create(customer=customer, rate_per_kg=Decimal('50'))
        self.client.force_login(staff)
        response = self.client.get(reverse('quotation_pdf', kwargs={'pk': quotation.pk}))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'application/pdf')
        self.assertTrue(response.content.startswith(b'%PDF'))

    def test_generate_quotation_pdf_survives_markup_like_text(self):
        """Company/grade text can come from a raw WhatsApp reply with no
        HTML-safety check — reportlab's Paragraph parses a real XML-like
        markup subset, so unescaped text containing '<', '>' or '&' used to
        raise a parse error inside doc.build() and silently kill the quote
        email (or 500 the staff PDF-download endpoint)."""
        customer = Customer.objects.create(name='<b>Evil & Co</b>', email='evil@example.com', phone='999')
        quotation = Quotation.objects.create(
            customer=customer, rate_per_kg=Decimal('50'), grade='<Foo & </para> Bar',
        )
        pdf_bytes = generate_quotation_pdf(quotation)
        self.assertTrue(pdf_bytes.startswith(b'%PDF'))


class QueryDashboardTests(TestCase):
    """Queries are logged before any Customer/Order exists — staff decide
    which ones to pursue by sending a quote (email, or a copy-link fallback
    when there's no address on file)."""

    def setUp(self):
        self.staff = User.objects.create_user('query_staff', password='pw', is_staff=True)
        self.product_type = ProductType.objects.create(item_code='Query Bar', grade='EN8D', size='1.200')

    def test_anonymous_cannot_view_dashboard(self):
        response = self.client.get(reverse('query_dashboard'))
        self.assertRedirects(response, f"{reverse('admin_login')}?next={reverse('query_dashboard')}")

    @patch('materials.views._send_whatsapp_template_message')
    def test_logging_a_query_with_minimal_fields(self, mock_send):
        self.client.force_login(self.staff)
        response = self.client.post(reverse('query_dashboard'), {
            'source': 'call', 'contact_phone': '9123456780',
        })
        self.assertRedirects(response, reverse('query_dashboard'))
        query = Query.objects.get(contact_phone='9123456780')
        self.assertEqual(query.source, 'call')
        self.assertEqual(query.status, 'new')
        self.assertEqual(query.company_name, '')
        mock_send.assert_called_once_with(
            '9123456780', WHATSAPP_QUERY_INTAKE_TEMPLATE, language=WHATSAPP_QUERY_INTAKE_TEMPLATE_LANGUAGE,
        )

    @patch('materials.views._send_whatsapp_template_message')
    def test_logging_a_query_normalizes_phone_to_digits_only(self, mock_send):
        self.client.force_login(self.staff)
        self.client.post(reverse('query_dashboard'), {
            'source': 'call', 'contact_phone': '+91 98765 43210',
        })
        query = Query.objects.get(contact_phone='919876543210')
        mock_send.assert_called_once_with(
            '919876543210', WHATSAPP_QUERY_INTAKE_TEMPLATE, language=WHATSAPP_QUERY_INTAKE_TEMPLATE_LANGUAGE,
        )

    @patch('materials.views._send_whatsapp_template_message')
    def test_logging_a_query_surfaces_warning_when_whatsapp_send_fails(self, mock_send):
        mock_send.side_effect = WhatsAppSendError("boom")
        self.client.force_login(self.staff)
        response = self.client.post(reverse('query_dashboard'), {
            'source': 'call', 'contact_phone': '9123456780',
        }, follow=True)

        self.assertTrue(Query.objects.filter(contact_phone='9123456780').exists())
        self.assertContains(response, "Please reach out directly")

    @patch('materials.views._send_whatsapp_template_message')
    def test_logging_a_query_twice_for_same_phone_is_rejected(self, mock_send):
        self.client.force_login(self.staff)
        self.client.post(reverse('query_dashboard'), {'source': 'call', 'contact_phone': '9123456780'})
        response = self.client.post(reverse('query_dashboard'), {'source': 'call', 'contact_phone': '9123456780'})

        self.assertEqual(Query.objects.filter(contact_phone='9123456780').count(), 1)
        self.assertContains(response, "already in progress")
        mock_send.assert_called_once()

    @patch('materials.views._send_whatsapp_template_message')
    def test_logging_a_query_for_same_phone_allowed_once_prior_query_closed(self, mock_send):
        self.client.force_login(self.staff)
        Query.objects.create(source='call', contact_phone='9123456780', status='converted')
        response = self.client.post(reverse('query_dashboard'), {'source': 'call', 'contact_phone': '9123456780'})

        self.assertRedirects(response, reverse('query_dashboard'))
        self.assertEqual(Query.objects.filter(contact_phone='9123456780').count(), 2)

    def test_logging_a_query_requires_phone_and_source(self):
        self.client.force_login(self.staff)
        response = self.client.post(reverse('query_dashboard'), {'source': 'call', 'contact_phone': ''})
        self.assertContains(response, "Phone number is required.")
        self.assertEqual(Query.objects.count(), 0)

        response = self.client.post(reverse('query_dashboard'), {'source': '', 'contact_phone': '9123456780'})
        self.assertContains(response, "Please select where this query came from.")
        self.assertEqual(Query.objects.count(), 0)

    def test_logging_a_query_rejects_incomplete_phone(self):
        # The field is pre-filled with "+91 " — submitting without adding
        # the actual number normalizes to just "91", not empty, so this
        # needs its own check beyond "phone number is required".
        self.client.force_login(self.staff)
        response = self.client.post(reverse('query_dashboard'), {'source': 'call', 'contact_phone': '+91 '})
        self.assertContains(response, "complete phone number")
        self.assertEqual(Query.objects.count(), 0)

    @override_settings(EMAIL_HOST_USER='sender@example.com', DEFAULT_FROM_EMAIL='sender@example.com')
    def test_send_quote_creates_customer_and_emails_the_link(self):
        query = Query.objects.create(source='referral', company_name='Referral Co', contact_email='ref@example.com')
        self.client.force_login(self.staff)
        response = self.client.post(
            reverse('query_send_quote', kwargs={'pk': query.pk}), {'rate_per_kg': '75.25'}, follow=True,
        )

        query.refresh_from_db()
        self.assertEqual(query.status, 'quote_sent')
        customer = Customer.objects.get(name='Referral Co')
        self.assertEqual(query.customer, customer)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn('ref@example.com', mail.outbox[0].to)
        quotation = Quotation.objects.get(customer=customer)
        self.assertEqual(quotation.source_query, query)
        self.assertEqual(quotation.rate_per_kg, Decimal('75.25'))
        self.assertContains(response, f"Quotation {quotation.formatted_no()} sent to {customer.email}")

    def test_send_quote_without_email_shows_copy_link_fallback(self):
        query = Query.objects.create(source='call', company_name='No Email Co', contact_phone='9999999999')
        self.client.force_login(self.staff)
        response = self.client.post(
            reverse('query_send_quote', kwargs={'pk': query.pk}), {'rate_per_kg': '75.25'}, follow=True,
        )

        query.refresh_from_db()
        self.assertEqual(query.status, 'quote_sent')
        self.assertEqual(len(mail.outbox), 0)
        self.assertContains(response, "Copy")

    def test_send_quote_rejects_missing_rate(self):
        query = Query.objects.create(source='call', company_name='Rateless Co', contact_email='rateless@example.com')
        self.client.force_login(self.staff)
        response = self.client.post(reverse('query_send_quote', kwargs={'pk': query.pk}), follow=True)

        query.refresh_from_db()
        self.assertEqual(query.status, 'new')
        self.assertContains(response, "Enter a valid rate per kg")
        self.assertEqual(Quotation.objects.count(), 0)
        self.assertEqual(len(mail.outbox), 0)

    def test_send_quote_prefills_grade_size_product_type_from_query(self):
        query = Query.objects.create(
            source='call', company_name='Prefill Rate Co', contact_email='prefill@example.com',
            product_type=self.product_type, grade='EN8D', size=Decimal('1.200'),
        )
        self.client.force_login(self.staff)
        self.client.post(reverse('query_send_quote', kwargs={'pk': query.pk}), {'rate_per_kg': '60'})

        quotation = Quotation.objects.get(source_query=query)
        self.assertEqual(quotation.grade, 'EN8D')
        self.assertEqual(quotation.size, Decimal('1.200'))
        self.assertEqual(quotation.product_type, self.product_type)

    @override_settings(
        PUBLIC_QUOTE_BASE_URL='https://quote.mattadrawing.com',
        ALLOWED_HOSTS=['mdw.tail2734e7.ts.net', 'testserver'],
    )
    def test_copy_link_uses_public_base_url_not_the_admin_request_host(self):
        """Same private-address problem as the emailed link — the Copy Link
        fallback previously built its URL from the admin's own request
        host instead of PUBLIC_QUOTE_BASE_URL."""
        query = Query.objects.create(source='call', company_name='No Email Co', contact_phone='9999999999')
        self.client.force_login(self.staff)
        self.client.post(reverse('query_send_quote', kwargs={'pk': query.pk}), {'rate_per_kg': '75.25'})
        query.refresh_from_db()

        response = self.client.get(reverse('query_dashboard'), HTTP_HOST='mdw.tail2734e7.ts.net')
        self.assertContains(response, f"https://quote.mattadrawing.com/quote/{query.customer.quote_token}/")
        self.assertNotContains(response, 'tail2734e7')

    def test_not_interested_sets_status(self):
        query = Query.objects.create(source='other', company_name='Dead End Co')
        self.client.force_login(self.staff)
        self.client.post(reverse('query_not_interested', kwargs={'pk': query.pk}))
        query.refresh_from_db()
        self.assertEqual(query.status, 'not_interested')

    def test_anonymous_cannot_edit_query(self):
        query = Query.objects.create(source='call', contact_phone='9123456780')
        response = self.client.get(reverse('query_edit', kwargs={'pk': query.pk}))
        self.assertRedirects(response, reverse('home'))

    def test_edit_query_updates_fields(self):
        query = Query.objects.create(source='whatsapp', contact_phone='919123456780')
        self.client.force_login(self.staff)
        response = self.client.post(reverse('query_edit', kwargs={'pk': query.pk}), {
            'company_name': 'Fixed Co Name', 'contact_phone': '+91 91234 56780',
            'contact_email': 'fixed@example.com', 'grade': 'EN8D',
            'size': '1.200', 'quantity': '500', 'notes': 'corrected via dashboard',
        })
        self.assertRedirects(response, reverse('query_dashboard'))
        query.refresh_from_db()
        self.assertEqual(query.company_name, 'Fixed Co Name')
        self.assertEqual(query.contact_phone, '919123456780')
        self.assertEqual(query.contact_email, 'fixed@example.com')
        self.assertEqual(query.grade, 'EN8D')
        self.assertEqual(query.size, Decimal('1.200'))
        self.assertEqual(query.quantity, Decimal('500'))
        self.assertEqual(query.notes, 'corrected via dashboard')

    def test_edit_query_links_product_type(self):
        product_type = ProductType.objects.create(item_code='Edit Bar', grade='SS304', size='2.500')
        query = Query.objects.create(source='call', contact_phone='9123456780')
        self.client.force_login(self.staff)
        self.client.post(reverse('query_edit', kwargs={'pk': query.pk}), {
            'company_name': '', 'contact_phone': '9123456780', 'contact_email': '',
            'product_type': str(product_type.pk), 'grade': '', 'size': '', 'quantity': '', 'notes': '',
        })
        query.refresh_from_db()
        self.assertEqual(query.product_type, product_type)

    def test_edit_query_rejects_invalid_size(self):
        query = Query.objects.create(source='call', contact_phone='9123456780')
        self.client.force_login(self.staff)
        response = self.client.post(reverse('query_edit', kwargs={'pk': query.pk}), {
            'company_name': '', 'contact_phone': '9123456780', 'contact_email': '',
            'grade': '', 'size': 'not-a-number', 'quantity': '', 'notes': '',
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "must be a decimal number")
        query.refresh_from_db()
        self.assertIsNone(query.size)

    def test_edit_query_does_not_change_status_or_source(self):
        query = Query.objects.create(source='call', contact_phone='9123456780', status='quote_sent')
        self.client.force_login(self.staff)
        self.client.post(reverse('query_edit', kwargs={'pk': query.pk}), {
            'company_name': 'Renamed Co', 'contact_phone': '9123456780', 'contact_email': '',
            'grade': '', 'size': '', 'quantity': '', 'notes': '',
        })
        query.refresh_from_db()
        self.assertEqual(query.source, 'call')
        self.assertEqual(query.status, 'quote_sent')

    def test_quote_form_prefills_from_in_flight_query(self):
        customer = Customer.objects.create(name='Prefill Co')
        Query.objects.create(
            source='call', company_name='Prefill Co', customer=customer, status='quote_sent',
            product_type=self.product_type, grade='EN8D', size=Decimal('1.200'),
            quantity=Decimal('750'), notes='Call back before Friday',
        )
        response = self.client.get(reverse('quote_form', kwargs={'token': customer.quote_token}))
        self.assertContains(response, 'value="EN8D"')
        self.assertContains(response, 'value="1.200"')
        self.assertContains(response, 'value="750.000"')
        self.assertContains(response, 'Call back before Friday')

    def test_quote_form_blank_when_no_query(self):
        customer = Customer.objects.create(name='No Query Co')
        response = self.client.get(reverse('quote_form', kwargs={'token': customer.quote_token}))
        self.assertNotContains(response, 'value="EN8D"')

    def test_submitting_quote_form_converts_query_and_links_order(self):
        customer = Customer.objects.create(name='Convert Co')
        query = Query.objects.create(
            source='indiamart', company_name='Convert Co', customer=customer, status='quote_sent',
        )
        self.client.post(reverse('quote_form', kwargs={'token': customer.quote_token}), {'quantity': '250'})

        query.refresh_from_db()
        self.assertEqual(query.status, 'converted')
        order = Order.objects.get(customer=customer)
        self.assertEqual(order.source_query, query)

    @override_settings(EMAIL_HOST_USER='sender@example.com', DEFAULT_FROM_EMAIL='sender@example.com')
    def test_send_quote_falls_back_to_phone_when_no_company_name(self):
        query = Query.objects.create(source='whatsapp', contact_phone='9998887776')
        self.client.force_login(self.staff)
        self.client.post(reverse('query_send_quote', kwargs={'pk': query.pk}), {'rate_per_kg': '40'})

        query.refresh_from_db()
        customer = Customer.objects.get(name='9998887776')
        self.assertEqual(query.customer, customer)


def _sign_whatsapp_payload(body_bytes, secret):
    digest = hmac.new(secret.encode('utf-8'), body_bytes, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


@override_settings(WHATSAPP_VERIFY_TOKEN='test-verify-token', WHATSAPP_APP_SECRET='test-app-secret')
class WhatsAppWebhookTests(TestCase):
    """Meta's Cloud API is the only caller of this endpoint — GET performs
    the one-time subscription handshake, POST delivers inbound messages.
    Both are unauthenticated by Django's usual means, so signature/token
    verification IS the security boundary being tested here."""

    def _post_payload(self, payload, secret='test-app-secret'):
        body = json.dumps(payload).encode('utf-8')
        return self.client.post(
            reverse('whatsapp_webhook'), data=body, content_type='application/json',
            HTTP_X_HUB_SIGNATURE_256=_sign_whatsapp_payload(body, secret),
        )

    def _message_payload(self, phone, text, profile_name=None):
        value = {'messages': [{'from': phone, 'text': {'body': text}}]}
        if profile_name is not None:
            value['contacts'] = [{'profile': {'name': profile_name}}]
        return {'entry': [{'changes': [{'value': value}]}]}

    def test_get_handshake_succeeds_with_correct_verify_token(self):
        response = self.client.get(reverse('whatsapp_webhook'), {
            'hub.mode': 'subscribe', 'hub.verify_token': 'test-verify-token', 'hub.challenge': '12345',
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content.decode(), '12345')

    def test_get_handshake_rejects_wrong_verify_token(self):
        response = self.client.get(reverse('whatsapp_webhook'), {
            'hub.mode': 'subscribe', 'hub.verify_token': 'wrong-token', 'hub.challenge': '12345',
        })
        self.assertEqual(response.status_code, 403)

    @override_settings(WHATSAPP_VERIFY_TOKEN='')
    def test_get_handshake_rejects_when_verify_token_unset(self):
        # An unset WHATSAPP_VERIFY_TOKEN must never "verify" anything, even
        # a request that also omits hub.verify_token ('' == '' would
        # otherwise pass).
        response = self.client.get(reverse('whatsapp_webhook'), {
            'hub.mode': 'subscribe', 'hub.challenge': '12345',
        })
        self.assertEqual(response.status_code, 403)

    def test_post_with_no_matching_query_creates_bare_query(self):
        payload = self._message_payload('919876543210', 'Need 500kg EN8D', profile_name='Ramesh Traders')
        response = self._post_payload(payload)

        self.assertEqual(response.status_code, 200)
        query = Query.objects.get(contact_phone='919876543210')
        self.assertEqual(query.source, 'whatsapp')
        self.assertEqual(query.company_name, 'Ramesh Traders')
        self.assertIn('Need 500kg EN8D', query.notes)

    def test_post_does_not_reuse_converted_query(self):
        Query.objects.create(
            source='whatsapp', contact_phone='919876543210', notes='old conversation', status='converted',
        )
        payload = self._message_payload('919876543210', 'new inquiry')
        self._post_payload(payload)

        self.assertEqual(Query.objects.filter(contact_phone='919876543210').count(), 2)
        new_query = Query.objects.filter(contact_phone='919876543210', status='new').get()
        self.assertEqual(new_query.notes, 'new inquiry')

    @patch('materials.views._send_whatsapp_text_message_background')
    def test_inbound_answer_captures_company_name_then_asks_email(self, mock_send):
        Query.objects.create(source='call', contact_phone='919876543210')
        payload = self._message_payload('919876543210', 'Ramesh Traders')
        self._post_payload(payload)

        query = Query.objects.get(contact_phone='919876543210')
        self.assertEqual(query.company_name, 'Ramesh Traders')
        mock_send.assert_called_once_with('919876543210', WHATSAPP_QUERY_QUESTIONS['contact_email'])

    @patch('materials.views._send_whatsapp_text_message_background')
    def test_inbound_answer_captures_email_then_asks_grade(self, mock_send):
        Query.objects.create(source='call', contact_phone='919876543210', company_name='Ramesh Traders')
        payload = self._message_payload('919876543210', 'ramesh@example.com')
        self._post_payload(payload)

        query = Query.objects.get(contact_phone='919876543210')
        self.assertEqual(query.contact_email, 'ramesh@example.com')
        mock_send.assert_called_once_with('919876543210', WHATSAPP_QUERY_QUESTIONS['grade'])

    @patch('materials.views._send_whatsapp_text_message_background')
    def test_inbound_answer_captures_grade_then_asks_size(self, mock_send):
        Query.objects.create(
            source='call', contact_phone='919876543210',
            company_name='Ramesh Traders', contact_email='ramesh@example.com',
        )
        payload = self._message_payload('919876543210', 'EN8D')
        self._post_payload(payload)

        query = Query.objects.get(contact_phone='919876543210')
        self.assertEqual(query.grade, 'EN8D')
        mock_send.assert_called_once_with('919876543210', WHATSAPP_QUERY_QUESTIONS['size'])

    @patch('materials.views._send_whatsapp_text_message_background')
    def test_inbound_size_answer_completes_sequence_and_sends_closing(self, mock_send):
        Query.objects.create(
            source='call', contact_phone='919876543210', company_name='Ramesh Traders',
            contact_email='ramesh@example.com', grade='EN8D',
        )
        payload = self._message_payload('919876543210', '1.2')
        self._post_payload(payload)

        query = Query.objects.get(contact_phone='919876543210')
        self.assertEqual(query.size, Decimal('1.2'))
        mock_send.assert_called_once_with('919876543210', WHATSAPP_CLOSING_MESSAGE)

    @patch('materials.views._send_whatsapp_text_message_background')
    def test_inbound_size_with_units_is_parsed(self, mock_send):
        Query.objects.create(
            source='call', contact_phone='919876543210', company_name='Ramesh Traders',
            contact_email='ramesh@example.com', grade='EN8D',
        )
        self._post_payload(self._message_payload('919876543210', '1.2mm'))

        query = Query.objects.get(contact_phone='919876543210')
        self.assertEqual(query.size, Decimal('1.2'))

    @patch('materials.views._send_whatsapp_text_message_background')
    def test_inbound_unparseable_size_reasks_without_saving(self, mock_send):
        Query.objects.create(
            source='call', contact_phone='919876543210', company_name='Ramesh Traders',
            contact_email='ramesh@example.com', grade='EN8D',
        )
        self._post_payload(self._message_payload('919876543210', 'not sure'))

        query = Query.objects.get(contact_phone='919876543210')
        self.assertIsNone(query.size)
        mock_send.assert_called_once_with('919876543210', WHATSAPP_QUERY_QUESTIONS['size'])

    @patch('materials.views._send_whatsapp_text_message_background')
    def test_inbound_negative_size_reasks_without_saving(self, mock_send):
        Query.objects.create(
            source='call', contact_phone='919876543210', company_name='Ramesh Traders',
            contact_email='ramesh@example.com', grade='EN8D',
        )
        self._post_payload(self._message_payload('919876543210', '-1.2'))

        query = Query.objects.get(contact_phone='919876543210')
        self.assertIsNone(query.size)
        mock_send.assert_called_once_with('919876543210', WHATSAPP_QUERY_QUESTIONS['size'])

    @patch('materials.views._send_whatsapp_text_message_background')
    def test_inbound_invalid_email_reasks_without_saving(self, mock_send):
        Query.objects.create(source='call', contact_phone='919876543210', company_name='Ramesh Traders')
        self._post_payload(self._message_payload('919876543210', 'not an email'))

        query = Query.objects.get(contact_phone='919876543210')
        self.assertEqual(query.contact_email, '')
        mock_send.assert_called_once_with('919876543210', WHATSAPP_QUERY_QUESTIONS['contact_email'])

    @patch('materials.views._send_whatsapp_text_message_background')
    def test_size_completion_matches_existing_product_type(self, mock_send):
        product_type = ProductType.objects.create(item_code='Matched Bar', grade='EN8D', size='1.200')
        Query.objects.create(
            source='call', contact_phone='919876543210', company_name='Ramesh Traders',
            contact_email='ramesh@example.com', grade='en8d',
        )
        self._post_payload(self._message_payload('919876543210', '1.2'))

        query = Query.objects.get(contact_phone='919876543210')
        self.assertEqual(query.product_type, product_type)

    @patch('materials.views._send_whatsapp_text_message_background')
    def test_size_completion_leaves_product_type_null_when_no_match(self, mock_send):
        Query.objects.create(
            source='call', contact_phone='919876543210', company_name='Ramesh Traders',
            contact_email='ramesh@example.com', grade='EN8D',
        )
        self._post_payload(self._message_payload('919876543210', '9.9'))

        query = Query.objects.get(contact_phone='919876543210')
        self.assertIsNone(query.product_type)

    @patch('materials.views._send_whatsapp_text_message_background')
    def test_size_completion_requires_both_grade_and_size_to_match(self, mock_send):
        # Same grade, different size — and same size, different grade —
        # must NOT match; only a ProductType agreeing on both should link.
        ProductType.objects.create(item_code='Wrong Size Bar', grade='EN8D', size='2.500')
        ProductType.objects.create(item_code='Wrong Grade Bar', grade='SS304', size='1.200')
        Query.objects.create(
            source='call', contact_phone='919876543210', company_name='Ramesh Traders',
            contact_email='ramesh@example.com', grade='EN8D',
        )
        self._post_payload(self._message_payload('919876543210', '1.2'))

        query = Query.objects.get(contact_phone='919876543210')
        self.assertIsNone(query.product_type)

    @patch('materials.views._send_whatsapp_text_message_background')
    def test_message_after_sequence_complete_is_appended_to_notes(self, mock_send):
        Query.objects.create(
            source='call', contact_phone='919876543210', company_name='Ramesh Traders',
            contact_email='ramesh@example.com', grade='EN8D', size=Decimal('1.2'),
        )
        self._post_payload(self._message_payload('919876543210', 'also need it urgently'))

        query = Query.objects.get(contact_phone='919876543210')
        self.assertIn('also need it urgently', query.notes)
        mock_send.assert_not_called()

    @patch('materials.views._send_whatsapp_text_message')
    def test_answer_capture_survives_send_failure(self, mock_send):
        mock_send.side_effect = WhatsAppSendError("boom")
        Query.objects.create(source='call', contact_phone='919876543210')
        self._post_payload(self._message_payload('919876543210', 'Ramesh Traders'))

        query = Query.objects.get(contact_phone='919876543210')
        self.assertEqual(query.company_name, 'Ramesh Traders')

    @patch('materials.views._send_whatsapp_text_message')
    def test_background_send_logs_and_swallows_failure(self, mock_send):
        # _send_whatsapp_text_message_background is what _process_whatsapp_answer
        # actually calls — it must never let a send failure propagate (the
        # webhook has already acked by the time this thread runs), but a
        # persistent failure (e.g. an expired access token) should still be
        # visible somewhere, via a log line.
        mock_send.side_effect = WhatsAppSendError("boom")
        with self.assertLogs('materials.views', level='WARNING') as logs:
            thread = views._send_whatsapp_text_message_background('919876543210', 'hello')
            thread.join(timeout=2)
        self.assertTrue(any('919876543210' in line and 'boom' in line for line in logs.output))

    def test_post_with_invalid_signature_is_rejected(self):
        payload = self._message_payload('919876543210', 'Need 500kg EN8D')
        response = self._post_payload(payload, secret='wrong-secret')

        self.assertEqual(response.status_code, 403)
        self.assertEqual(Query.objects.count(), 0)

    def test_post_with_missing_signature_header_is_rejected(self):
        body = json.dumps(self._message_payload('919876543210', 'Need 500kg EN8D')).encode('utf-8')
        response = self.client.post(reverse('whatsapp_webhook'), data=body, content_type='application/json')

        self.assertEqual(response.status_code, 403)
        self.assertEqual(Query.objects.count(), 0)

    @override_settings(WHATSAPP_APP_SECRET='')
    def test_post_is_rejected_when_app_secret_unset(self):
        # An unset WHATSAPP_APP_SECRET must never validate anything — HMAC
        # keyed with an empty string is a key anyone can also compute, so
        # this must reject even a signature computed the "correct" way
        # against that same empty secret.
        payload = self._message_payload('919876543210', 'Need 500kg EN8D')
        response = self._post_payload(payload, secret='')

        self.assertEqual(response.status_code, 403)
        self.assertEqual(Query.objects.count(), 0)

    @patch('materials.views._send_whatsapp_text_message_background')
    @patch('materials.views._send_whatsapp_template_message')
    def test_phone_with_symbols_still_routes_to_open_query(self, mock_template_send, mock_text_send):
        # Staff may log a number formatted like the UI's own placeholder
        # ("+91 98765 43210") — Meta's inbound "from" is always digits-only,
        # so the stored value must be normalized the same way at write time
        # for the two to ever match.
        staff = User.objects.create_user('symbol_phone_staff', password='pw', is_staff=True)
        self.client.force_login(staff)
        self.client.post(reverse('query_dashboard'), {
            'source': 'call', 'contact_phone': '+91 98765 43210',
        })

        payload = self._message_payload('919876543210', 'Ramesh Traders')
        self._post_payload(payload)

        self.assertEqual(Query.objects.filter(contact_phone='919876543210').count(), 1)
        query = Query.objects.get(contact_phone='919876543210')
        self.assertEqual(query.company_name, 'Ramesh Traders')

    def test_post_ignores_status_payloads(self):
        payload = {'entry': [{'changes': [{'value': {'statuses': [{'id': 'abc', 'status': 'delivered'}]}}]}]}
        response = self._post_payload(payload)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(Query.objects.count(), 0)
