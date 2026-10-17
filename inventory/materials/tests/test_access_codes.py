"""Owners change the employee PIN and the finance code from the dashboard; a change signs everybody out."""

from io import StringIO

from django.conf import settings
from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import Client, TestCase
from django.urls import reverse

from ..models import AccessCode


class AccessCodeTestCase(TestCase):
    def setUp(self):
        cache.clear()
        self.owner = User.objects.create_user('owner', password='pw', is_staff=True)
        self.owner_client = Client()
        self.owner_client.force_login(self.owner)

    def sign_in(self, code):
        client = Client()
        client.post(reverse('login'), {'pin': code})
        return client

    def change(self, role, new, repeat=None):
        return self.owner_client.post(reverse('access_codes'), {
            'role': role, 'new_code': new, 'repeat': new if repeat is None else repeat})

    def is_in(self, client, name):
        return client.get(reverse(name)).status_code == 200


class UntilChangedTests(AccessCodeTestCase):
    def test_the_env_codes_work_until_an_owner_changes_them(self):
        self.assertFalse(AccessCode.objects.exists())
        self.assertTrue(self.is_in(self.sign_in(settings.EMPLOYEE_PIN), 'employee'))
        self.assertTrue(self.is_in(self.sign_in(settings.FINANCE_CODE), 'finance'))

    def test_a_sign_in_from_before_the_feature_stays_valid_until_the_first_change(self):
        client = Client()
        session = client.session
        session['employee_auth'] = True          # what the login stored before codes had versions
        session.save()
        self.assertTrue(self.is_in(client, 'employee'))
        self.change('employee', '4321')
        self.assertFalse(self.is_in(client, 'employee'))


class ChangingACodeTests(AccessCodeTestCase):
    def test_the_new_code_works_and_the_old_one_does_not(self):
        self.change('employee', '4321')
        self.assertTrue(self.is_in(self.sign_in('4321'), 'employee'))
        self.assertFalse(self.is_in(self.sign_in(settings.EMPLOYEE_PIN), 'employee'))

    def test_the_finance_code_changes_the_same_way(self):
        self.change('finance', '8642')
        self.assertTrue(self.is_in(self.sign_in('8642'), 'finance'))
        self.assertFalse(self.is_in(self.sign_in(settings.FINANCE_CODE), 'finance'))

    def test_changing_a_code_signs_out_everyone_signed_in_with_it_immediately(self):
        tablet_one, tablet_two = self.sign_in(settings.EMPLOYEE_PIN), self.sign_in(settings.EMPLOYEE_PIN)
        self.assertTrue(self.is_in(tablet_one, 'employee'))
        self.change('employee', '4321')
        for tablet in (tablet_one, tablet_two):
            response = tablet.get(reverse('employee'))
            self.assertEqual(response.status_code, 302)
            self.assertIn(reverse('login'), response['Location'])

    def test_someone_signed_in_with_the_new_code_stays_in_until_it_changes_again(self):
        self.change('employee', '4321')
        tablet = self.sign_in('4321')
        self.assertTrue(self.is_in(tablet, 'employee'))
        self.change('employee', '9753')
        self.assertFalse(self.is_in(tablet, 'employee'))

    def test_changing_one_role_does_not_sign_out_the_other(self):
        finance, employee = self.sign_in(settings.FINANCE_CODE), self.sign_in(settings.EMPLOYEE_PIN)
        self.change('employee', '4321')
        self.assertTrue(self.is_in(finance, 'finance'))
        self.assertFalse(self.is_in(employee, 'employee'))

    def test_the_owners_own_sign_in_is_untouched(self):
        self.change('employee', '4321')
        self.assertEqual(self.owner_client.get(reverse('staff_home')).status_code, 200)

    def test_the_code_is_stored_scrambled_never_in_clear(self):
        self.change('employee', '4321')
        row = AccessCode.objects.get(role='employee')
        self.assertNotIn('4321', row.code_hash)
        self.assertTrue(row.code_hash.startswith('pbkdf2'))

    def test_who_and_when_are_recorded_and_the_code_is_never_shown(self):
        self.change('employee', '4321')
        row = AccessCode.objects.get(role='employee')
        self.assertEqual(row.changed_by, self.owner)
        page = self.owner_client.get(reverse('access_codes'))
        self.assertContains(page, 'by owner')
        self.assertNotContains(page, '4321')
        self.assertNotContains(page, settings.EMPLOYEE_PIN)

    def test_a_change_confirms_that_everyone_was_signed_out(self):
        response = self.owner_client.post(reverse('access_codes'), {'role': 'employee', 'new_code': '4321', 'repeat': '4321'}, follow=True)
        self.assertContains(response, 'Employee PIN changed. Everyone signed in with the old one has been signed out.')

    def test_setting_the_same_code_again_changes_nothing_and_signs_nobody_out(self):
        self.change('employee', '4321')
        tablet = self.sign_in('4321')
        version = AccessCode.objects.get(role='employee').version
        response = self.owner_client.post(reverse('access_codes'), {'role': 'employee', 'new_code': '4321', 'repeat': '4321'}, follow=True)
        self.assertContains(response, 'nothing was changed')
        self.assertEqual(AccessCode.objects.get(role='employee').version, version)
        self.assertTrue(self.is_in(tablet, 'employee'))

    def test_the_lockout_still_applies_to_saved_codes(self):
        self.change('employee', '4321')
        for _ in range(settings.EMPLOYEE_LOGIN_MAX_FAILURES):
            Client().post(reverse('login'), {'pin': '0000'})
        response = Client().post(reverse('login'), {'pin': '4321'})
        self.assertContains(response, 'Too many incorrect attempts')


class WhatACodeMustBeTests(AccessCodeTestCase):
    def assertRefused(self, role, new, repeat=None):
        response = self.change(role, new, repeat)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(AccessCode.objects.filter(role=role).exists())
        return response

    def test_exactly_four_digits(self):
        for bad in ('123', '12345', '12a4', 'abcd', '12 4'):
            with self.subTest(code=bad):
                self.assertRefused('employee', bad)

    def test_it_must_be_typed_twice_the_same(self):
        response = self.assertRefused('employee', '4321', '4322')
        self.assertContains(response, "do not match")

    def test_it_must_differ_from_the_other_roles_code(self):
        response = self.assertRefused('employee', settings.FINANCE_CODE)
        self.assertContains(response, 'already the Finance code')

    def test_it_must_differ_from_the_other_roles_saved_code_too(self):
        self.change('finance', '8642')
        response = self.assertRefused('employee', '8642')
        self.assertContains(response, 'already the Finance code')

    def test_a_refused_change_signs_nobody_out(self):
        tablet = self.sign_in(settings.EMPLOYEE_PIN)
        self.assertRefused('employee', '12')
        self.assertTrue(self.is_in(tablet, 'employee'))


class WhoMayChangeThemTests(AccessCodeTestCase):
    def test_only_owners_may_open_the_page_or_change_a_code(self):
        for client in (Client(), self.sign_in(settings.EMPLOYEE_PIN), self.sign_in(settings.FINANCE_CODE)):
            self.assertEqual(client.get(reverse('access_codes')).status_code, 302)
            client.post(reverse('access_codes'), {'role': 'employee', 'new_code': '4321', 'repeat': '4321'})
        self.assertFalse(AccessCode.objects.exists())

    def test_a_non_staff_login_cannot(self):
        User.objects.create_user('plain', password='pw')
        client = Client()
        client.login(username='plain', password='pw')
        self.assertEqual(client.get(reverse('access_codes')).status_code, 302)

    def test_the_owners_dashboard_has_the_card(self):
        self.assertContains(self.owner_client.get(reverse('staff_home')), reverse('access_codes'))


class ResetCommandTests(AccessCodeTestCase):
    def run_command(self, *args):
        out = StringIO()
        call_command('reset_access_code', *args, stdout=out)
        return out.getvalue()

    def test_it_sets_a_code_and_signs_everyone_out(self):
        tablet = self.sign_in(settings.EMPLOYEE_PIN)
        self.assertIn('changed', self.run_command('employee', '4321'))
        self.assertTrue(self.is_in(self.sign_in('4321'), 'employee'))
        self.assertFalse(self.is_in(tablet, 'employee'))

    def test_it_applies_the_same_rules(self):
        for args in (('employee', '12'), ('employee', 'abcd'), ('employee', settings.FINANCE_CODE)):
            with self.subTest(args=args), self.assertRaises(CommandError):
                self.run_command(*args)
        self.assertFalse(AccessCode.objects.exists())
