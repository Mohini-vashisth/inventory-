from django.contrib.auth import get_user_model
from django.db import OperationalError, connection, transaction
from django.test import TestCase, override_settings

from ..models import Customer

STANDBY = override_settings(
    SESSION_ENGINE='django.contrib.sessions.backends.signed_cookies',
    MIDDLEWARE=[
        'django.middleware.security.SecurityMiddleware',
        'django.contrib.sessions.middleware.SessionMiddleware',
        'django.middleware.common.CommonMiddleware',
        'django.middleware.csrf.CsrfViewMiddleware',
        'django.contrib.auth.middleware.AuthenticationMiddleware',
        'django.contrib.messages.middleware.MessageMiddleware',
        'materials.middleware.StandbyReadOnlyMiddleware',
    ],
)


@STANDBY
class StandbyReadOnlyTests(TestCase):
    def test_pages_open_with_the_copy_banner(self):
        response = self.client.get('/welcome/')
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'Read-only copy as of', response.content)

    def test_changes_are_refused(self):
        for path in ('/orders/1/confirm/', '/quote/00000000-0000-0000-0000-000000000000/', '/webhooks/whatsapp/'):
            self.assertEqual(self.client.post(path).status_code, 403, path)

    def test_entry_pages_do_not_open(self):
        for path in ('/gate-entry/', '/gate-entry/new/', '/non-coil-entry/', '/gate-entry/lot/1/coil/', '/queries/1/edit/',
                     '/quotations/new/', '/orders/1/edit/', '/select-order/', '/scan-job/', '/admin/materials/order/add/'):
            self.assertEqual(self.client.get(path).status_code, 403, path)
        self.assertEqual(self.client.get('/orders/1/?edit=1').status_code, 403)

    def test_data_pages_get_the_script_that_disables_change_buttons(self):
        response = self.client.get('/welcome/')
        self.assertContains(response, "button,input[type=submit]")
        self.assertContains(response, "gate-entry/lot/")
        body = response.content.split(b'</head>')[1]
        self.assertTrue(body.lstrip().startswith(b'<body'))
        self.assertIn(b'Read-only copy as of', body.split(b'<script>')[0])   # the banner is inside the body, not the stylesheet

    def test_signing_in_is_still_allowed(self):
        get_user_model().objects.create_user('boss', password='pw12345!', is_staff=True)
        response = self.client.post('/admin-login/', {'username': 'boss', 'password': 'pw12345!'})
        self.assertNotEqual(response.status_code, 403)


class QueryOnlyTests(TestCase):
    def test_database_refuses_writes_when_query_only(self):
        with connection.cursor() as cursor:
            cursor.execute('PRAGMA query_only=ON;')
        try:
            with self.assertRaises(OperationalError), transaction.atomic():
                Customer.objects.create(name='X')
        finally:
            with connection.cursor() as cursor:
                cursor.execute('PRAGMA query_only=OFF;')
