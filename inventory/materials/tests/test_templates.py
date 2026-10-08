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


class CopyLinkScriptTests(TestCase):
    """The Copy Link button must work on a plain-http page, where navigator.clipboard doesn't exist."""

    def _run(self, harness):
        import json
        import re
        import shutil
        import subprocess
        import tempfile
        from django.template.loader import render_to_string
        if not shutil.which('node'):
            self.skipTest('node is not installed')
        script = re.search(r'<script>(.*?)</script>', render_to_string('materials/_copy_link_script.html'), re.S).group(1)
        with tempfile.NamedTemporaryFile('w', suffix='.js', delete=False) as handle:
            handle.write(script + harness)
        try:
            result = subprocess.run(['node', handle.name], capture_output=True, text=True, timeout=20)
        finally:
            os.unlink(handle.name)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    HARNESS_HEAD = """
      const out = {};
      const button = {textContent: 'Copy'};
      global.document = {
        createElement: () => ({style: {}, setAttribute() {}, select() {}}),
        body: {appendChild() {}, removeChild() {}},
        execCommand: (command) => { out.command = command; return COPY_WORKS; },
      };
      global.setTimeout = (fn) => fn();
    """

    def test_on_a_plain_http_page_it_copies_through_a_hidden_box(self):
        harness = self.HARNESS_HEAD.replace('COPY_WORKS', 'true') + """
        Object.defineProperty(globalThis, 'navigator', {value: {}, configurable: true});   // no clipboard on http
        global.window = {isSecureContext: false, prompt: () => { out.prompted = true; }};
        copyLink('https://x.example/quote/abc/', button);
        console.log(JSON.stringify({command: out.command, prompted: !!out.prompted, label: button.textContent}));
        """
        result = self._run(harness)
        self.assertEqual(result['command'], 'copy')
        self.assertFalse(result['prompted'])
        self.assertEqual(result['label'], 'Copy')   # restored after the "✓ Copied!" flash (timers run at once here)

    def test_if_copying_is_refused_the_link_is_shown_to_copy_by_hand(self):
        harness = self.HARNESS_HEAD.replace('COPY_WORKS', 'false') + """
        Object.defineProperty(globalThis, 'navigator', {value: {}, configurable: true});
        global.window = {isSecureContext: false, prompt: (message, url) => { out.url = url; }};
        copyLink('https://x.example/quote/abc/', button);
        console.log(JSON.stringify({url: out.url}));
        """
        self.assertEqual(self._run(harness)['url'], 'https://x.example/quote/abc/')

    def test_on_a_secure_page_it_uses_the_clipboard_api(self):
        harness = self.HARNESS_HEAD.replace('COPY_WORKS', 'false') + """
        Object.defineProperty(globalThis, 'navigator', {value: {clipboard: {writeText: (url) => { out.url = url; return Promise.resolve(); }}}, configurable: true});   // navigator is read-only in newer Node
        global.window = {isSecureContext: true, prompt: () => { out.prompted = true; }};
        copyLink('https://x.example/quote/abc/', button);
        setImmediate(() => console.log(JSON.stringify({url: out.url, prompted: !!out.prompted, command: out.command || null})));
        """
        result = self._run(harness)
        self.assertEqual(result, {'url': 'https://x.example/quote/abc/', 'prompted': False, 'command': None})

    def test_every_page_with_a_copy_link_button_includes_the_one_shared_script(self):
        templates = Path(__file__).resolve().parents[2] / 'templates' / 'materials'
        checked = 0
        for page in sorted(templates.glob('*.html')):
            if page.name.startswith('_'):
                continue
            html = page.read_text()
            if 'copyLink(' in html or '_query_actions.html' in html:
                checked += 1
                with self.subTest(page=page.name):
                    self.assertIn('_copy_link_script.html', html)
                    self.assertNotIn('function copyLink', html)   # no private copy that can drift
        self.assertGreaterEqual(checked, 3)   # query dashboard, query detail, order dashboard


class PortalManifestTests(TestCase):
    """The employee pages offer an installable, address-bar-free app for the plant tablet; the
    customer-facing pages must not reference /static/ (see the templates section of CLAUDE.md)."""

    def test_employee_login_links_the_manifest_and_the_file_is_valid(self):
        import json
        from django.contrib.staticfiles import finders
        page = self.client.get('/employee-login/').content.decode()
        self.assertIn('rel="manifest"', page)
        path = finders.find('materials/portal.webmanifest')
        manifest = json.load(open(path))
        self.assertEqual((manifest['display'], manifest['start_url']), ('standalone', '/welcome/'))
        for icon in manifest['icons']:
            self.assertTrue(finders.find(icon['src'].replace('/static/', '')), icon['src'])
