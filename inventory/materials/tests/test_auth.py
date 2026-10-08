"""Employee PIN login/throttle, access control on every route, and the public/employee landing pages."""

from django.conf import settings
from django.contrib.auth.models import User
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from ..models import (
    Customer,
    GateEntry,
    GateEntryLot,
    Material,
    Order,
    OrderCoilPick,
    ProcessStep,
    ProductionJob,
    ProductType,
)


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
        product_type = ProductType.objects.create(item_code='Bar', grade='EN8D')
        ProcessStep.objects.create(product_type=product_type, name='Cutting', order=1)

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


class EmployeeLoginThrottleTests(TestCase):
    def setUp(self):
        from django.core.cache import cache
        cache.clear()
        self.addCleanup(cache.clear)
        self.url = reverse('employee_login')

    def _fail(self, times, **extra):
        for _ in range(times):
            self.client.post(self.url, {'pin': 'wrong'}, **extra)

    def test_locks_out_after_max_failures_even_for_the_correct_pin(self):
        self._fail(settings.EMPLOYEE_LOGIN_MAX_FAILURES)
        response = self.client.post(self.url, {'pin': settings.EMPLOYEE_PIN})
        self.assertContains(response, "Too many incorrect attempts")
        self.assertFalse(self.client.session.get('employee_auth'))

    def test_correct_pin_still_works_below_the_limit(self):
        self._fail(settings.EMPLOYEE_LOGIN_MAX_FAILURES - 1)
        self.client.post(self.url, {'pin': settings.EMPLOYEE_PIN})
        self.assertTrue(self.client.session.get('employee_auth'))

    def test_successful_login_resets_the_failure_count(self):
        self._fail(settings.EMPLOYEE_LOGIN_MAX_FAILURES - 1)
        self.client.post(self.url, {'pin': settings.EMPLOYEE_PIN})
        self.client.post(reverse('employee_logout'))
        self._fail(settings.EMPLOYEE_LOGIN_MAX_FAILURES - 1)
        self.client.post(self.url, {'pin': settings.EMPLOYEE_PIN})
        self.assertTrue(self.client.session.get('employee_auth'))

    def test_lockout_is_per_ip(self):
        self._fail(settings.EMPLOYEE_LOGIN_MAX_FAILURES, REMOTE_ADDR='10.0.0.1')
        self.client.post(self.url, {'pin': settings.EMPLOYEE_PIN}, REMOTE_ADDR='10.0.0.2')
        self.assertTrue(self.client.session.get('employee_auth'))

    def test_spoofed_forwarded_for_header_does_not_dodge_the_lockout(self):
        for i in range(settings.EMPLOYEE_LOGIN_MAX_FAILURES):
            self.client.post(self.url, {'pin': 'wrong'}, HTTP_X_FORWARDED_FOR=f'9.9.9.{i}')
        response = self.client.post(self.url, {'pin': settings.EMPLOYEE_PIN}, HTTP_X_FORWARDED_FOR='1.2.3.4')
        self.assertContains(response, "Too many incorrect attempts")


class EmployeePinSettingTests(SimpleTestCase):
    def _import_settings(self, **env):
        import os
        import subprocess
        import sys
        base_env = {k: v for k, v in os.environ.items()
                    if k not in ('EMPLOYEE_PIN', 'DJANGO_DEBUG', 'DJANGO_SECRET_KEY')}
        # Explicit empty values, not absent ones: load_dotenv fills in absent
        # vars from a developer's real .env, but never overrides a set one.
        base_env.update({'EMPLOYEE_PIN': '', 'DJANGO_SECRET_KEY': ''})
        base_env.update(env)
        return subprocess.run(
            [sys.executable, '-c', 'import inventory.settings as s; print(s.EMPLOYEE_PIN)'],
            cwd=settings.BASE_DIR, env=base_env, capture_output=True, text=True,
        )

    def test_production_refuses_to_start_without_an_employee_pin(self):
        result = self._import_settings(DJANGO_DEBUG='False', DJANGO_SECRET_KEY='x' * 50)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("EMPLOYEE_PIN must be set", result.stderr)

    def test_production_starts_with_an_employee_pin(self):
        result = self._import_settings(DJANGO_DEBUG='False', DJANGO_SECRET_KEY='x' * 50, EMPLOYEE_PIN='4821')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), '4821')

    def test_debug_falls_back_to_the_dev_pin(self):
        result = self._import_settings(DJANGO_DEBUG='True')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), '1234')


class MaterialsRouteGuardTests(TestCase):
    """Walks materials/urls.py so a new view can't ship without access control:
    every route must carry @staff_required / @employee_required, or be listed
    in PUBLIC below with the reason it's open."""

    PUBLIC = {
        'home',               # landing page, no data
        'welcome',            # the tablet app's first screen, static text
        'admin_login',        # login form itself
        'employee_login',     # PIN form itself
        'employee_logout',    # only clears this browser's own session
        'quote_form',         # customer-facing; the unguessable UUID token is the credential
        'whatsapp_webhook',   # Meta calls it directly; HMAC signature is checked in the view
    }

    @staticmethod
    def _patterns():
        import materials.urls
        return materials.urls.urlpatterns

    @staticmethod
    def _url_for(pattern):
        import re
        import uuid
        route = re.sub(r'<int:\w+>', '1', str(pattern.pattern))
        route = re.sub(r'<uuid:\w+>', str(uuid.uuid4()), route)
        route = re.sub(r'<path:\w+>', 'purchase_orders/x.pdf', route)
        return '/' + route

    def _guarded(self):
        return [(p, p.callback) for p in self._patterns() if p.callback.__name__ not in self.PUBLIC]

    def test_every_route_is_guarded_or_explicitly_public(self):
        unguarded = [cb.__name__ for _, cb in self._guarded() if not hasattr(cb, 'access')]
        self.assertEqual(unguarded, [], "Add @staff_required/@employee_required, or list the view in PUBLIC with a reason.")

    def test_public_allowlist_has_no_stale_entries(self):
        routed = {p.callback.__name__ for p in self._patterns()}
        self.assertEqual(self.PUBLIC - routed, set())

    def _assert_denied(self, response, view_name):
        if view_name == 'customer_autocomplete':
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), [])
        elif view_name == 'product_code_lookup':   # a JSON endpoint called by script: 403, not a redirect
            self.assertEqual(response.status_code, 403)
            self.assertEqual(response.json(), {'error': 'forbidden'})
        else:
            self.assertEqual(response.status_code, 302, f"{view_name} served an unauthorised request")

    def test_anonymous_requests_are_denied_on_every_guarded_route(self):
        for pattern, cb in self._guarded():
            url = self._url_for(pattern)
            for method in (self.client.get, self.client.post):
                with self.subTest(view=cb.__name__, method=method.__name__):
                    self._assert_denied(method(url), cb.__name__)

    def test_employee_pin_session_does_not_unlock_staff_routes(self):
        session = self.client.session
        session['employee_auth'] = True
        session.save()
        for pattern, cb in self._guarded():
            if getattr(cb, 'access', None) != 'staff':
                continue
            for method in (self.client.get, self.client.post):
                with self.subTest(view=cb.__name__, method=method.__name__):
                    self._assert_denied(method(self._url_for(pattern)), cb.__name__)

    def test_non_staff_login_does_not_unlock_staff_routes(self):
        from django.contrib.auth import get_user_model
        get_user_model().objects.create_user('plain', password='pw')
        self.client.login(username='plain', password='pw')
        for pattern, cb in self._guarded():
            if getattr(cb, 'access', None) != 'staff':
                continue
            with self.subTest(view=cb.__name__):
                self._assert_denied(self.client.get(self._url_for(pattern)), cb.__name__)

    def test_rest_api_rejects_anonymous_requests(self):
        for name in ('coils', 'orders', 'jobs', 'product-types'):
            with self.subTest(endpoint=name):
                self.assertIn(self.client.get(f'/api/{name}/').status_code, (401, 403))


class WelcomePageTests(TestCase):
    def test_welcome_is_public_and_leads_to_the_pin_page(self):
        response = self.client.get(reverse('welcome'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Welcome to')
        self.assertContains(response, 'Matta Drawing Works')
        self.assertContains(response, reverse('employee_login'))

    def test_opening_the_welcome_page_asks_for_the_pin_again(self):
        session = self.client.session
        session['employee_auth'] = True
        session.save()
        self.assertEqual(self.client.get(reverse('employee')).status_code, 200)   # signed in
        self.client.get(reverse('welcome'))
        response = self.client.get(reverse('employee'))
        self.assertEqual(response.status_code, 302)
        self.assertIn('/employee-login/', response['Location'])
