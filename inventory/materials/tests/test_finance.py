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
    client.post(reverse('login'), {'pin': settings.EMPLOYEE_PIN})


def finance_login(client):
    return client.post(reverse('login'), {'pin': settings.FINANCE_CODE})


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
        self.assertIn(reverse('login'), response['Location'])

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
        self.assertIn(reverse('login'), response['Location'])


class FinanceLoginTests(TestCase):
    def setUp(self):
        from django.core.cache import cache
        cache.clear()   # the failure counter is shared across tests in this process

    def test_right_code_opens_the_finance_page(self):
        response = finance_login(self.client)
        self.assertRedirects(response, reverse('finance'))
        self.assertEqual(self.client.get(reverse('finance')).status_code, 200)

    def test_wrong_code_is_refused(self):
        response = self.client.post(reverse('login'), {'pin': 'nope'})
        self.assertContains(response, 'Incorrect code.')
        self.assertEqual(self.client.get(reverse('finance')).status_code, 302)

    def test_too_many_wrong_codes_lock_out_even_the_right_one(self):
        from django.core.cache import cache
        cache.clear()
        for _ in range(settings.EMPLOYEE_LOGIN_MAX_FAILURES):
            self.client.post(reverse('login'), {'pin': 'nope'})
        response = finance_login(self.client)
        self.assertContains(response, 'Too many incorrect attempts')
        cache.clear()

    def test_the_lockout_is_shared_with_the_employee_code_because_it_is_one_login_page(self):
        from django.core.cache import cache
        cache.clear()
        for _ in range(settings.EMPLOYEE_LOGIN_MAX_FAILURES):
            self.client.post(reverse('login'), {'pin': 'nope'})
        self.assertContains(finance_login(self.client), 'Too many incorrect attempts')
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
        response = self.client.post(reverse('login'), {'pin': settings.FINANCE_CODE, 'next': 'https://evil.example.com/'})
        self.assertEqual(response['Location'], reverse('finance'))


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
                self.assertIn(reverse('login'), response['Location'])

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


class CommonLoginPageTests(TestCase):
    """One login page: the code typed decides which dashboard opens."""

    def setUp(self):
        from django.core.cache import cache
        cache.clear()

    def _post(self, code, **extra):
        return self.client.post(reverse('login'), {'pin': code, **extra})

    def test_the_employee_code_opens_the_employee_portal(self):
        self.assertRedirects(self._post(settings.EMPLOYEE_PIN), reverse('employee'))
        self.assertTrue(self.client.session.get('employee_auth'))
        self.assertFalse(self.client.session.get('finance_auth'))

    def test_the_finance_code_opens_the_finance_page(self):
        self.assertRedirects(self._post(settings.FINANCE_CODE), reverse('finance'))
        self.assertTrue(self.client.session.get('finance_auth'))
        self.assertFalse(self.client.session.get('employee_auth'))

    def test_a_wrong_code_gets_one_message_for_every_role(self):
        self.assertContains(self._post('0000'), 'Incorrect code.')
        self.assertFalse(self.client.session.get('employee_auth') or self.client.session.get('finance_auth'))

    def test_the_page_offers_the_admin_login(self):
        page = self.client.get(reverse('login'))
        self.assertContains(page, 'Login as admin instead')
        self.assertContains(page, reverse('admin_login'))

    def test_next_is_followed_only_when_the_role_may_open_it(self):
        # an employee heading for an employee page goes there
        r = self._post(settings.EMPLOYEE_PIN, next=reverse('select_order'))
        self.assertEqual(r['Location'], reverse('select_order'))
        self.client.post(reverse('employee_logout'))
        # an employee code with a finance page as `next`: lands on the employee dashboard instead
        r = self._post(settings.EMPLOYEE_PIN, next=reverse('finance'))
        self.assertEqual(r['Location'], reverse('employee'))
        self.client.post(reverse('employee_logout'))
        # a finance code with an employee page as `next`: lands on the finance page
        r = self._post(settings.FINANCE_CODE, next=reverse('select_order'))
        self.assertEqual(r['Location'], reverse('finance'))

    def test_an_already_signed_in_browser_skips_the_page(self):
        self._post(settings.FINANCE_CODE)
        self.assertRedirects(self.client.get(reverse('login')), reverse('finance'))

    def test_the_old_addresses_show_the_same_page(self):
        for name in ('employee_login', 'finance_login'):
            with self.subTest(old_address=name):
                self.assertContains(self.client.get(reverse(name)), 'Login as admin instead')
        self.assertRedirects(self.client.post(reverse('finance_login'), {'pin': settings.FINANCE_CODE}), reverse('finance'))

    def test_the_two_codes_must_differ(self):
        import os
        import subprocess
        import sys
        env = {k: v for k, v in os.environ.items() if k not in ('EMPLOYEE_PIN', 'FINANCE_CODE')}
        env.update({'DJANGO_DEBUG': 'True', 'EMPLOYEE_PIN': '4321', 'FINANCE_CODE': '4321'})
        result = subprocess.run([sys.executable, '-c', 'import inventory.settings'],
                                cwd=settings.BASE_DIR, env=env, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('must be different', result.stderr)


class WelcomeAndStaffDashboardTests(TestCase):
    def test_the_main_address_is_the_welcome_page(self):
        response = self.client.get('/')
        self.assertContains(response, 'Matta Drawing Works')
        self.assertContains(response, reverse('login'))

    def test_welcome_signs_out_both_shared_codes_but_not_a_staff_login(self):
        User.objects.create_user('boss', password='pw', is_staff=True)
        self.client.login(username='boss', password='pw')
        session = self.client.session
        session['employee_auth'] = True
        session['finance_auth'] = True
        session.save()
        self.client.get(reverse('welcome'))
        self.assertFalse(self.client.session.get('employee_auth'))
        self.assertFalse(self.client.session.get('finance_auth'))
        self.assertEqual(self.client.get(reverse('staff_home')).status_code, 200)   # still signed in as staff

    def test_admin_login_lands_on_the_staff_dashboard(self):
        User.objects.create_user('boss', password='pw', is_staff=True)
        response = self.client.post(reverse('admin_login'), {'username': 'boss', 'password': 'pw'})
        self.assertRedirects(response, reverse('staff_home'))

    def test_the_staff_dashboard_links_to_orders_queries_and_the_django_admin_without_another_login(self):
        User.objects.create_user('boss', password='pw', is_staff=True)
        self.client.login(username='boss', password='pw')
        page = self.client.get(reverse('staff_home'))
        for url in (reverse('order_dashboard'), reverse('query_dashboard'), '/admin/'):
            self.assertContains(page, f'href="{url}"')
            self.assertEqual(self.client.get(url).status_code, 200)   # opens straight away: no login page in between

    def test_the_staff_dashboard_is_staff_only(self):
        response = self.client.get(reverse('staff_home'))
        self.assertIn(reverse('admin_login'), response['Location'])
