"""Non-coil entries, the finance login and the finance page where the supplier's bill is uploaded."""

import shutil
import tempfile

from django.conf import settings
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from ..models import GateEntry, NonCoilEntry

MEDIA = tempfile.mkdtemp()


def bill_file(name='bill.pdf', content=b'%PDF-1.4 fake bill'):
    return SimpleUploadedFile(name, content)


def employee_login(client):
    client.post(reverse('employee_login'), {'pin': settings.EMPLOYEE_PIN})


def finance_login(client):
    return client.post(reverse('finance_login'), {'pin': settings.FINANCE_CODE})


@override_settings(MEDIA_ROOT=MEDIA)
class FinanceTestCase(TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(MEDIA, ignore_errors=True)


class NonCoilEntryFormTests(TestCase):
    def setUp(self):
        employee_login(self.client)

    def test_requires_the_employee_pin(self):
        self.client.post(reverse('employee_logout'))
        response = self.client.get(reverse('non_coil_entry_form'))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse('employee_login'), response['Location'])

    def test_saving_records_category_company_and_the_time_in_capitals(self):
        response = self.client.post(reverse('non_coil_entry_form'), {'category': ' dies ', 'company': 'abc tools'})
        self.assertRedirects(response, reverse('non_coil_entry_form'))
        entry = NonCoilEntry.objects.get()
        self.assertEqual((entry.category, entry.company), ('DIES', 'ABC TOOLS'))
        self.assertIsNotNone(entry.arrived_at)
        self.assertFalse(entry.has_bill())

    def test_shows_a_confirmation_after_saving(self):
        response = self.client.post(reverse('non_coil_entry_form'), {'category': 'dies', 'company': 'abc'}, follow=True)
        self.assertContains(response, 'Saved: DIES from ABC.')

    def test_both_fields_are_required(self):
        for data in ({'category': 'dies'}, {'company': 'abc'}, {}):
            with self.subTest(data=data):
                response = self.client.post(reverse('non_coil_entry_form'), data)
                self.assertEqual(response.status_code, 200)
        self.assertFalse(NonCoilEntry.objects.exists())

    def test_portal_gate_entry_button_opens_a_choice_of_two(self):
        home = self.client.get(reverse('employee'))
        self.assertContains(home, reverse('gate_entry_choice'))
        self.assertNotContains(home, reverse('non_coil_entry_form'))   # no extra tile on the portal home
        choice = self.client.get(reverse('gate_entry_choice'))
        self.assertContains(choice, reverse('gate_entry_form'))
        self.assertContains(choice, reverse('non_coil_entry_form'))

    def test_the_choice_page_needs_the_employee_pin(self):
        self.client.post(reverse('employee_logout'))
        response = self.client.get(reverse('gate_entry_choice'))
        self.assertIn(reverse('employee_login'), response['Location'])


class FinanceLoginTests(TestCase):
    def test_right_code_opens_the_finance_page(self):
        response = finance_login(self.client)
        self.assertRedirects(response, reverse('finance'))
        self.assertEqual(self.client.get(reverse('finance')).status_code, 200)

    def test_wrong_code_is_refused(self):
        response = self.client.post(reverse('finance_login'), {'pin': 'nope'})
        self.assertContains(response, 'Incorrect code.')
        self.assertEqual(self.client.get(reverse('finance')).status_code, 302)

    def test_too_many_wrong_codes_lock_out_even_the_right_one(self):
        from django.core.cache import cache
        cache.clear()
        for _ in range(settings.EMPLOYEE_LOGIN_MAX_FAILURES):
            self.client.post(reverse('finance_login'), {'pin': 'nope'})
        response = finance_login(self.client)
        self.assertContains(response, 'Too many incorrect attempts')
        cache.clear()

    def test_finance_lockout_is_separate_from_the_employee_one(self):
        from django.core.cache import cache
        cache.clear()
        for _ in range(settings.EMPLOYEE_LOGIN_MAX_FAILURES):
            self.client.post(reverse('employee_login'), {'pin': 'nope'})
        self.assertRedirects(finance_login(self.client), reverse('finance'))
        cache.clear()

    def test_logout_clears_only_the_finance_flag(self):
        finance_login(self.client)
        self.client.post(reverse('finance_logout'))
        self.assertEqual(self.client.get(reverse('finance')).status_code, 302)

    def test_get_does_not_log_out(self):
        finance_login(self.client)
        self.client.get(reverse('finance_logout'))
        self.assertEqual(self.client.get(reverse('finance')).status_code, 200)

    def test_offsite_next_is_ignored(self):
        response = self.client.post(reverse('finance_login'), {'pin': settings.FINANCE_CODE, 'next': 'https://evil.example.com/'})
        self.assertEqual(response.url, reverse('finance'))


class FinanceAccessSeparationTests(TestCase):
    """The finance code opens only the finance pages; the employee PIN does not open them."""

    def _finance_urls(self):
        return [reverse('finance'), reverse('finance_coil_bill', args=[1]), reverse('finance_other_bill', args=[1])]

    def test_employee_pin_does_not_open_finance(self):
        employee_login(self.client)
        for url in self._finance_urls():
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 302)
                self.assertIn(reverse('finance_login'), response['Location'])

    def test_finance_code_does_not_open_employee_or_staff_pages(self):
        finance_login(self.client)
        for name in ('employee', 'gate_entry_form', 'non_coil_entry_form', 'select_order', 'order_dashboard', 'query_dashboard'):
            with self.subTest(view=name):
                response = self.client.get(reverse(name))
                self.assertEqual(response.status_code, 302)
                self.assertNotEqual(response['Location'], reverse('finance'))

    def test_a_staff_login_can_open_the_finance_page(self):
        User.objects.create_user('owner', password='pw', is_staff=True)
        self.client.login(username='owner', password='pw')
        self.assertEqual(self.client.get(reverse('finance')).status_code, 200)

    def test_a_non_staff_login_cannot(self):
        User.objects.create_user('plain', password='pw')
        self.client.login(username='plain', password='pw')
        self.assertEqual(self.client.get(reverse('finance')).status_code, 302)


class FinanceDashboardTests(FinanceTestCase):
    def setUp(self):
        finance_login(self.client)

    def _coil_entry(self, vendor='TATA', bill=False):
        entry = GateEntry.objects.create(vendor=vendor, invoice_no='INV1', total_weight=1000)
        if bill:
            entry.bill = bill_file()
            entry.save()
        return entry

    def test_two_sections_with_the_entries_of_each_kind(self):
        self._coil_entry('TATA')
        NonCoilEntry.objects.create(category='DIES', company='ABC TOOLS')
        response = self.client.get(reverse('finance'))
        self.assertContains(response, 'Coil gate entries')
        self.assertContains(response, 'Other arrivals')
        self.assertContains(response, 'TATA')
        self.assertContains(response, 'ABC TOOLS')

    def test_entries_without_a_bill_come_first(self):
        old_waiting = self._coil_entry('OLDWAITING')
        newer_billed = self._coil_entry('NEWERBILLED', bill=True)
        entries = self.client.get(reverse('finance')).context['coil_entries']
        self.assertEqual([e.pk for e in entries], [old_waiting.pk, newer_billed.pk])

        waiting = NonCoilEntry.objects.create(category='A', company='A')
        billed = NonCoilEntry.objects.create(category='B', company='B', bill=bill_file())
        NonCoilEntry.objects.filter(pk=waiting.pk).update(arrived_at='2020-01-01T00:00:00Z')   # older, still first
        entries = self.client.get(reverse('finance')).context['other_entries']
        self.assertEqual([e.pk for e in entries], [waiting.pk, billed.pk])

    def test_counts_the_entries_still_waiting(self):
        self._coil_entry('A')
        self._coil_entry('B', bill=True)
        context = self.client.get(reverse('finance')).context
        self.assertEqual(context['coil_waiting_count'], 1)

    def test_uploading_a_coil_bill(self):
        entry = self._coil_entry()
        response = self.client.post(reverse('finance_upload_coil_bill', args=[entry.pk]), {'bill': bill_file()}, follow=True)
        self.assertContains(response, 'Bill uploaded.')
        entry.refresh_from_db()
        self.assertTrue(entry.has_bill())
        self.assertIsNotNone(entry.bill_uploaded_at)

    def test_uploading_a_non_coil_bill_and_replacing_it(self):
        entry = NonCoilEntry.objects.create(category='DIES', company='ABC')
        url = reverse('finance_upload_other_bill', args=[entry.pk])
        self.client.post(url, {'bill': bill_file('first.pdf')})
        first = NonCoilEntry.objects.get(pk=entry.pk).bill.name
        response = self.client.post(url, {'bill': bill_file('second.pdf')}, follow=True)
        self.assertContains(response, 'Bill replaced.')
        self.assertNotEqual(NonCoilEntry.objects.get(pk=entry.pk).bill.name, first)

    def test_a_file_type_that_is_not_a_bill_is_refused(self):
        entry = NonCoilEntry.objects.create(category='DIES', company='ABC')
        response = self.client.post(reverse('finance_upload_other_bill', args=[entry.pk]),
                                    {'bill': bill_file('evil.html', b'<script>')}, follow=True)
        self.assertContains(response, 'not allowed')
        self.assertFalse(NonCoilEntry.objects.get(pk=entry.pk).has_bill())

    def test_a_file_over_ten_megabytes_is_refused(self):
        entry = NonCoilEntry.objects.create(category='DIES', company='ABC')
        big = SimpleUploadedFile('big.pdf', b'x' * (10 * 1024 * 1024 + 1))
        response = self.client.post(reverse('finance_upload_other_bill', args=[entry.pk]), {'bill': big}, follow=True)
        self.assertContains(response, 'too large')
        self.assertFalse(NonCoilEntry.objects.get(pk=entry.pk).has_bill())

    def test_no_file_is_refused(self):
        entry = NonCoilEntry.objects.create(category='DIES', company='ABC')
        response = self.client.post(reverse('finance_upload_other_bill', args=[entry.pk]), {}, follow=True)
        self.assertFalse(NonCoilEntry.objects.get(pk=entry.pk).has_bill())
        self.assertContains(response, 'alert-error')

    def test_get_on_the_upload_route_changes_nothing(self):
        entry = NonCoilEntry.objects.create(category='DIES', company='ABC')
        response = self.client.get(reverse('finance_upload_other_bill', args=[entry.pk]))
        self.assertRedirects(response, reverse('finance'))

    def test_viewing_a_bill(self):
        entry = NonCoilEntry.objects.create(category='DIES', company='ABC', bill=bill_file())
        response = self.client.get(reverse('finance_other_bill', args=[entry.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'application/pdf')
        self.assertEqual(b''.join(response.streaming_content), b'%PDF-1.4 fake bill')

    def test_viewing_a_missing_bill_is_a_404(self):
        entry = NonCoilEntry.objects.create(category='DIES', company='ABC')
        self.assertEqual(self.client.get(reverse('finance_other_bill', args=[entry.pk])).status_code, 404)

    def test_the_media_route_stays_staff_only_for_bills(self):
        entry = NonCoilEntry.objects.create(category='DIES', company='ABC', bill=bill_file())
        response = self.client.get(f'/media/{entry.bill.name}')
        self.assertEqual(response.status_code, 302)   # a finance session is not staff
