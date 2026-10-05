"""Template conventions: everything extends base.html, and public pages stay self-contained."""
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from django.conf import settings
from django.template.loader import render_to_string
from django.test import TestCase
from django.urls import reverse

from ..models import Customer


class TemplateStructureTests(TestCase):
    def test_every_page_template_extends_base_html(self):
        templates_dir = Path(settings.BASE_DIR) / 'templates'
        not_pages = {'base.html', '_base.css'}
        # files starting with '_' are partials ({% include %}d into pages), not pages
        pages = [p for p in templates_dir.rglob('*')
                 if p.is_file() and p.name not in not_pages and not p.name.startswith('_')]
        self.assertGreater(len(pages), 20)  # the scan itself must not silently find nothing
        missing = [str(p.relative_to(templates_dir)) for p in pages
                   if not p.read_text().lstrip().startswith('{% extends "materials/base.html" %}')]
        self.assertEqual(missing, [], "New pages should extend materials/base.html (see CLAUDE.md, 'Templates').")

    def test_customer_facing_pages_load_nothing_external(self):
        """quote_form and quote_submitted are reached through the Tailscale Funnel, which
        exposes only /quote/* - a /static/ stylesheet or script would 404 for customers and
        leave the page unstyled."""
        customer = Customer.objects.create(name='Public Co')
        pages = {
            'quote form': self.client.get(f'/quote/{customer.quote_token}/').content.decode(),
            'quote submitted': render_to_string('materials/quote_submitted.html', {'customer': customer}),
        }
        for name, html in pages.items():
            with self.subTest(page=name):
                self.assertIn('<style>', html)  # styled inline
                self.assertNotIn('/static/', html)
                self.assertIsNone(re.search(r'<link[^>]+stylesheet', html))
                self.assertIsNone(re.search(r'<script[^>]+src=', html))


@unittest.skipUnless(shutil.which('node'), 'node is needed to syntax-check the inline scripts')
class InlineScriptSyntaxTests(TestCase):
    """None of the Python tests execute JavaScript, so a typo in a page's inline
    <script> (a dropped parenthesis once silently disabled the quote form's code
    matching) would otherwise ship unnoticed. This only parses; it doesn't run it."""

    def test_the_inline_scripts_of_the_main_staff_pages_parse(self):
        from django.contrib.auth.models import User
        from ..models import Query
        staff = User.objects.create_user('syntax_staff', password='pw', is_staff=True)
        self.client.force_login(staff)
        query = Query.objects.create(source='indiamart', contact_phone='9123456780', company_name='Syntax Co')
        pages = [reverse('quotation_form'), reverse('query_dashboard'), reverse('order_dashboard'),
                 reverse('query_edit', kwargs={'pk': query.pk}), reverse('query_detail', kwargs={'pk': query.pk})]
        checked = 0
        for url in pages:
            html = self.client.get(url).content.decode()
            for index, script in enumerate(re.findall(r'<script>(.*?)</script>', html, re.S)):
                with self.subTest(page=url, script=index):
                    with tempfile.NamedTemporaryFile('w', suffix='.js', delete=False) as handle:
                        handle.write(script)
                    result = subprocess.run(['node', '--check', handle.name], capture_output=True, text=True)
                    os.unlink(handle.name)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    checked += 1
        self.assertGreater(checked, 3)   # the scan itself must not silently find nothing
