"""The Dispatch tiles and their (still empty) pages for the owners, the plant and finance."""

from django.conf import settings
from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse


class DispatchTilesTests(TestCase):
    def test_the_owners_dashboard_has_a_dispatch_planning_tile_and_page(self):
        self.client.force_login(User.objects.create_user('boss', password='pw', is_staff=True))
        home = self.client.get(reverse('staff_home'))
        self.assertContains(home, 'Dispatch planning')
        self.assertContains(home, reverse('dispatch_planning'))
        page = self.client.get(reverse('dispatch_planning'))
        self.assertContains(page, 'Dispatch planning')
        self.assertContains(page, reverse('staff_home'))   # the back link

    def test_the_employee_portal_has_a_dispatch_tile_and_page(self):
        self.client.post(reverse('login'), {'pin': settings.EMPLOYEE_PIN})
        self.assertContains(self.client.get(reverse('employee')), reverse('employee_dispatch'))
        page = self.client.get(reverse('employee_dispatch'))
        self.assertContains(page, 'Dispatch')
        self.assertContains(page, reverse('employee'))

    def test_the_finance_page_has_a_dispatch_link_and_page(self):
        self.client.post(reverse('login'), {'pin': settings.FINANCE_CODE})
        self.assertContains(self.client.get(reverse('finance')), reverse('finance_dispatch'))
        page = self.client.get(reverse('finance_dispatch'))
        self.assertContains(page, 'Dispatch')
        self.assertContains(page, reverse('finance'))

    def test_each_page_opens_only_for_its_own_role(self):
        self.client.post(reverse('login'), {'pin': settings.EMPLOYEE_PIN})
        for name in ('dispatch_planning', 'finance_dispatch'):
            self.assertEqual(self.client.get(reverse(name)).status_code, 302, name)
        self.client.logout()
        self.client.post(reverse('login'), {'pin': settings.FINANCE_CODE})
        for name in ('dispatch_planning', 'employee_dispatch'):
            self.assertEqual(self.client.get(reverse(name)).status_code, 302, name)
