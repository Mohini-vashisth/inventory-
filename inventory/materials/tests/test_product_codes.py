"""Generated product codes: type + grade + size, e.g. FBB00100120."""
from decimal import Decimal

from django.contrib.auth.models import User
from django.contrib.messages import get_messages
from django.test import TestCase
from django.urls import reverse
from unittest.mock import patch

from ..models import GradeOption, ProductCategory, ProductType, Quotation, QuotationLineItem
from ..product_codes import describe_product_code, get_or_create_product_code, size_digits
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


class CodeGenerationTests(TestCase):
    def setUp(self):
        self.flat_bar = ProductCategory.objects.get(name='Flat Bright Bar')   # FBB
        self.round_bar = ProductCategory.objects.get(name='Round Bright Bar')   # RBB

    def test_the_example_from_the_brief(self):
        """Flat Bright Bar, EN8D (the first grade), 1.2 mm."""
        code, created = get_or_create_product_code(self.flat_bar, 'EN8D', Decimal('1.2'))
        self.assertTrue(created)
        self.assertEqual(code.item_code, 'FBB00100120')
        self.assertEqual((code.category, code.grade, code.size), (self.flat_bar, 'EN8D', Decimal('1.2')))

    def test_the_type_letters_come_from_the_type(self):
        code, _ = get_or_create_product_code(self.round_bar, 'EN8D', Decimal('12'))
        self.assertEqual(code.item_code, 'RBB00101200')

    def test_grades_are_numbered_in_the_order_they_are_first_used_and_the_number_is_kept(self):
        get_or_create_product_code(self.flat_bar, 'EN8D', Decimal('1.2'))
        second, _ = get_or_create_product_code(self.flat_bar, 'SS304', Decimal('1.2'))
        self.assertEqual(second.item_code, 'FBB00200120')
        again, created = get_or_create_product_code(self.flat_bar, 'EN8D', Decimal('2'))
        self.assertTrue(created)
        self.assertEqual(again.item_code, 'FBB00100200')   # EN8D is still 001
        self.assertEqual(GradeOption.objects.get(name='EN8D').number, 1)
        self.assertEqual(GradeOption.objects.get(name='SS304').number, 2)

    def test_the_grade_is_matched_without_regard_to_case_and_keeps_its_spelling(self):
        get_or_create_product_code(self.flat_bar, 'EN8D', Decimal('1.2'))
        code, created = get_or_create_product_code(self.flat_bar, ' en8d ', Decimal('1.5'))
        self.assertTrue(created)
        self.assertEqual((code.item_code, code.grade), ('FBB00100150', 'EN8D'))
        self.assertEqual(GradeOption.objects.count(), 1)

    def test_an_existing_grade_without_a_number_is_numbered_when_first_used(self):
        GradeOption.objects.create(name='EN9')   # e.g. added from a gate entry, never used in a code
        code, _ = get_or_create_product_code(self.flat_bar, 'EN9', Decimal('3'))
        self.assertEqual(code.item_code, 'FBB00100300')

    def test_an_existing_code_is_reused_not_duplicated(self):
        first, created_first = get_or_create_product_code(self.flat_bar, 'EN8D', Decimal('1.2'))
        second, created_second = get_or_create_product_code(self.flat_bar, 'en8d', Decimal('1.200'))
        self.assertEqual((created_first, created_second), (True, False))
        self.assertEqual(first, second)
        self.assertEqual(ProductType.objects.count(), 1)

    def test_the_same_grade_and_size_in_another_type_is_a_different_code(self):
        flat, _ = get_or_create_product_code(self.flat_bar, 'EN8D', Decimal('12'))
        rnd, _ = get_or_create_product_code(self.round_bar, 'EN8D', Decimal('12'))
        self.assertNotEqual(flat.item_code, rnd.item_code)
        self.assertEqual((flat.item_code[:3], rnd.item_code[:3]), ('FBB', 'RBB'))

    def test_a_hand_made_code_with_the_same_text_is_not_overwritten(self):
        ProductType.objects.create(item_code='FBB00100120', grade='OTHER', size='9.000')   # no category
        code, created = get_or_create_product_code(self.flat_bar, 'EN8D', Decimal('1.2'))
        self.assertTrue(created)
        self.assertEqual(code.item_code, 'FBB00100120-2')

    def test_no_code_is_made_when_the_combination_cant_be_written(self):
        no_letters = ProductCategory.objects.create(name='Brand New Type')   # no 3-letter code yet
        cases = {
            'size finer than 0.01 mm': (self.flat_bar, 'EN8D', Decimal('1.205')),
            'size too big': (self.flat_bar, 'EN8D', Decimal('1000')),
            'size zero': (self.flat_bar, 'EN8D', Decimal('0')),
            'grade too long': (self.flat_bar, 'G' * 21, Decimal('2')),
            'type without letters': (no_letters, 'EN8D', Decimal('2')),
        }
        for label, (category, grade, size) in cases.items():
            with self.subTest(case=label):
                code, created = get_or_create_product_code(category, grade, size)
                self.assertEqual((code, created), (None, False))
                self.assertTrue(describe_product_code(category, grade, size)['reason'])
        self.assertEqual(ProductType.objects.count(), 0)
        self.assertEqual(GradeOption.objects.count(), 0)   # nothing half-created

    def test_an_incomplete_combination_creates_nothing(self):
        for category, grade, size in ((None, 'EN8D', Decimal('1')), (self.flat_bar, '', Decimal('1')),
                                      (self.flat_bar, 'EN8D', None)):
            self.assertEqual(get_or_create_product_code(category, grade, size), (None, False))
        self.assertEqual(ProductType.objects.count(), 0)


class DescribeProductCodeTests(TestCase):
    def setUp(self):
        self.flat_bar = ProductCategory.objects.get(name='Flat Bright Bar')

    def test_a_new_combination_previews_the_code_without_creating_anything(self):
        result = describe_product_code(self.flat_bar, 'EN8D', Decimal('1.2'))
        self.assertEqual(result, {'exists': False, 'item_code': 'FBB00100120', 'reason': ''})
        self.assertEqual((ProductType.objects.count(), GradeOption.objects.count()), (0, 0))

    def test_a_new_grade_previews_the_next_free_number(self):
        get_or_create_product_code(self.flat_bar, 'EN8D', Decimal('1.2'))
        self.assertEqual(describe_product_code(self.flat_bar, 'SS304', Decimal('1.2'))['item_code'], 'FBB00200120')

    def test_an_existing_code_is_reported_as_existing(self):
        get_or_create_product_code(self.flat_bar, 'EN8D', Decimal('1.2'))
        self.assertEqual(describe_product_code(self.flat_bar, 'en8d', Decimal('1.2')),
                         {'exists': True, 'item_code': 'FBB00100120', 'reason': ''})

    def test_the_seeded_types_all_have_distinct_three_letter_codes(self):
        codes = list(ProductCategory.objects.values_list('code', flat=True))
        self.assertEqual(len(codes), 14)
        self.assertEqual(len(set(codes)), 14)
        self.assertTrue(all(c and len(c) == 3 and c.isalpha() and c.isupper() for c in codes))
        self.assertEqual(ProductCategory.objects.get(name='Flat Bright Bar').code, 'FBB')


class ProductCodeLookupEndpointTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user('lookup_staff', password='pw', is_staff=True)
        self.flat_bar = ProductCategory.objects.get(name='Flat Bright Bar')
        self.url = reverse('product_code_lookup')

    def _get(self, **params):
        return self.client.get(self.url, params)

    def test_staff_get_the_code_that_would_be_created(self):
        self.client.force_login(self.staff)
        self.assertEqual(self._get(category=self.flat_bar.pk, grade='EN8D', size='1.2').json(),
                         {'exists': False, 'item_code': 'FBB00100120', 'reason': ''})

    def test_staff_get_an_existing_code(self):
        get_or_create_product_code(self.flat_bar, 'EN8D', Decimal('1.2'))
        self.client.force_login(self.staff)
        self.assertEqual(self._get(category=self.flat_bar.pk, grade='EN8D', size='1.2').json()['exists'], True)

    def test_the_reason_is_returned_when_no_code_can_be_made(self):
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


class QuoteSendCreatesCodesTests(TestCase):
    def setUp(self):
        from ..models import Customer
        self.staff = User.objects.create_user('send_staff', password='pw', is_staff=True)
        self.client.force_login(self.staff)
        self.customer = Customer.objects.create(name='Code Co', email='c@example.com')
        self.flat_bar = ProductCategory.objects.get(name='Flat Bright Bar')

    def _post(self, action='send', **item):
        data = quotation_item_post_data(**{f'item-0-{k}': v for k, v in item.items()}, action=action)
        with patch('materials.views.quotations._dispatch_quote_email'):
            return self.client.post(f"{reverse('quotation_form')}?customer={self.customer.pk}", data, follow=True)

    def test_sending_creates_the_code_and_attaches_it_to_the_line(self):
        response = self._post(category=str(self.flat_bar.pk), grade='EN8D', size='1.2')
        line = QuotationLineItem.objects.get()
        self.assertEqual(line.product_type.item_code, 'FBB00100120')
        self.assertEqual(ProductType.objects.count(), 1)
        messages = [str(m) for m in get_messages(response.wsgi_request)] + [str(m) for m in response.context['messages']]
        self.assertTrue(any('New product code FBB00100120 created' in m for m in messages))

    def test_a_second_quote_for_the_same_spec_reuses_the_code(self):
        self._post(category=str(self.flat_bar.pk), grade='EN8D', size='1.2')
        self._post(category=str(self.flat_bar.pk), grade='en8d', size='1.200')
        self.assertEqual(ProductType.objects.count(), 1)
        self.assertEqual(QuotationLineItem.objects.filter(product_type__isnull=False).count(), 2)

    def test_saving_a_draft_does_not_create_a_code(self):
        self._post(action='save_draft', category=str(self.flat_bar.pk), grade='EN8D', size='1.2')
        self.assertEqual(ProductType.objects.count(), 0)
        self.assertEqual(GradeOption.objects.count(), 0)
        self.assertIsNone(QuotationLineItem.objects.get().product_type)

    def test_sending_a_saved_draft_creates_the_code_then(self):
        self._post(action='save_draft', category=str(self.flat_bar.pk), grade='EN8D', size='1.2')
        draft = Quotation.objects.get(status='draft')
        data = quotation_item_post_data(**{'item-0-category': str(self.flat_bar.pk), 'item-0-grade': 'EN8D',
                                           'item-0-size': '1.2', 'action': 'send'})
        with patch('materials.views.quotations._dispatch_quote_email'):
            self.client.post(reverse('quotation_edit', kwargs={'pk': draft.pk}), data)
        self.assertEqual(QuotationLineItem.objects.get().product_type.item_code, 'FBB00100120')

    def test_a_code_picked_by_hand_is_not_replaced(self):
        picked = ProductType.objects.create(item_code='HAND-1', category=self.flat_bar, grade='SS304', size='5.000')
        self._post(category=str(self.flat_bar.pk), grade='EN8D', size='1.2', product_type=str(picked.pk))
        self.assertEqual(QuotationLineItem.objects.get().product_type, picked)
        self.assertEqual(ProductType.objects.count(), 1)

    def test_an_item_without_a_type_gets_no_new_code(self):
        self._post(grade='EN8D', size='1.2')
        self.assertEqual(ProductType.objects.count(), 0)
        self.assertIsNone(QuotationLineItem.objects.get().product_type)

    def test_a_size_that_cant_be_coded_still_sends_with_a_warning(self):
        response = self._post(category=str(self.flat_bar.pk), grade='EN8D', size='1.205')
        self.assertEqual(Quotation.objects.filter(status='sent').count(), 1)   # the quote itself goes out
        self.assertEqual(ProductType.objects.count(), 0)
        messages = [str(m) for m in response.context['messages']]
        self.assertTrue(any('No product code' in m and '2 decimals' in m for m in messages))

    def test_the_page_carries_the_preview_url_and_hint_style(self):
        html = self.client.get(reverse('quotation_form')).content.decode()
        self.assertIn(reverse('product_code_lookup'), html)
        self.assertIn('code-hint', html)
