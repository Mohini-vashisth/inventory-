"""Product codes: type + grade, e.g. FBB009 - made in the admin, only looked up by quotes.

Size is not part of a code: width and thickness vary per order."""
import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from ..models import Customer, GradeOption, ProductCategory, ProductType, Quotation, QuotationLineItem
from ..product_codes import canonical_grade, describe_product_code, grade_key, item_code_for, reserve_grade_number
from .helpers import create_query, quotation_item_post_data


class ItemCodeForTests(TestCase):
    """The code the admin's Add product code form generates when Item Code is left blank."""

    def setUp(self):
        self.flat_bar = ProductCategory.objects.get(name='Flat Bright Bar')       # FBB
        self.square_bar = ProductCategory.objects.get(name='Square Bright Bar')   # SQB

    def test_the_example_from_the_brief(self):
        """Flat Bright Bar in the first grade."""
        self.assertEqual(item_code_for(self.flat_bar, 'EN8D'), ('FBB001', ''))

    def test_the_type_letters_come_from_the_type(self):
        self.assertEqual(item_code_for(self.square_bar, 'EN8D')[0], 'SQB001')

    def test_a_new_grade_previews_the_next_free_number_and_reserving_it_takes_the_number(self):
        reserve_grade_number('EN8D')
        self.assertEqual(GradeOption.objects.get(name='EN8D').number, 1)
        self.assertEqual(item_code_for(self.flat_bar, 'SS304')[0], 'FBB002')
        reserve_grade_number('SS304')
        self.assertEqual(item_code_for(self.square_bar, 'EN8D')[0], 'SQB001')   # EN8D is still 001

    def test_a_listed_grade_without_a_number_is_numbered_when_first_used(self):
        GradeOption.objects.create(name='EN9')
        self.assertEqual(item_code_for(self.flat_bar, 'EN9')[0], 'FBB001')
        reserve_grade_number('EN9')
        self.assertEqual(GradeOption.objects.get(name='EN9').number, 1)

    def test_a_combination_that_already_has_a_code_is_refused(self):
        existing = ProductType.objects.create(item_code='FBB-OLD', category=self.flat_bar, grade='EN8D')
        code, reason = item_code_for(self.flat_bar, 'en8d')
        self.assertIsNone(code)
        self.assertIn(existing.item_code, reason)

    def test_the_code_being_edited_doesnt_count_as_its_own_duplicate(self):
        existing = ProductType.objects.create(item_code='FBB-OLD', category=self.flat_bar, grade='EN8D')
        self.assertEqual(item_code_for(self.flat_bar, 'EN8D', exclude_pk=existing.pk)[0], 'FBB001')

    def test_the_same_grade_in_another_type_is_a_different_code(self):
        self.assertNotEqual(item_code_for(self.flat_bar, 'EN8D')[0], item_code_for(self.square_bar, 'EN8D')[0])

    def test_a_hand_made_code_with_the_same_text_gets_a_suffix(self):
        ProductType.objects.create(item_code='FBB001', grade='OTHER')   # no type
        self.assertEqual(item_code_for(self.flat_bar, 'EN8D')[0], 'FBB001-2')

    def test_no_code_when_the_combination_cant_be_written(self):
        no_letters = ProductCategory.objects.create(name='Brand New Type')   # no 3-letter code yet
        cases = {
            'grade too long': (self.flat_bar, 'G' * 21),
            'type without letters': (no_letters, 'EN8D'),
            'no type': (None, 'EN8D'),
            'no grade': (self.flat_bar, ''),
        }
        for label, (category, grade) in cases.items():
            with self.subTest(case=label):
                code, reason = item_code_for(category, grade)
                self.assertIsNone(code)
                self.assertTrue(reason)
        self.assertEqual(GradeOption.objects.count(), 0)   # looking never adds anything

    def test_the_seven_types_have_distinct_three_letter_codes(self):
        codes = list(ProductCategory.objects.values_list('code', flat=True))
        self.assertEqual(len(codes), 7)
        self.assertEqual(len(set(codes)), 7)
        self.assertTrue(all(c and len(c) == 3 and c.isalpha() and c.isupper() for c in codes))
        self.assertEqual(ProductCategory.objects.get(name='Flat Bright Bar').code, 'FBB')

    def test_canonical_grade_is_capitals_and_digits_only_and_never_refuses_a_new_grade(self):
        self.assertEqual(canonical_grade(' en8d '), 'EN8D')
        self.assertEqual(canonical_grade('en-8d cr'), 'EN8DCR')
        self.assertEqual(canonical_grade('Brand-New 7'), 'BRANDNEW7')
        self.assertEqual(canonical_grade(None), '')


class DescribeProductCodeTests(TestCase):
    def setUp(self):
        self.flat_bar = ProductCategory.objects.get(name='Flat Bright Bar')

    def test_a_missing_code_previews_what_it_would_be_without_creating_anything(self):
        result = describe_product_code(self.flat_bar, 'EN8D')
        self.assertEqual(result, {'exists': False, 'item_code': 'FBB001', 'reason': ''})
        self.assertEqual((ProductType.objects.count(), GradeOption.objects.count()), (0, 0))

    def test_an_existing_code_is_reported_as_existing(self):
        ProductType.objects.create(item_code='FBB001', category=self.flat_bar, grade='EN8D')
        self.assertEqual(describe_product_code(self.flat_bar, 'en8d'),
                         {'exists': True, 'item_code': 'FBB001', 'reason': ''})

    def test_an_incomplete_combination_describes_nothing(self):
        self.assertEqual(describe_product_code(None, 'EN8D'), {'exists': False, 'item_code': None, 'reason': ''})
        self.assertEqual(describe_product_code(self.flat_bar, ''), {'exists': False, 'item_code': None, 'reason': ''})


class ProductCodeLookupEndpointTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user('lookup_staff', password='pw', is_staff=True)
        self.flat_bar = ProductCategory.objects.get(name='Flat Bright Bar')
        self.url = reverse('product_code_lookup')

    def _get(self, **params):
        return self.client.get(self.url, params)

    def test_a_missing_code_comes_with_a_prefilled_admin_link(self):
        self.client.force_login(self.staff)
        data = self._get(category=self.flat_bar.pk, grade='EN8D').json()
        self.assertEqual((data['exists'], data['item_code']), (False, 'FBB001'))
        link = urlparse(data['add_url'])
        self.assertEqual(link.path, reverse('admin:materials_producttype_add'))
        self.assertEqual(parse_qs(link.query), {'category': [str(self.flat_bar.pk)], 'grade': ['EN8D']})

    def test_an_existing_code_has_no_add_link(self):
        ProductType.objects.create(item_code='FBB001', category=self.flat_bar, grade='EN8D')
        self.client.force_login(self.staff)
        data = self._get(category=self.flat_bar.pk, grade='EN8D').json()
        self.assertTrue(data['exists'])
        self.assertNotIn('add_url', data)

    def test_the_reason_is_returned_when_no_code_can_be_generated(self):
        no_letters = ProductCategory.objects.create(name='Brand New Type')
        self.client.force_login(self.staff)
        data = self._get(category=no_letters.pk, grade='EN8D').json()
        self.assertIsNone(data['item_code'])
        self.assertIn('3-letter code', data['reason'])

    def test_bad_or_missing_input_is_harmless(self):
        self.client.force_login(self.staff)
        for params in ({}, {'category': 'abc', 'grade': 'EN8D'}, {'category': self.flat_bar.pk}):
            with self.subTest(params=params):
                self.assertEqual(self._get(**params).json(), {'exists': False, 'item_code': None, 'reason': ''})

    def test_it_never_creates_anything(self):
        self.client.force_login(self.staff)
        self._get(category=self.flat_bar.pk, grade='EN8D')
        self.assertEqual((ProductType.objects.count(), GradeOption.objects.count()), (0, 0))

    def test_anonymous_requests_are_refused(self):
        self.assertEqual(self._get(category=self.flat_bar.pk, grade='EN8D').status_code, 403)


class AdminGeneratesTheCodeTests(TestCase):
    """The admin's Add product code form: leave Item Code blank and it's generated."""

    def setUp(self):
        self.admin = User.objects.create_superuser('code_admin', 'a@example.com', 'pw')
        self.client.force_login(self.admin)
        self.flat_bar = ProductCategory.objects.get(name='Flat Bright Bar')
        self.square_bar = ProductCategory.objects.get(name='Square Bright Bar')
        self.add_url = reverse('admin:materials_producttype_add')

    def _data(self, **fields):
        data = {'category': str(self.flat_bar.pk), 'item_code': '', 'grade': 'EN8D', 'description': '',
                'steps-TOTAL_FORMS': '0', 'steps-INITIAL_FORMS': '0', 'steps-MIN_NUM_FORMS': '0', 'steps-MAX_NUM_FORMS': '1000',
                'allowed_specs-TOTAL_FORMS': '0', 'allowed_specs-INITIAL_FORMS': '0',
                'allowed_specs-MIN_NUM_FORMS': '0', 'allowed_specs-MAX_NUM_FORMS': '1000'}
        data.update(fields)
        return data

    def _add(self, **fields):
        return self.client.post(self.add_url, self._data(**fields))

    def test_a_blank_item_code_is_generated_from_type_and_grade(self):
        response = self._add()
        self.assertEqual(response.status_code, 302)
        code = ProductType.objects.get()
        self.assertEqual((code.item_code, code.category, code.grade), ('FBB001', self.flat_bar, 'EN8D'))

    def test_there_is_no_size_field_on_the_form(self):
        html = self.client.get(self.add_url).content.decode()
        self.assertNotIn('id="id_size"', html)

    def test_saving_reserves_the_grade_number_so_the_next_new_grade_gets_the_next_one(self):
        self._add()
        self.assertEqual(GradeOption.objects.get(name='EN8D').number, 1)
        self._add(grade='SS304')
        self.assertEqual(ProductType.objects.get(grade='SS304').item_code, 'FBB002')

    def test_the_grade_is_saved_as_capitals_and_digits_only(self):
        GradeOption.objects.create(name='EN-8D', number=9)   # listed in the one spelling, EN8D
        self._add(grade='en-8d')
        code = ProductType.objects.get()
        self.assertEqual((code.grade, code.item_code), ('EN8D', 'FBB009'))
        self.assertEqual(GradeOption.objects.count(), 1)

    def test_a_code_typed_by_hand_is_kept_and_takes_no_grade_number(self):
        self._add(item_code='MY-OWN-CODE')
        self.assertEqual(ProductType.objects.get().item_code, 'MY-OWN-CODE')
        self.assertEqual(GradeOption.objects.count(), 0)

    def test_type_and_grade_are_required_even_with_a_hand_typed_code(self):
        for field in ('category', 'grade'):
            with self.subTest(field=field):
                response = self._add(item_code='MY-CODE', **{field: ''})
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, 'This field is required')
                self.assertEqual(ProductType.objects.count(), 0)

    def test_item_code_is_the_first_field_on_the_form(self):
        html = self.client.get(self.add_url).content.decode()
        self.assertLess(html.index('id="id_item_code"'), html.index('id="id_category"'))

    def test_a_type_without_letters_explains_why(self):
        bare = ProductCategory.objects.create(name='Brand New Type')
        self.assertContains(self._add(category=str(bare.pk)), '3-letter code')

    def test_a_duplicate_of_an_existing_code_is_refused(self):
        self._add()
        response = self._add()
        self.assertContains(response, 'already exists: FBB001')
        self.assertEqual(ProductType.objects.count(), 1)

    def test_a_differently_spelled_duplicate_is_refused_too(self):
        self._add(grade='EN-8D')
        response = self._add(grade='EN8D')
        self.assertContains(response, 'already exists')
        self.assertEqual(ProductType.objects.count(), 1)

    def test_the_same_grade_under_another_type_is_fine(self):
        self._add()
        self._add(category=str(self.square_bar.pk))
        self.assertEqual(sorted(ProductType.objects.values_list('item_code', flat=True)), ['FBB001', 'SQB001'])

    def test_editing_a_code_and_clearing_item_code_regenerates_it_without_calling_it_a_duplicate(self):
        self._add()
        code = ProductType.objects.get()
        response = self.client.post(reverse('admin:materials_producttype_change', args=[code.pk]),
                                    self._data(description='x'))
        self.assertEqual(response.status_code, 302)
        code.refresh_from_db()
        self.assertEqual((code.item_code, code.description), ('FBB001', 'x'))

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
        html = self.client.get(f"{self.add_url}?category={self.square_bar.pk}&grade=EN8D").content.decode()
        self.assertRegex(html, rf'<option value="{self.square_bar.pk}"\s+selected>')
        self.assertIn('value="EN8D"', html)

    def test_the_live_fill_script_parses(self):
        script = Path(__file__).resolve().parents[1] / 'static' / 'materials' / 'admin_product_code.js'
        self.assertTrue(script.exists())
        if shutil.which('node'):
            result = subprocess.run(['node', '--check', str(script)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)


class QuoteSendRequiresACodeTests(TestCase):
    """Quotes only look codes up. With type and grade chosen but no code in the
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

    def test_sending_is_refused_when_no_code_exists_for_the_chosen_type_and_grade(self):
        response, dispatch = self._post(category=str(self.flat_bar.pk), grade='EN8D')
        self.assertEqual(response.status_code, 200)   # the form again, not a redirect
        self.assertContains(response, 'no product code exists for: Flat Bright Bar · EN8D')
        self.assertContains(response, 'Ask the admin')
        self.assertFalse(Quotation.objects.exists())
        dispatch.assert_not_called()

    def test_the_error_links_to_the_admin_add_page_with_the_values_filled_in(self):
        response, _ = self._post(category=str(self.flat_bar.pk), grade='EN8D')
        link = f"{reverse('admin:materials_producttype_add')}?category={self.flat_bar.pk}&amp;grade=EN8D"
        self.assertContains(response, f'href="{link}"')
        self.assertContains(response, 'Add product code: Flat Bright Bar · EN8D')

    def test_nothing_is_created_by_a_refused_send(self):
        self._post(category=str(self.flat_bar.pk), grade='EN8D')
        self.assertEqual((ProductType.objects.count(), GradeOption.objects.count(), QuotationLineItem.objects.count()), (0, 0, 0))

    def test_what_the_owner_typed_is_still_on_the_page_after_a_refusal(self):
        response, _ = self._post(category=str(self.flat_bar.pk), grade='EN8D', description='Flat bar for Rao')
        self.assertContains(response, 'value="Flat bar for Rao"')

    def test_sending_works_once_the_admin_has_added_the_code(self):
        self._post(category=str(self.flat_bar.pk), grade='EN8D')
        code = ProductType.objects.create(item_code='FBB001', category=self.flat_bar, grade='EN8D')
        response, dispatch = self._post(category=str(self.flat_bar.pk), grade='en8d')
        self.assertEqual(response.status_code, 302)
        self.assertEqual(QuotationLineItem.objects.get().product_type, code)
        dispatch.assert_called_once()

    def test_the_width_and_thickness_do_not_change_which_code_is_used(self):
        code = ProductType.objects.create(item_code='FBB001', category=self.flat_bar, grade='EN8D')
        for width, thickness in (('50', '6'), ('120', '10.5')):
            with self.subTest(width=width):
                self._post(category=str(self.flat_bar.pk), grade='EN8D', width=width, thickness=thickness)
        self.assertEqual({line.product_type for line in QuotationLineItem.objects.all()}, {code})
        self.assertEqual(QuotationLineItem.objects.count(), 2)

    def test_a_code_picked_by_hand_is_enough(self):
        picked = ProductType.objects.create(item_code='HAND-1', category=self.flat_bar, grade='SS304')
        response, _ = self._post(category=str(self.flat_bar.pk), grade='EN8D', product_type=str(picked.pk))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(QuotationLineItem.objects.get().product_type, picked)

    def test_saving_a_draft_is_never_blocked(self):
        response, _ = self._post(action='save_draft', category=str(self.flat_bar.pk), grade='EN8D')
        self.assertEqual(response.status_code, 302)
        self.assertIsNone(QuotationLineItem.objects.get().product_type)
        self.assertEqual(ProductType.objects.count(), 0)

    def test_an_item_without_a_type_and_grade_is_not_blocked(self):
        for index, spec in enumerate(({}, {'grade': 'EN8D'}, {'category': str(self.flat_bar.pk)})):
            with self.subTest(spec=spec):
                response, _ = self._post(**spec)
                self.assertEqual(response.status_code, 302)
                self.assertEqual(Quotation.objects.filter(status='sent').count(), index + 1)

    def test_only_the_items_missing_a_code_are_listed(self):
        ProductType.objects.create(item_code='HAS-ONE', category=self.flat_bar, grade='SS304')
        data = quotation_item_post_data(**{
            'action': 'send', 'item-TOTAL_FORMS': '2', 'item-INITIAL_FORMS': '0',
            'item-0-category': str(self.flat_bar.pk), 'item-0-grade': 'SS304',
            'item-1-description': 'Second', 'item-1-width': '40', 'item-1-thickness': '5', 'item-1-quantity': '5',
            'item-1-rate_per_kg': '10', 'item-1-unit': 'KGS',
            'item-1-category': str(self.flat_bar.pk), 'item-1-grade': 'EN8D',
        })
        with patch('materials.views.quotations._dispatch_quote_email'):
            response = self.client.post(f"{reverse('quotation_form')}?customer={self.customer.pk}", data)
        self.assertContains(response, 'no product code exists for: Flat Bright Bar · EN8D')
        self.assertNotContains(response, 'Flat Bright Bar · SS304')
        self.assertFalse(Quotation.objects.exists())

    def test_the_page_carries_the_preview_url_and_hint_style(self):
        html = self.client.get(reverse('quotation_form')).content.decode()
        self.assertIn(reverse('product_code_lookup'), html)
        self.assertIn('code-hint', html)
        self.assertIn('Add it in the admin before sending', html)


class GradeSpellingTests(TestCase):
    """EN8D, en-8d and EN 8D are the same grade; EN-8D CR is not."""

    def setUp(self):
        self.flat = ProductCategory.objects.get(name='Flat Bright Bar')
        GradeOption.objects.create(name='EN-8D', number=9)
        GradeOption.objects.create(name='EN-8D CR', number=10)
        self.code = ProductType.objects.create(item_code='FBB009', category=self.flat, grade='EN-8D')

    def test_the_key_ignores_case_spaces_and_punctuation(self):
        self.assertEqual({grade_key(g) for g in ('EN8D', 'en-8d', 'EN 8D', ' EN-8D ')}, {'en8d'})
        self.assertNotEqual(grade_key('EN-8D CR'), grade_key('EN8D'))
        self.assertEqual(grade_key(None), '')

    def test_a_typed_spelling_is_stored_as_capitals_and_digits(self):
        self.assertEqual(canonical_grade('EN-8D'), 'EN8D')
        self.assertEqual(canonical_grade('en 8d cr'), 'EN8DCR')
        self.assertEqual(canonical_grade('NEW-1'), 'NEW1')

    def test_every_grade_field_normalizes_on_save(self):
        from ..models import Material, normalize_grade
        self.assertEqual(normalize_grade('SAE 1008'), 'SAE1008')
        self.assertEqual(Material.objects.create(grade='en-1a (pb)', quantity=1).grade, 'EN1APB')
        self.assertEqual(create_query(source='call', grade='sae 6165').items.get().grade, 'SAE6165')
        self.assertIsNone(Material.objects.create(grade=None, quantity=1).grade)
        self.assertEqual(GradeOption.objects.create(name='hc-1').name, 'HC1')

    def test_the_code_is_found_whichever_way_the_grade_is_spelled(self):
        from ..views.common import _match_product_type
        for typed in ('EN8D', 'en-8d', 'EN 8D'):
            with self.subTest(typed=typed):
                self.assertEqual(_match_product_type(typed, self.flat), self.code)
                self.assertEqual(describe_product_code(self.flat, typed)['item_code'], 'FBB009')
        self.assertIsNone(_match_product_type('EN8D CR', self.flat))

    def test_a_differently_spelled_duplicate_is_refused_and_reuses_the_grade_number(self):
        code, reason = item_code_for(self.flat, 'EN8D')
        self.assertIsNone(code)
        self.assertIn('FBB009', reason)
        square = ProductCategory.objects.get(name='Square Bright Bar')
        self.assertEqual(item_code_for(square, 'EN8D')[0], 'SQB009')   # EN-8D's number, 9
        reserve_grade_number('EN8D')
        self.assertEqual(GradeOption.objects.filter(name__startswith='EN').count(), 2)   # no second option

    def test_the_forms_code_maps_use_the_same_key(self):
        staff = User.objects.create_user('key_staff', password='pw', is_staff=True)
        self.client.force_login(staff)
        from ..models import Query
        query = Query.objects.create(source='call', contact_phone='9123456780')
        html = self.client.get(reverse('query_edit', kwargs={'pk': query.pk})).content.decode()
        self.assertIn('"grade": "en8d"', html)
