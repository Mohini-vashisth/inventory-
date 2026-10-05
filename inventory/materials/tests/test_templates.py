"""Template conventions: everything extends base.html, and public pages stay self-contained."""
import re
from pathlib import Path

from django.conf import settings
from django.template.loader import render_to_string
from django.test import TestCase

from ..models import Customer


class TemplateStructureTests(TestCase):
    def test_every_page_template_extends_base_html(self):
        templates_dir = Path(settings.BASE_DIR) / 'templates'
        not_pages = {'base.html', '_base.css'}
        pages = [p for p in templates_dir.rglob('*') if p.is_file() and p.name not in not_pages]
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
