"""Staff-only media serving and a smoke test of every admin page."""

import tempfile

from django.test import TestCase, override_settings
from django.urls import reverse
from pathlib import Path

from ..models import (
    QueryItem,
    AllowedCoilSpec,
    ProductCategory,
    Customer,
    GateEntry,
    GateEntryLot,
    GradeOption,
    Material,
    Order,
    OrderCoilPick,
    ProcessStep,
    ProductionJob,
    ProductType,
    Query,
    Quotation,
    QuotationLineItem,
    SizeOption,
    StepLog,
)


class ServeMediaTests(TestCase):
    def setUp(self):
        from django.contrib.auth import get_user_model
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        override = override_settings(MEDIA_ROOT=Path(self.tmp.name))
        override.enable()
        self.addCleanup(override.disable)
        folder = Path(self.tmp.name) / 'purchase_orders'
        folder.mkdir()
        (folder / 'po.pdf').write_bytes(b'%PDF-1.4 fake')
        (folder / 'evil.html').write_text('<script>alert(1)</script>')
        self.staff = get_user_model().objects.create_user('boss', password='pw', is_staff=True)

    def _read(self, response):
        return b''.join(response.streaming_content)

    def test_staff_can_open_an_uploaded_pdf_inline(self):
        self.client.force_login(self.staff)
        response = self.client.get('/media/purchase_orders/po.pdf')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self._read(response), b'%PDF-1.4 fake')
        self.assertNotIn('attachment', response.get('Content-Disposition', ''))

    def test_non_displayable_types_are_forced_to_download(self):
        self.client.force_login(self.staff)
        response = self.client.get('/media/purchase_orders/evil.html')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Disposition'], 'attachment')

    def test_anonymous_request_is_redirected_to_login_not_served(self):
        response = self.client.get('/media/purchase_orders/po.pdf')
        self.assertEqual(response.status_code, 302)
        self.assertIn('/admin-login/', response['Location'])

    def test_employee_pin_session_cannot_read_uploads(self):
        session = self.client.session
        session['employee_auth'] = True
        session.save()
        self.assertEqual(self.client.get('/media/purchase_orders/po.pdf').status_code, 302)

    def test_path_traversal_is_rejected(self):
        self.client.force_login(self.staff)
        for bad in ('/media/../db.sqlite3', '/media/purchase_orders/../../manage.py', '/media/%2e%2e/manage.py'):
            with self.subTest(url=bad):
                self.assertIn(self.client.get(bad).status_code, (400, 404))

    def test_missing_file_is_404(self):
        self.client.force_login(self.staff)
        self.assertEqual(self.client.get('/media/purchase_orders/nope.pdf').status_code, 404)

    def test_model_file_urls_point_at_the_served_route(self):
        from django.core.files.base import ContentFile
        query = Query.objects.create(source='whatsapp', contact_phone='9876543210')
        item = QueryItem.objects.create(query=query)
        item.drawing.save('d.pdf', ContentFile(b'%PDF-1.4'), save=True)
        self.assertTrue(item.drawing.url.startswith('/media/query_drawings/'))
        self.client.force_login(self.staff)
        self.assertEqual(self.client.get(item.drawing.url).status_code, 200)


class AdminSmokeTests(TestCase):
    """Every registered admin page must render. Added for the Django 5.2
    upgrade: django-jazzmin only lists support through Django 5.0, and nothing
    else in the suite touches the admin. Sample records are loaded so the
    custom list_display columns / readonly methods actually execute."""

    @classmethod
    def setUpTestData(cls):
        from django.contrib.auth import get_user_model
        from django.core.files.base import ContentFile
        cls.admin_user = get_user_model().objects.create_superuser('root', 'r@example.com', 'pw')
        cls._media = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls._media.cleanup)
        with override_settings(MEDIA_ROOT=Path(cls._media.name)):
            customer = Customer.objects.create(name='Admin Smoke Co', email='a@example.com')
            product = ProductType.objects.create(item_code='Smoke Bar', grade='EN8D')
            ProcessStep.objects.create(product_type=product, name='Cutting', order=1)
            AllowedCoilSpec.objects.create(product_type=product, size='1.200')
            GradeOption.objects.create(name='EN8D')
            SizeOption.objects.create(value='1.200')
            gate = GateEntry.objects.create(vendor='V', vehicle_no='HR26AB1234', total_weight=1000)
            lot = GateEntryLot.objects.create(gate_entry=gate, company='Tata', grade='EN8D', size='1.200', no_of_coils=2)
            coil = Material.objects.create(quantity=500, grade='EN8D', size='1.2', lot=lot)
            order = Order.objects.create(customer=customer, product_type=product, quantity=100, status='in_production')
            pick = OrderCoilPick.objects.create(order=order, coil=coil, weight_allocated=50)
            job = ProductionJob.objects.create(pick=pick, product_type=product, job_no='SMOKE-1', order=order)
            StepLog.objects.create(job=job, step=product.steps.first(), status='completed')
            query = Query.objects.create(source='whatsapp', contact_phone='919876543210', company_name='Smoke Co')
            QueryItem.objects.create(query=query).drawing.save('d.pdf', ContentFile(b'%PDF-1.4'), save=True)
            quotation = Quotation.objects.create(customer=customer, source_query=query, status='sent')
            QuotationLineItem.objects.create(quotation=quotation, order=1, description='Bar', quantity=10, rate_per_kg=90)
            Quotation.objects.create(customer=customer, status='draft')

    def setUp(self):
        self.client.force_login(self.admin_user)

    def test_admin_index_and_every_registered_model_page_renders(self):
        from django.contrib import admin
        from django.test import RequestFactory
        self.assertEqual(self.client.get('/admin/').status_code, 200)
        request = RequestFactory().get('/admin/')
        request.user = self.admin_user
        checked = 0
        for model, model_admin in admin.site._registry.items():
            info = (model._meta.app_label, model._meta.model_name)
            urls = [reverse('admin:%s_%s_changelist' % info)]
            if model_admin.has_add_permission(request):  # some admins deliberately block adds
                urls.append(reverse('admin:%s_%s_add' % info))
            urls += [reverse('admin:%s_%s_change' % info, args=[obj.pk]) for obj in model.objects.all()[:3]]
            for url in urls:
                with self.subTest(url=url):
                    self.assertEqual(self.client.get(url).status_code, 200)
                    checked += 1
        self.assertGreater(checked, 40)  # the sweep itself must not silently shrink to nothing


class OrderAdminShowsTypeAndCodeTests(TestCase):
    def test_list_and_change_pages_show_the_product_type_and_code(self):
        from django.contrib.auth import get_user_model
        get_user_model().objects.create_superuser('root', 'r@example.com', 'pw')
        self.client.login(username='root', password='pw')
        category = ProductCategory.objects.get(name='Flat Bright Bar')
        code = ProductType.objects.create(item_code='ZZ-FBB9', category=category, grade='EN8D')
        order = Order.objects.create(customer=Customer.objects.create(name='Admin Co'), product_type=code, grade='EN8D', quantity=10)
        listing = self.client.get(reverse('admin:materials_order_changelist')).content.decode()
        self.assertIn('Product type', listing)
        self.assertIn('Product code', listing)
        self.assertIn('Flat Bright Bar', listing)
        self.assertIn('ZZ-FBB9', listing)
        change = self.client.get(reverse('admin:materials_order_change', args=[order.pk])).content.decode()
        self.assertIn('Flat Bright Bar', change)
        self.assertIn('Product code', change)
