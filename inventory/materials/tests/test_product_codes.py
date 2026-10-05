"""Product codes: type + grade + size, e.g. FBB00100120 - made in the admin, only looked up by quotes."""
from decimal import Decimal
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from ..models import Customer, GradeOption, ProductCategory, ProductType, Quotation, QuotationLineItem
from ..product_codes import canonical_grade, describe_product_code, item_code_for, reserve_grade_number, size_digits
from .helpers import quotation_item_post_data


class SizeDigitsTests(TestCase):
    def test_size_is_written_in_hundredths_of_a_mm_over_five_digits(self):
        cases = {'1.2': '00120', '1.200': '00120', '12': '01200', '0.5': '00050', '2.505': None, '25.4': '02540',
                 '100': '10000', '999.99': '99999', '1000': None, '0': None, '-1': None, '0.001': None}
        for size, expected in cases.items():
            with self.subTest(size=size):
                self.assertEqual(size_digits(Decimal(size)), expected)

    def test_no_size_has_no_digits(self):
        self.assertIsNone(size_digits(None))


class ItemCodeForTests(TestCase):
    """The code the admin's Add product code form generates when Item Code is left blank."""

    def setUp(self):
        self.flat_bar = ProductCategory.objects.get(name='Flat Bright Bar')     # FBB
        self.round_bar = ProductCategory.objects.get(name='Round Bright Bar')   # RBB

    def test_the_example_from_the_brief(self):
        """Flat Bright Bar, EN8D (the first grade), 1.2 mm."""
        self.assertEqual(item_code_for(self.flat_bar, 'EN8D', Decimal('1.2')), ('FBB00100120', ''))

    def test_the_type_letters_come_from_the_type(self):
        self.assertEqual(item_code_for(self.round_bar, 'EN8D', Decimal('12'))[0], 'RBB00101200')

    def test_a_new_grade_previews_the_next_free_number_and_reserving_it_takes_the_number(self):
        reserve_grade_number('EN8D')
        self.assertEqual(GradeOption.objects.get(name='EN8D').number, 1)
        self.assertEqual(item_code_for(self.flat_bar, 'SS304', Decimal('1.2'))[0], 'FBB00200120')
        reserve_grade_number('SS304')
        self.assertEqual(item_code_for(self.flat_bar, 'EN8D', Decimal('2'))[0], 'FBB00100200')   # EN8D is still 001

    def test_the_grade_matches_a_listed_one_without_regard_to_case(self):
        reserve_grade_number('EN8D')
        self.assertEqual(item_code_for(self.flat_bar, ' en8d ', Decimal('1.5'))[0], 'FBB00100150')
        self.assertEqual(GradeOption.objects.count(), 1)

    def test_a_listed_grade_without_a_number_is_numbered_when_first_used(self):
        GradeOption.objects.create(name='EN9')
        self.assertEqual(item_code_for(self.flat_bar, 'EN9', Decimal('3'))[0], 'FBB00100300')
        reserve_grade_number('EN9')
        self.assertEqual(GradeOption.objects.get(name='EN9').number, 1)

    def test_a_combination_that_already_has_a_code_is_refused(self):
        existing = ProductType.objects.create(item_code='FBB-OLD', category=self.flat_bar, grade='EN8D', size='1.200')
        code, reason = item_code_for(self.flat_bar, 'en8d', Decimal('1.2'))
        self.assertIsNone(code)
        self.assertIn(existing.item_code, reason)

    def test_the_code_being_edited_doesnt_count_as_its_own_duplicate(self):
        existing = ProductType.objects.create(item_code='FBB-OLD', category=self.flat_bar, grade='EN8D', size='1.200')
        self.assertEqual(item_code_for(self.flat_bar, 'EN8D', Decimal('1.2'), exclude_pk=existing.pk)[0], 'FBB00100120')

    def test_the_same_grade_and_size_in_another_type_is_a_different_code(self):
        self.assertNotEqual(item_code_for(self.flat_bar, 'EN8D', Decimal('12'))[0],
                            item_code_for(self.round_bar, 'EN8D', Decimal('12'))[0])

    def test_a_hand_made_code_with_the_same_text_gets_a_suffix(self):
        ProductType.objects.create(item_code='FBB00100120', grade='OTHER', size='9.000')   # no type
        self.assertEqual(item_code_for(self.flat_bar, 'EN8D', Decimal('1.2'))[0], 'FBB00100120-2')

    def test_no_code_when_the_combination_cant_be_written(self):
        no_letters = ProductCategory.objects.create(name='Brand New Type')   # no 3-letter code yet
        cases = {
            'size finer than 0.01 mm': (self.flat_bar, 'EN8D', Decimal('1.205')),
            'size too big': (self.flat_bar, 'EN8D', Decimal('1000')),
            'size zero': (self.flat_bar, 'EN8D', Decimal('0')),
            'grade too long': (self.flat_bar, 'G' * 21, Decimal('2')),
            'type without letters': (no_letters, 'EN8D', Decimal('2')),
            'no type': (None, 'EN8D', Decimal('2')),
            'no grade': (self.flat_bar, '', Decimal('2')),
            'no size': (self.flat_bar, 'EN8D', None),
        }
        for label, (category, grade, size) in cases.items():
            with self.subTest(case=label):
                code, reason = item_code_for(category, grade, size)
                self.assertIsNone(code)
                self.assertTrue(reason)
        self.assertEqual(GradeOption.objects.count(), 0)   # looking never adds anything

    def test_the_seeded_types_all_have_distinct_three_letter_codes(self):
        codes = list(ProductCategory.objects.values_list('code', flat=True))
        self.assertEqual(len(codes), 14)
        self.assertEqual(len(set(codes)), 14)
        self.assertTrue(all(c and len(c) == 3 and c.isalpha() and c.isupper() for c in codes))
        self.assertEqual(ProductCategory.objects.get(name='Flat Bright Bar').code, 'FBB')

    def test_canonical_grade_uses_the_listed_spelling_but_never_refuses_a_new_one(self):
        GradeOption.objects.create(name='EN8D')
        self.assertEqual(canonical_grade(' en8d '), 'EN8D')
        self.assertEqual(canonical_grade('Brand-New 7'), 'Brand-New 7')
        self.assertEqual(canonical_grade(None), '')


class DescribeProductCodeTests(TestCase):
    def setUp(self):
        self.flat_bar = ProductCategory.objects.get(name='Flat Bright Bar')

    def test_a_missing_code_previews_what_it_would_be_without_creating_anything(self):
        result = describe_product_code(self.flat_bar, 'EN8D', Decimal('1.2'))
        self.assertEqual(result, {'exists': False, 'item_code': 'FBB00100120', 'reason': ''})
        self.assertEqual((ProductType.objects.count(), GradeOption.objects.count()), (0, 0))

    def test_an_existing_code_is_reported_as_existing(self):
        ProductType.objects.create(item_code='FBB00100120', category=self.flat_bar, grade='EN8D', size='1.200')
        self.assertEqual(describe_product_code(self.flat_bar, 'en8d', Decimal('1.2')),
                         {'exists': True, 'item_code': 'FBB00100120', 'reason': ''})

    def test_an_incomplete_combination_describes_nothing(self):
        self.assertEqual(describe_product_code(None, 'EN8D', Decimal('1')), {'exists': False, 'item_code': None, 'reason': ''})


class ProductCodeLookupEndpointTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user('lookup_staff', password='pw', is_staff=True)
        self.flat_bar = ProductCategory.objects.get(name='Flat Bright Bar')
        self.url = reverse('product_code_lookup')

    def _get(self, **params):
        return self.client.get(self.url, params)

    def test_a_missing_code_comes_with_a_prefilled_admin_link(self):
        self.client.force_login(self.staff)
        data = self._get(category=self.flat_bar.pk, grade='EN8D', size='1.2').json()
        self.assertEqual((data['exists'], data['item_code']), (False, 'FBB00100120'))
        link = urlparse(data['add_url'])
        self.assertEqual(link.path, reverse('admin:materials_producttype_add'))
        self.assertEqual(parse_qs(link.query), {'category': [str(self.flat_bar.pk)], 'grade': ['EN8D'], 'size': ['1.2']})

    def test_an_existing_code_has_no_add_link(self):
        ProductType.objects.create(item_code='FBB00100120', category=self.flat_bar, grade='EN8D', size='1.200')
        self.client.force_login(self.staff)
        data = self._get(category=self.flat_bar.pk, grade='EN8D', size='1.2').json()
        self.assertTrue(data['exists'])
        self.assertNotIn('add_url', data)

    def test_the_reason_is_returned_when_no_code_can_be_generated(self):
        self.client.force_login(self.staff)
        data = self._get(category=self.flat_bar.pk, grade='EN8D', size='1.205').json()
        self.assertIsNone(data['item_code'])
        self.assertIn('2 decimals', data['reason'])

    def test_bad_or_missing_input_is_harmless(self):
        self.client.force_login(self.staff)
        for params in ({}, {'category': 'abc', 'grade': 'EN8D', 'size': '1'}, {'category': self.flat_bar.pk, 'size': 'x'}):
            with self.subTest(params=params):
                self.assertEqual(self._get(**params).json(), {'exists': False, 'item_code': None, 'reason': ''})

    def test_it_never_creates_anything(self):
        self.client.force_login(self.staff)
        self._get(category=self.flat_bar.pk, grade='EN8D', size='1.2')
        self.assertEqual((ProductType.objects.count(), GradeOption.objects.count()), (0, 0))

    def test_anonymous_requests_are_refused(self):
        self.assertEqual(self._get(category=self.flat_bar.pk, grade='EN8D', size='1.2').status_code, 403)


class AdminGeneratesTheCodeTests(TestCase):
    """The admin's Add product code form: leave Item Code blank and it's generated."""

    def setUp(self):
        self.admin = User.objects.create_superuser('code_admin', 'a@example.com', 'pw')
        self.client.force_login(self.admin)
        self.flat_bar = ProductCategory.objects.get(name='Flat Bright Bar')
        self.round_bar = ProductCategory.objects.get(name='Round Bright Bar')
        self.add_url = reverse('admin:materials_producttype_add')

    def _add(self, **fields):
        data = {'category': str(self.flat_bar.pk), 'item_code': '', 'grade': 'EN8D', 'size': '1.2', 'description': '',
                'steps-TOTAL_FORMS': '0', 'steps-INITIAL_FORMS': '0', 'steps-MIN_NUM_FORMS': '0', 'steps-MAX_NUM_FORMS': '1000',
                'allowed_specs-TOTAL_FORMS': '0', 'allowed_specs-INITIAL_FORMS': '0',
                'allowed_specs-MIN_NUM_FORMS': '0', 'allowed_specs-MAX_NUM_FORMS': '1000'}
        data.update(fields)
        return self.client.post(self.add_url, data)

    def test_a_blank_item_code_is_generated_from_type_grade_and_size(self):
        response = self._add()
        self.assertEqual(response.status_code, 302)
        code = ProductType.objects.get()
        self.assertEqual((code.item_code, code.category, code.grade, code.size), ('FBB00100120', self.flat_bar, 'EN8D', Decimal('1.2')))

    def test_saving_reserves_the_grade_number_so_the_next_new_grade_gets_the_next_one(self):
        self._add()
        self.assertEqual(GradeOption.objects.get(name='EN8D').number, 1)
        self._add(grade='SS304')
        self.assertEqual(ProductType.objects.get(grade='SS304').item_code, 'FBB00200120')

    def test_the_grade_is_tidied_to_the_listed_spelling(self):
        GradeOption.objects.create(name='EN8D', number=1)
        self._add(grade='en8d', size='2')
        code = ProductType.objects.get()
        self.assertEqual((code.grade, code.item_code), ('EN8D', 'FBB00100200'))
        self.assertEqual(GradeOption.objects.count(), 1)

    def test_a_code_typed_by_hand_is_kept_and_takes_no_grade_number(self):
        self._add(item_code='MY-OWN-CODE')
        self.assertEqual(ProductType.objects.get().item_code, 'MY-OWN-CODE')
        self.assertEqual(GradeOption.objects.count(), 0)

    def test_blank_code_with_a_missing_part_is_an_error_not_a_guess(self):
        for label, change in {'no size': {'size': ''}, 'no grade': {'grade': ''}, 'no type': {'category': ''}}.items():
            with self.subTest(case=label):
                response = self._add(**change)
                self.assertEqual(response.status_code, 200)   # form redisplayed with an error
                self.assertEqual(ProductType.objects.count(), 0)

    def test_a_size_that_cant_be_coded_explains_why(self):
        response = self._add(size='1.205')
        self.assertContains(response, '2 decimals')
        self.assertEqual(ProductType.objects.count(), 0)

    def test_a_type_without_letters_explains_why(self):
        bare = ProductCategory.objects.create(name='Brand New Type')
        self.assertContains(self._add(category=str(bare.pk)), '3-letter code')

    def test_a_duplicate_of_an_existing_code_is_refused(self):
        self._add()
        response = self._add()
        self.assertContains(response, 'already exists: FBB00100120')
        self.assertEqual(ProductType.objects.count(), 1)

    def test_editing_a_code_and_clearing_item_code_regenerates_it_without_calling_it_a_duplicate(self):
        self._add()
        code = ProductType.objects.get()
        data = {'category': str(self.flat_bar.pk), 'item_code': '', 'grade': 'EN8D', 'size': '1.2', 'description': 'x',
                'steps-TOTAL_FORMS': '0', 'steps-INITIAL_FORMS': '0', 'steps-MIN_NUM_FORMS': '0', 'steps-MAX_NUM_FORMS': '1000',
                'allowed_specs-TOTAL_FORMS': '0', 'allowed_specs-INITIAL_FORMS': '0',
                'allowed_specs-MIN_NUM_FORMS': '0', 'allowed_specs-MAX_NUM_FORMS': '1000'}
        response = self.client.post(reverse('admin:materials_producttype_change', args=[code.pk]), data)
        self.assertEqual(response.status_code, 302)
        code.refresh_from_db()
        self.assertEqual((code.item_code, code.description), ('FBB00100120', 'x'))

    def test_the_form_has_the_live_fill_script_the_grade_suggestions_and_the_help_text(self):
        GradeOption.objects.create(name='EN8D', number=1)
        html = self.client.get(self.add_url).content.decode()
        self.assertIn('materials/admin_product_code.js', html)
        self.assertIn(f'data-lookup-url="{reverse("product_code_lookup")}"', html)
        self.assertIn('<datalist id="grade-options"><option value="EN8D"></datalist>', html)
        self.assertIn('list="grade-options"', html)
        self.assertIn('Leave blank', html)

    def test_the_add_page_can_be_prefilled_from_a_link(self):
        """The quote form's "add it in the admin" link opens the form with the values in."""
        html = self.client.get(f"{self.add_url}?category={self.round_bar.pk}&grade=EN8D&size=1.2").content.decode()
        self.assertRegex(html, rf'<option value="{self.round_bar.pk}"\s+selected>')
        self.assertIn('value="EN8D"', html)
        self.assertIn('value="1.2"', html)

    def test_the_live_fill_script_parses(self):
        import shutil
        import subprocess
        from pathlib import Path
        script = Path(__file__).resolve().parents[1] / 'static' / 'materials' / 'admin_product_code.js'
        self.assertTrue(script.exists())
        if shutil.which('node'):
            result = subprocess.run(['node', '--check', str(script)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)


class QuoteSendRequiresACodeTests(TestCase):
    """Quotes only look codes up. With type, grade and size chosen but no code in the
    catalogue, sending is refused until the admin adds one."""

    def setUp(self):
        self.staff = User.objects.create_user('send_staff', password='pw', is_staff=True)
        self.client.force_login(self.staff)
        self.customer = Customer.objects.create(name='Code Co', email='c@example.com')
        self.flat_bar = ProductCategory.objects.get(name='Flat Bright Bar')

    def _post(self, action='send', **item):
        data = quotation_item_post_data(**{f'item-0-{k}': v for k, v in item.items()}, action=action)
        with patch('materials.views.quotations._dispatch_quote_email') as dispatch:
            response = self.client.post(f"{reverse('quotation_form')}?customer={self.customer.pk}", data)
        return response, dispatch

    def test_sending_is_refused_when_no_code_exists_for_the_chosen_type_grade_and_size(self):
        response, dispatch = self._post(category=str(self.flat_bar.pk), grade='EN8D', size='1.2')
        self.assertEqual(response.status_code, 200)   # the form again, not a redirect
        self.assertContains(response, 'no product code exists for: Flat Bright Bar · EN8D · 1.2 mm')
        self.assertContains(response, 'Ask the admin')
        self.assertFalse(Quotation.objects.exists())
        dispatch.assert_not_called()

    def test_the_error_links_to_the_admin_add_page_with_the_values_filled_in(self):
        response, _ = self._post(category=str(self.flat_bar.pk), grade='EN8D', size='1.2')
        link = f"{reverse('admin:materials_producttype_add')}?category={self.flat_bar.pk}&amp;grade=EN8D&amp;size=1.2"
        self.assertContains(response, f'href="{link}"')
        self.assertContains(response, 'Add product code: Flat Bright Bar · EN8D · 1.2 mm')

    def test_nothing_is_created_by_a_refused_send(self):
        self._post(category=str(self.flat_bar.pk), grade='EN8D', size='1.2')
        self.assertEqual((ProductType.objects.count(), GradeOption.objects.count(), QuotationLineItem.objects.count()), (0, 0, 0))

    def test_what_the_owner_typed_is_still_on_the_page_after_a_refusal(self):
        response, _ = self._post(category=str(self.flat_bar.pk), grade='EN8D', size='1.2', description='Flat bar for Rao')
        self.assertContains(response, 'value="Flat bar for Rao"')

    def test_sending_works_once_the_admin_has_added_the_code(self):
        self._post(category=str(self.flat_bar.pk), grade='EN8D', size='1.2')
        code = ProductType.objects.create(item_code='FBB00100120', category=self.flat_bar, grade='EN8D', size='1.200')
        response, dispatch = self._post(category=str(self.flat_bar.pk), grade='en8d', size='1.200')
        self.assertEqual(response.status_code, 302)
        self.assertEqual(QuotationLineItem.objects.get().product_type, code)
        dispatch.assert_called_once()

    def test_a_code_picked_by_hand_is_enough(self):
        picked = ProductType.objects.create(item_code='HAND-1', category=self.flat_bar, grade='SS304', size='5.000')
        response, _ = self._post(category=str(self.flat_bar.pk), grade='EN8D', size='1.2', product_type=str(picked.pk))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(QuotationLineItem.objects.get().product_type, picked)

    def test_saving_a_draft_is_never_blocked(self):
        response, _ = self._post(action='save_draft', category=str(self.flat_bar.pk), grade='EN8D', size='1.2')
        self.assertEqual(response.status_code, 302)
        self.assertIsNone(QuotationLineItem.objects.get().product_type)
        self.assertEqual(ProductType.objects.count(), 0)

    def test_an_item_without_a_full_type_grade_and_size_is_not_blocked(self):
        for index, spec in enumerate(({}, {'grade': 'EN8D', 'size': '1.2'}, {'category': str(self.flat_bar.pk), 'grade': 'EN8D'})):
            with self.subTest(spec=spec):
                response, _ = self._post(**spec)
                self.assertEqual(response.status_code, 302)
                self.assertEqual(Quotation.objects.filter(status='sent').count(), index + 1)

    def test_only_the_items_missing_a_code_are_listed(self):
        ProductType.objects.create(item_code='HAS-ONE', category=self.flat_bar, grade='SS304', size='2.000')
        data = quotation_item_post_data(**{
            'action': 'send', 'item-TOTAL_FORMS': '2', 'item-INITIAL_FORMS': '0',
            'item-0-category': str(self.flat_bar.pk), 'item-0-grade': 'SS304', 'item-0-size': '2',
            'item-1-description': 'Second', 'item-1-quantity': '5', 'item-1-rate_per_kg': '10', 'item-1-unit': 'KGS',
            'item-1-category': str(self.flat_bar.pk), 'item-1-grade': 'EN8D', 'item-1-size': '1.2',
        })
        with patch('materials.views.quotations._dispatch_quote_email'):
            response = self.client.post(f"{reverse('quotation_form')}?customer={self.customer.pk}", data)
        self.assertContains(response, 'no product code exists for: Flat Bright Bar · EN8D · 1.2 mm')
        self.assertNotContains(response, 'Flat Bright Bar · SS304')
        self.assertFalse(Quotation.objects.exists())

    def test_the_page_carries_the_preview_url_and_hint_style(self):
        html = self.client.get(reverse('quotation_form')).content.decode()
        self.assertIn(reverse('product_code_lookup'), html)
        self.assertIn('code-hint', html)
        self.assertIn('Add it in the admin before sending', html)
