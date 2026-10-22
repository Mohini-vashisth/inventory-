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


class BillDetailsPageTests(FinanceTestCase):
    """The products on a non-coil bill: description, quantity, unit, entered by finance after the bill is uploaded."""

    def setUp(self):
        finance_login(self.client)
        self.entry = NonCoilEntry.objects.create(category='DIES', company='ABC TOOLS', bill=bill_file())
        self.url = reverse('finance_bill_details', args=[self.entry.pk])

    def _post(self, rows):
        data = {'line-TOTAL_FORMS': str(len(rows))}
        for index, (description, quantity, unit) in enumerate(rows):
            data.update({f'line-{index}-description': description, f'line-{index}-quantity': quantity, f'line-{index}-unit': unit})
        return self.client.post(self.url, data)

    def test_uploading_a_non_coil_bill_goes_on_to_the_details_page(self):
        entry = NonCoilEntry.objects.create(category='BEARINGS', company='XYZ')
        response = self.client.post(reverse('finance_upload_other_bill', args=[entry.pk]), {'bill': bill_file()})
        self.assertRedirects(response, reverse('finance_bill_details', args=[entry.pk]))

    def test_a_refused_bill_stays_on_the_list(self):
        entry = NonCoilEntry.objects.create(category='BEARINGS', company='XYZ')
        response = self.client.post(reverse('finance_upload_other_bill', args=[entry.pk]), {'bill': bill_file('x.html', b'<b>')})
        self.assertRedirects(response, reverse('finance'))

    def test_a_coil_bill_now_opens_its_details_page(self):
        coil = GateEntry.objects.create(vendor='TATA', invoice_no='I1', total_weight=100)
        response = self.client.post(reverse('finance_upload_coil_bill', args=[coil.pk]), {'bill': bill_file()})
        self.assertRedirects(response, reverse('finance_coil_bill_details', args=[coil.pk]))

    def test_the_page_shows_the_entry_and_the_bill(self):
        page = self.client.get(self.url)
        self.assertContains(page, 'DIES')
        self.assertContains(page, 'ABC TOOLS')
        self.assertContains(page, reverse('finance_other_bill', args=[self.entry.pk]))

    def test_saving_products_stores_them_in_order_with_the_unit_in_capitals(self):
        response = self._post([('Die 12 mm', '4', 'nos'), ('Grease', '2.5', 'kg')])
        self.assertRedirects(response, reverse('finance'))
        lines = list(self.entry.lines.all())
        self.assertEqual([(line.description, str(line.quantity), line.unit, line.position) for line in lines],
                         [('Die 12 mm', '4.000', 'NOS', 1), ('Grease', '2.500', 'KG', 2)])

    def test_saving_again_replaces_the_list(self):
        self._post([('Die 12 mm', '4', 'NOS'), ('Grease', '2', 'KG')])
        self._post([('Die 12 mm', '6', 'NOS')])
        self.assertEqual([(line.description, str(line.quantity)) for line in self.entry.lines.all()], [('Die 12 mm', '6.000')])

    def test_blank_rows_are_ignored_and_saving_none_is_allowed(self):
        self._post([('', '', ''), ('Die', '1', 'NOS')])
        self.assertEqual(self.entry.lines.count(), 1)
        self._post([('', '', '')])
        self.assertEqual(self.entry.lines.count(), 0)

    def test_a_half_filled_row_is_refused_and_nothing_is_saved(self):
        self._post([('Die', '1', 'NOS')])
        response = self._post([('Die', '1', 'NOS'), ('Grease', '', 'KG')])
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Please complete or remove the rows marked below.')
        self.assertContains(response, 'Grease')                        # what was typed is kept
        self.assertEqual([line.description for line in self.entry.lines.all()], ['Die'])   # the earlier list is untouched

    def test_a_quantity_must_be_above_zero(self):
        response = self._post([('Die', '0', 'NOS')])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.entry.lines.count(), 0)

    def test_the_page_lists_the_saved_products_to_edit(self):
        self._post([('Die 12 mm', '4', 'NOS')])
        page = self.client.get(self.url)
        self.assertContains(page, 'value="Die 12 mm"')
        self.assertContains(page, 'value="4"')

    def test_an_entry_without_a_bill_has_no_details_page(self):
        bare = NonCoilEntry.objects.create(category='TOOLS', company='NO BILL')
        response = self.client.get(reverse('finance_bill_details', args=[bare.pk]), follow=True)
        self.assertContains(response, 'Upload the bill first')

    def test_the_list_links_to_the_page_and_counts_the_products(self):
        self._post([('Die 12 mm', '4', 'NOS'), ('Grease', '2', 'KG')])
        page = self.client.get(reverse('finance'))
        self.assertContains(page, reverse('finance_bill_details', args=[self.entry.pk]))
        self.assertContains(page, '2 products')
        other = NonCoilEntry.objects.create(category='X', company='Y', bill=bill_file())
        self.assertContains(self.client.get(reverse('finance')), 'No products yet')
        other.delete()

    def test_a_coil_row_links_to_its_own_details_page(self):
        self.entry.delete()   # leave only a coil entry on the page
        coil = GateEntry.objects.create(vendor='TATA', invoice_no='I1', total_weight=100, bill=bill_file())
        page = self.client.get(reverse('finance'))
        self.assertContains(page, reverse('finance_coil_bill_details', args=[coil.pk]))
        self.assertNotContains(page, reverse('finance_bill_details', args=[coil.pk]))

    def test_the_page_confirms_a_fresh_upload(self):
        entry = NonCoilEntry.objects.create(category='BEARINGS', company='XYZ')
        page = self.client.post(reverse('finance_upload_other_bill', args=[entry.pk]), {'bill': bill_file()}, follow=True)
        self.assertContains(page, 'Bill uploaded.')

    def test_the_employee_pin_does_not_open_it(self):
        self.client.post(reverse('finance_logout'))
        employee_login(self.client)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse('login'), response['Location'])


class CoilBillDetailsTests(FinanceTestCase):
    def setUp(self):
        from decimal import Decimal
        finance_login(self.client)
        self.entry = GateEntry.objects.create(vendor='TATA', invoice_no='I1', total_weight=1000)
        self.entry.bill = bill_file()
        self.entry.save()
        from ..models import GateEntryLot
        self.a = GateEntryLot.objects.create(gate_entry=self.entry, company='TATA', grade='EN8D', size=Decimal('1.2'), no_of_coils=2, weight=Decimal('600'))
        self.b = GateEntryLot.objects.create(gate_entry=self.entry, company='JSW', grade='EN9', size=Decimal('2'), no_of_coils=3)
        self.url = reverse('finance_coil_bill_details', args=[self.entry.pk])

    def post(self, **data):
        base = {f'rate-{self.a.pk}': '', f'rate-{self.b.pk}': '', 'charge-TOTAL_FORMS': '0'}
        base.update(data)
        return self.client.post(self.url, base)

    def test_page_fills_in_the_lots_and_links_the_bill(self):
        page = self.client.get(self.url)
        for text in ('EN8D', 'EN9', 'TATA', 'JSW', 'View the bill'):
            self.assertContains(page, text)
        self.assertContains(self.client.get(reverse('finance')), self.url)

    def test_saving_rates_and_other_amounts(self):
        from decimal import Decimal
        response = self.post(**{f'rate-{self.a.pk}': '52.50', 'charge-TOTAL_FORMS': '2',
                                'charge-0-description': 'Freight', 'charge-0-amount': '1200',
                                'charge-1-description': '', 'charge-1-amount': ''})
        self.assertRedirects(response, reverse('finance'))
        self.a.refresh_from_db()
        self.assertEqual(self.a.rate, Decimal('52.50'))
        self.assertEqual(self.a.amount(), Decimal('31500.00'))
        self.assertIsNone(self.b.amount())
        charge = self.entry.charges.get()
        self.assertEqual((charge.description, charge.amount), ('Freight', Decimal('1200.00')))

    def test_saving_again_replaces_the_amounts_and_a_blank_rate_clears_it(self):
        self.post(**{f'rate-{self.a.pk}': '50', 'charge-TOTAL_FORMS': '1', 'charge-0-description': 'Freight', 'charge-0-amount': '10'})
        self.post(**{'charge-TOTAL_FORMS': '1', 'charge-0-description': 'Loading', 'charge-0-amount': '5'})
        self.a.refresh_from_db()
        self.assertIsNone(self.a.rate)
        self.assertEqual(list(self.entry.charges.values_list('description', flat=True)), ['Loading'])

    def test_a_bad_rate_or_half_filled_amount_saves_nothing(self):
        response = self.post(**{f'rate-{self.a.pk}': '-3'})
        self.assertContains(response, 'Enter a rate above 0')
        response = self.post(**{'charge-TOTAL_FORMS': '1', 'charge-0-description': 'Freight', 'charge-0-amount': ''})
        self.assertContains(response, 'Please fix the rows marked below')
        self.assertFalse(self.entry.charges.exists())

    def test_needs_the_bill_first_and_finance_or_staff(self):
        bare = GateEntry.objects.create(vendor='X', invoice_no='I2', total_weight=10)
        response = self.client.get(reverse('finance_coil_bill_details', args=[bare.pk]))
        self.assertRedirects(response, reverse('finance'))
        self.client.logout()
        self.assertEqual(self.client.get(self.url).status_code, 302)


class BillsAreStoredAsPdfTests(FinanceTestCase):
    def setUp(self):
        finance_login(self.client)
        self.entry = GateEntry.objects.create(vendor='TATA', invoice_no='I1', total_weight=100)

    def image(self, name, fmt):
        import io
        from PIL import Image
        buffer = io.BytesIO()
        Image.new('RGB', (40, 60), (200, 30, 30)).save(buffer, format=fmt)
        return SimpleUploadedFile(name, buffer.getvalue())

    def test_a_photo_is_stored_as_a_pdf(self):
        for name, fmt in (('scan.jpg', 'JPEG'), ('scan.png', 'PNG'), ('scan.webp', 'WEBP')):
            with self.subTest(name=name):
                self.client.post(reverse('finance_upload_coil_bill', args=[self.entry.pk]), {'bill': self.image(name, fmt)})
                self.entry.refresh_from_db()
                self.assertTrue(self.entry.bill.name.endswith('.pdf'))
                self.entry.bill.open('rb')
                self.assertEqual(self.entry.bill.read(5), b'%PDF-')
                self.entry.bill.close()

    def test_a_pdf_is_kept_as_it_is(self):
        self.client.post(reverse('finance_upload_coil_bill', args=[self.entry.pk]), {'bill': bill_file('mine.pdf')})
        self.entry.refresh_from_db()
        self.entry.bill.open('rb')
        self.assertEqual(self.entry.bill.read(), b'%PDF-1.4 fake bill')
        self.entry.bill.close()

    def test_a_fake_image_is_refused(self):
        response = self.client.post(reverse('finance_upload_coil_bill', args=[self.entry.pk]),
                                    {'bill': SimpleUploadedFile('x.jpg', b'not an image')}, follow=True)
        self.assertContains(response, "isn&#x27;t a readable image")
        self.entry.refresh_from_db()
        self.assertFalse(self.entry.has_bill())
