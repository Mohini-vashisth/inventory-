"""Gate entry, lot and coil-registration forms and views."""

from decimal import Decimal
from django.conf import settings
from django.test import TestCase
from django.urls import reverse
from unittest.mock import patch

from ..forms import MaterialForm
from ..models import GateEntry, GateEntryLot, GradeOption, Material, SizeOption


class MaterialFormValidationTests(TestCase):
    """Grade/size must come from the admin-curated lists, even on a raw POST."""

    def setUp(self):
        GradeOption.objects.get_or_create(name='EN8D')
        SizeOption.objects.get_or_create(value='1.200')

    def base_data(self, **overrides):
        data = {
            'date': '2026-07-06', 'grade': 'EN8D', 'size': '1.200',
            'company': 'Tata Steel', 'vendor': 'ABC Traders',
            'quantity': '500.000', 'heat_no': 'H001',
        }
        data.update(overrides)
        return data

    def test_grade_not_in_gradeoption_rejected(self):
        form = MaterialForm(self.base_data(grade='MADE-UP'))
        self.assertFalse(form.is_valid())
        self.assertIn('grade', form.errors)

    def test_size_not_in_sizeoption_rejected(self):
        form = MaterialForm(self.base_data(size='9.999'))
        self.assertFalse(form.is_valid())
        self.assertIn('size', form.errors)

    def test_known_grade_and_size_accepted(self):
        form = MaterialForm(self.base_data())
        self.assertTrue(form.is_valid(), form.errors)

    def test_negative_quantity_rejected(self):
        form = MaterialForm(self.base_data(quantity='-500.000'))
        self.assertFalse(form.is_valid())
        self.assertIn('quantity', form.errors)

    def test_zero_quantity_rejected(self):
        form = MaterialForm(self.base_data(quantity='0'))
        self.assertFalse(form.is_valid())
        self.assertIn('quantity', form.errors)


class GateEntryFormViewErrorDisplayTests(TestCase):
    """A rejected gate entry submission must show why, and not force the
    employee to retype everything."""

    def setUp(self):
        GradeOption.objects.get_or_create(name='EN8D')
        SizeOption.objects.get_or_create(value='1.200')
        self.client.post(reverse('employee_login'), {'pin': settings.EMPLOYEE_PIN})

    def test_non_numeric_total_weight_shows_error_and_repopulates_fields(self):
        response = self.client.post(reverse('gate_entry_form'), {
            'date': '2026-07-06', 'vendor': 'ABC Traders',
            'vehicle_no': 'AP16TA1234', 'total_weight': 'not-a-number',
            'lot-TOTAL_FORMS': '1', 'lot-INITIAL_FORMS': '0',
            'lot-MIN_NUM_FORMS': '0', 'lot-MAX_NUM_FORMS': '1000',
            'lot-0-company': 'Tata Steel', 'lot-0-grade': 'EN8D',
            'lot-0-size': '1.200', 'lot-0-no_of_coils': '3',
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Enter a number')
        self.assertContains(response, 'ABC Traders')
        self.assertEqual(GateEntry.objects.count(), 0)

    def test_negative_total_weight_rejected(self):
        response = self.client.post(reverse('gate_entry_form'), {
            'date': '2026-07-06', 'vendor': 'ABC Traders',
            'vehicle_no': 'AP16TA1234', 'total_weight': '-2500.000',
            'lot-TOTAL_FORMS': '1', 'lot-INITIAL_FORMS': '0',
            'lot-MIN_NUM_FORMS': '0', 'lot-MAX_NUM_FORMS': '1000',
            'lot-0-company': 'Tata Steel', 'lot-0-grade': 'EN8D',
            'lot-0-size': '1.200', 'lot-0-no_of_coils': '3',
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "greater than zero")
        self.assertEqual(GateEntry.objects.count(), 0)


class GateEntryLotFormViewErrorDisplayTests(TestCase):
    """Grade/size validation against GradeOption/SizeOption lives here, not
    in material_form — a lot's grade/size is locked in once created and
    inherited by every coil registered against it."""

    def setUp(self):
        GradeOption.objects.get_or_create(name='EN8D')
        SizeOption.objects.get_or_create(value='1.200')
        self.client.post(reverse('employee_login'), {'pin': settings.EMPLOYEE_PIN})
        self.gate_entry = GateEntry.objects.create(
            vendor='ABC Traders', vehicle_no='AP16TA1234', total_weight=2500,
        )

    def test_invalid_grade_shows_error_and_repopulates_fields(self):
        response = self.client.post(reverse('gate_entry_lot_form', args=[self.gate_entry.pk]), {
            'grade': 'MADE-UP', 'size': '1.200', 'no_of_coils': '5',
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Select a grade from the list.')
        self.assertEqual(GateEntryLot.objects.count(), 0)


class MaterialFormViewErrorDisplayTests(TestCase):
    """A rejected coil submission must show why, and not force the employee
    to retype everything."""

    def setUp(self):
        GradeOption.objects.get_or_create(name='EN8D')
        SizeOption.objects.get_or_create(value='1.200')
        self.client.post(reverse('employee_login'), {'pin': settings.EMPLOYEE_PIN})
        gate_entry = GateEntry.objects.create(
            vendor='ABC Traders', vehicle_no='AP16TA1234', total_weight=2500,
        )
        self.lot = GateEntryLot.objects.create(gate_entry=gate_entry, company='Tata Steel', grade='EN8D', size='1.200', no_of_coils=5)

    def test_non_numeric_quantity_shows_error_and_repopulates_fields(self):
        response = self.client.post(reverse('material_form', args=[self.lot.pk]), {
            'date': '2026-07-06', 'quantity': 'not-a-number', 'heat_no': 'H001',
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Enter a number')
        self.assertContains(response, 'H001')
        self.assertEqual(Material.objects.count(), 0)


class MaterialFieldAutocompleteTests(TestCase):
    def setUp(self):
        self.client.post(reverse('employee_login'), {'pin': settings.EMPLOYEE_PIN})
        Material.objects.create(company='Tata Steel', vendor='ABC Traders', quantity=500)
        Material.objects.create(company='Tata Sons', vendor='XYZ Traders', quantity=500)

    def test_matches_company_by_partial_name(self):
        response = self.client.get(reverse('material_field_autocomplete'), {'field': 'company', 'q': 'tata'})
        self.assertEqual(set(response.json()), {'Tata Steel', 'Tata Sons'})

    def test_matches_vendor_by_partial_name(self):
        response = self.client.get(reverse('material_field_autocomplete'), {'field': 'vendor', 'q': 'abc'})
        self.assertEqual(response.json(), ['ABC Traders'])

    def test_unknown_field_returns_empty(self):
        response = self.client.get(reverse('material_field_autocomplete'), {'field': 'heat_no', 'q': 'H'})
        self.assertEqual(response.json(), [])

    def test_empty_query_returns_empty(self):
        response = self.client.get(reverse('material_field_autocomplete'), {'field': 'company'})
        self.assertEqual(response.json(), [])

    def test_requires_employee_login(self):
        self.client.post(reverse('employee_logout'))
        url = reverse('material_field_autocomplete')
        response = self.client.get(url, {'field': 'company', 'q': 'tata'})
        self.assertRedirects(response, f"{reverse('employee_login')}?next={url}")


class GateEntryFormTests(TestCase):
    """gate_entry_form creates the GateEntry and all of its lots together,
    in one submission — the single-page form with a repeatable, collapsible
    lot section described by the user."""

    def setUp(self):
        GradeOption.objects.get_or_create(name='EN8D')
        SizeOption.objects.get_or_create(value='1.200')
        self.client.post(reverse('employee_login'), {'pin': settings.EMPLOYEE_PIN})

    def _post_data(self, lots, **overrides):
        data = {
            'date': '2026-07-06', 'vendor': 'ABC Traders',
            'vehicle_no': 'AP16TA1234', 'total_weight': '2500.000',
            'lot-TOTAL_FORMS': str(len(lots)), 'lot-INITIAL_FORMS': '0',
            'lot-MIN_NUM_FORMS': '0', 'lot-MAX_NUM_FORMS': '1000',
        }
        for i, lot in enumerate(lots):
            for key, value in lot.items():
                data[f'lot-{i}-{key}'] = value
        data.update(overrides)
        return data

    def test_valid_submission_creates_gate_entry_and_lot_then_redirects_to_detail(self):
        data = self._post_data([
            {'company': 'Tata Steel', 'grade': 'EN8D', 'size': '1.200', 'no_of_coils': '3'},
        ])
        response = self.client.post(reverse('gate_entry_form'), data)
        ge = GateEntry.objects.get()
        self.assertRedirects(response, reverse('gate_entry_detail', args=[ge.pk]))
        self.assertEqual(ge.vendor, 'ABC TRADERS')
        self.assertEqual(ge.total_weight, Decimal('2500.000'))
        lot = GateEntryLot.objects.get()
        self.assertEqual(lot.gate_entry, ge)
        self.assertEqual(lot.company, 'TATA STEEL')
        self.assertEqual(lot.no_of_coils, 3)

    def test_multiple_lots_created_together(self):
        data = self._post_data([
            {'company': 'Tata Steel', 'grade': 'EN8D', 'size': '1.200', 'no_of_coils': '3'},
            {'company': 'JSW', 'grade': 'EN8D', 'size': '1.200', 'no_of_coils': '2'},
        ])
        self.client.post(reverse('gate_entry_form'), data)
        ge = GateEntry.objects.get()
        self.assertEqual(GateEntryLot.objects.filter(gate_entry=ge).count(), 2)
        self.assertEqual(ge.no_of_coils(), 5)
        companies = set(GateEntryLot.objects.filter(gate_entry=ge).values_list('company', flat=True))
        self.assertEqual(companies, {'TATA STEEL', 'JSW'})

    def test_invalid_lot_rolls_back_the_whole_submission(self):
        """All-or-nothing: an invalid second lot must not leave a gate entry
        or a valid first lot behind."""
        data = self._post_data([
            {'company': 'Tata Steel', 'grade': 'EN8D', 'size': '1.200', 'no_of_coils': '3'},
            {'company': 'JSW', 'grade': 'MADE-UP', 'size': '1.200', 'no_of_coils': '2'},
        ])
        response = self.client.post(reverse('gate_entry_form'), data)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(GateEntry.objects.count(), 0)
        self.assertEqual(GateEntryLot.objects.count(), 0)

    def test_requires_employee_login(self):
        self.client.post(reverse('employee_logout'))
        response = self.client.get(reverse('gate_entry_form'))
        self.assertRedirects(response, f"{reverse('employee_login')}?next={reverse('gate_entry_form')}")

    def test_vehicle_no_is_uppercased_even_if_submitted_lowercase(self):
        """The form's own JS already forces uppercase as the employee types —
        this covers anything submitted without it (a direct API call, JS
        disabled, etc.)."""
        data = self._post_data(
            [{'company': 'Tata Steel', 'grade': 'EN8D', 'size': '1.200', 'no_of_coils': '3'}],
            vehicle_no='ap16ta1234',
        )
        self.client.post(reverse('gate_entry_form'), data)
        ge = GateEntry.objects.get()
        self.assertEqual(ge.vehicle_no, 'AP16TA1234')


class GateEntryLotFormTests(TestCase):
    def setUp(self):
        GradeOption.objects.get_or_create(name='EN8D')
        SizeOption.objects.get_or_create(value='1.200')
        self.client.post(reverse('employee_login'), {'pin': settings.EMPLOYEE_PIN})
        self.gate_entry = GateEntry.objects.create(
            vendor='ABC Traders', vehicle_no='AP16TA1234', total_weight=2500,
        )

    def test_valid_submission_creates_lot_and_redirects_to_gate_entry_detail(self):
        response = self.client.post(reverse('gate_entry_lot_form', args=[self.gate_entry.pk]), {
            'grade': 'EN8D', 'size': '1.200', 'no_of_coils': '5',
        })
        lot = GateEntryLot.objects.get()
        self.assertRedirects(response, reverse('gate_entry_detail', args=[self.gate_entry.pk]))
        self.assertEqual(lot.gate_entry, self.gate_entry)
        self.assertEqual(lot.no_of_coils, 5)

    def test_zero_coils_rejected(self):
        response = self.client.post(reverse('gate_entry_lot_form', args=[self.gate_entry.pk]), {
            'grade': 'EN8D', 'size': '1.200', 'no_of_coils': '0',
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(GateEntryLot.objects.count(), 0)

    def test_unknown_gate_entry_404s(self):
        response = self.client.get(reverse('gate_entry_lot_form', args=[99999]))
        self.assertEqual(response.status_code, 404)


class GateEntryDetailTests(TestCase):
    def setUp(self):
        self.client.post(reverse('employee_login'), {'pin': settings.EMPLOYEE_PIN})
        self.gate_entry = GateEntry.objects.create(
            vendor='ABC Traders', vehicle_no='AP16TA1234', total_weight=1000,
        )

    def test_lists_lots_added_so_far(self):
        lot = GateEntryLot.objects.create(gate_entry=self.gate_entry, company='Tata Steel', grade='EN8D', size='1.200', no_of_coils=3)
        response = self.client.get(reverse('gate_entry_detail', args=[self.gate_entry.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual([row.pk for row in response.context['lots']], [lot.pk])
        self.assertContains(response, 'EN8D')

    def test_no_lots_shows_empty_state(self):
        response = self.client.get(reverse('gate_entry_detail', args=[self.gate_entry.pk]))
        self.assertContains(response, "No lots added yet")

    def test_invoice_no_shown_when_present(self):
        self.gate_entry.invoice_no = 'INV-0042'
        self.gate_entry.save()
        response = self.client.get(reverse('gate_entry_detail', args=[self.gate_entry.pk]))
        self.assertContains(response, 'INV-0042')

    def test_edit_link_shown(self):
        response = self.client.get(reverse('gate_entry_detail', args=[self.gate_entry.pk]))
        self.assertContains(response, reverse('gate_entry_edit', args=[self.gate_entry.pk]))


class GateEntryEditTests(TestCase):
    """Fixing a mistake in a gate entry's top-level details after it's
    already been saved — allowed regardless of whether coils have already
    been registered against its lots, since these fields are just
    paper/reference details."""

    def setUp(self):
        self.client.post(reverse('employee_login'), {'pin': settings.EMPLOYEE_PIN})
        self.gate_entry = GateEntry.objects.create(
            date='2026-07-01', vendor='ABC Traders', vehicle_no='AP16TA1234',
            invoice_no='INV-0001', total_weight=1000,
        )

    def test_requires_employee_login(self):
        self.client.post(reverse('employee_logout'))
        response = self.client.get(reverse('gate_entry_edit', args=[self.gate_entry.pk]))
        self.assertRedirects(
            response,
            f"{reverse('employee_login')}?next={reverse('gate_entry_edit', args=[self.gate_entry.pk])}",
        )

    def test_get_prefills_existing_values(self):
        response = self.client.get(reverse('gate_entry_edit', args=[self.gate_entry.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'ABC Traders')
        self.assertContains(response, 'AP16TA1234')
        self.assertContains(response, 'INV-0001')

    def test_valid_edit_updates_and_redirects_to_detail(self):
        response = self.client.post(
            reverse('gate_entry_edit', args=[self.gate_entry.pk]),
            {
                'date': '2026-07-02', 'vendor': 'XYZ Traders', 'vehicle_no': 'ka1a1234',
                'invoice_no': 'INV-0002', 'total_weight': '1500.000',
            },
        )
        self.assertRedirects(response, reverse('gate_entry_detail', args=[self.gate_entry.pk]))
        self.gate_entry.refresh_from_db()
        self.assertEqual(self.gate_entry.vendor, 'XYZ TRADERS')
        self.assertEqual(self.gate_entry.vehicle_no, 'KA1A1234')  # server-side uppercase safety net
        self.assertEqual(self.gate_entry.invoice_no, 'INV-0002')
        self.assertEqual(self.gate_entry.total_weight, Decimal('1500.000'))

    def test_edit_allowed_even_after_coils_registered(self):
        lot = GateEntryLot.objects.create(
            gate_entry=self.gate_entry, company='Tata Steel', grade='EN8D', size='1.200', no_of_coils=1,
        )
        Material.objects.create(lot=lot, quantity=500)
        response = self.client.post(
            reverse('gate_entry_edit', args=[self.gate_entry.pk]),
            {
                'date': '2026-07-02', 'vendor': 'XYZ Traders', 'vehicle_no': 'AP16TA1234',
                'invoice_no': 'INV-0002', 'total_weight': '1500.000',
            },
        )
        self.assertRedirects(response, reverse('gate_entry_detail', args=[self.gate_entry.pk]))
        self.gate_entry.refresh_from_db()
        self.assertEqual(self.gate_entry.vendor, 'XYZ TRADERS')

    def test_invalid_edit_shows_error_and_does_not_save(self):
        response = self.client.post(
            reverse('gate_entry_edit', args=[self.gate_entry.pk]),
            {'date': '2026-07-02', 'vendor': 'XYZ Traders', 'total_weight': 'not-a-number'},
        )
        self.assertEqual(response.status_code, 200)
        self.gate_entry.refresh_from_db()
        self.assertEqual(self.gate_entry.vendor, 'ABC Traders')  # unchanged


class GateEntryLotDeleteTests(TestCase):
    """Removing a lot added by mistake — only while it has no coils
    registered against it yet."""

    def setUp(self):
        self.client.post(reverse('employee_login'), {'pin': settings.EMPLOYEE_PIN})
        self.gate_entry = GateEntry.objects.create(
            vendor='ABC Traders', vehicle_no='AP16TA1234', total_weight=1000,
        )
        self.lot = GateEntryLot.objects.create(
            gate_entry=self.gate_entry, company='Tata Steel', grade='EN8D', size='1.200', no_of_coils=3,
        )

    def test_empty_lot_is_removed_and_redirects_to_detail(self):
        response = self.client.post(reverse('gate_entry_lot_delete', args=[self.lot.pk]))
        self.assertRedirects(response, reverse('gate_entry_detail', args=[self.gate_entry.pk]))
        self.assertFalse(GateEntryLot.objects.filter(pk=self.lot.pk).exists())

    def test_lot_with_registered_coils_is_not_removed(self):
        Material.objects.create(lot=self.lot, quantity=500)
        self.client.post(reverse('gate_entry_lot_delete', args=[self.lot.pk]))
        self.assertTrue(GateEntryLot.objects.filter(pk=self.lot.pk).exists())

    def test_get_does_not_delete(self):
        self.client.get(reverse('gate_entry_lot_delete', args=[self.lot.pk]))
        self.assertTrue(GateEntryLot.objects.filter(pk=self.lot.pk).exists())

    def test_requires_employee_login(self):
        self.client.post(reverse('employee_logout'))
        url = reverse('gate_entry_lot_delete', args=[self.lot.pk])
        response = self.client.post(url)
        self.assertRedirects(response, f"{reverse('employee_login')}?next={url}")


class SelectGateEntryTests(TestCase):
    def setUp(self):
        self.client.post(reverse('employee_login'), {'pin': settings.EMPLOYEE_PIN})

    def test_only_open_lots_are_listed(self):
        open_ge = GateEntry.objects.create(total_weight=1000, vehicle_no='OPEN1')
        open_lot = GateEntryLot.objects.create(gate_entry=open_ge, grade='EN8D', size='1.200', no_of_coils=2)

        complete_ge = GateEntry.objects.create(total_weight=1000, vehicle_no='DONE1')
        complete_lot = GateEntryLot.objects.create(gate_entry=complete_ge, grade='EN8D', size='1.200', no_of_coils=1)
        Material.objects.create(lot=complete_lot, quantity=1000)

        response = self.client.get(reverse('select_gate_entry'))
        listed_ids = [row['lot'].pk for row in response.context['lots']]
        self.assertEqual(listed_ids, [open_lot.pk])

    def test_no_open_lots_shows_empty_state(self):
        response = self.client.get(reverse('select_gate_entry'))
        self.assertContains(response, "No lot has coils left")


class MaterialFormGateEntryTests(TestCase):
    """New Coil Entry is always scoped to a lot: vendor comes from the gate
    entry, company/grade/size from the lot, all locked; invoice_weight is
    computed from the gate entry; and the lot caps how many coils can be
    registered against it."""

    def setUp(self):
        GradeOption.objects.get_or_create(name='EN8D')
        SizeOption.objects.get_or_create(value='1.200')
        self.client.post(reverse('employee_login'), {'pin': settings.EMPLOYEE_PIN})
        self.gate_entry = GateEntry.objects.create(
            vendor='ABC Traders', vehicle_no='AP16TA1234', total_weight=1000,
        )
        self.lot = GateEntryLot.objects.create(
            gate_entry=self.gate_entry, company='Tata Steel', grade='EN8D', size='1.200', no_of_coils=2,
        )

    def _post_coil(self, lot=None, **overrides):
        lot = lot or self.lot
        data = {'date': '2026-07-06', 'heat_no': 'H001', 'quantity': '480.500'}
        data.update(overrides)
        return self.client.post(reverse('material_form', args=[lot.pk]), data)

    def test_requires_employee_login(self):
        self.client.post(reverse('employee_logout'))
        url = reverse('material_form', args=[self.lot.pk])
        response = self.client.get(url)
        self.assertRedirects(response, f"{reverse('employee_login')}?next={url}")

    def test_unknown_lot_404s(self):
        response = self.client.get(reverse('material_form', args=[99999]))
        self.assertEqual(response.status_code, 404)

    def test_fields_are_locked_even_if_tampered(self):
        """company/vendor/grade/size are never read from the submitted form —
        an attacker (or a stale cached page) posting different values has no effect."""
        self._post_coil(company='SPOOFED', vendor='SPOOFED', grade='SPOOFED', size='9.999')
        coil = Material.objects.get()
        self.assertEqual(coil.company, 'Tata Steel')
        self.assertEqual(coil.vendor, 'ABC Traders')
        self.assertEqual(coil.grade, 'EN8D')
        self.assertEqual(coil.size, Decimal('1.200'))

    def test_archived_at_and_legacy_used_weight_cannot_be_posted(self):
        """These aren't read from trusted sources like company/vendor/grade/
        size (there's no legitimate way to set them from this form at all —
        archived_at only ever comes from the admin's archive action,
        legacy_used_weight only from import_excel) — MaterialForm.Meta must
        exclude both, or a raw/scripted POST could pre-archive a brand-new
        coil or corrupt its weight_used() math with a fake legacy figure."""
        self._post_coil(archived_at='2020-01-01T00:00:00Z', legacy_used_weight='999999')
        coil = Material.objects.get()
        self.assertIsNone(coil.archived_at)
        self.assertEqual(coil.legacy_used_weight, Decimal('0'))

    def test_invoice_weight_computed_from_gate_entry_average(self):
        self._post_coil()
        coil = Material.objects.get()
        self.assertEqual(coil.invoice_weight, Decimal('500.000'))  # 1000 / 2
        self.assertEqual(coil.quantity, Decimal('480.500'))  # the actual measured weight, unaffected

    def test_registering_exactly_no_of_coils_succeeds_then_blocks_further(self):
        self._post_coil(heat_no='H001')
        self._post_coil(heat_no='H002')
        self.assertEqual(Material.objects.filter(lot=self.lot).count(), 2)

        response = self._post_coil(heat_no='H003')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "already been registered")
        self.assertEqual(Material.objects.filter(lot=self.lot).count(), 2)

    def test_complete_lot_shows_banner_on_get(self):
        self._post_coil(heat_no='H001')
        self._post_coil(heat_no='H002')
        response = self.client.get(reverse('material_form', args=[self.lot.pk]))
        self.assertContains(response, "have already been registered")

    def test_concurrent_registration_of_the_last_slot_rolls_back(self):
        """Two requests can both pass the `complete` check against a stale
        read before either has written anything. coils_registered() is
        called again after the coil is inserted — simulate a second,
        already-committed registration showing up between those two calls
        and confirm the write rolls back instead of exceeding no_of_coils."""
        self._post_coil(heat_no='H001')  # 1 of 2 used

        # First call is the pre-check (real value: 1 registered, 1 slot left,
        # not complete) — second is the post-insert re-check, mocked to look
        # as if a second, concurrent registration had already landed too.
        with patch.object(GateEntryLot, 'coils_registered', side_effect=[1, 3]):
            response = self._post_coil(heat_no='H002')

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "reload and check with the office")
        self.assertEqual(Material.objects.filter(lot=self.lot).count(), 1)


class GateEntryUppercaseTests(TestCase):
    """Whatever is typed on the gate entry pages is stored in capitals."""

    def test_vendor_invoice_and_company_are_stored_in_capitals(self):
        from ..forms import GateEntryForm, GateEntryLotForm
        form = GateEntryForm({'date': '2026-07-06', 'vendor': ' abc traders ', 'vehicle_no': 'ap16ta1234',
                              'invoice_no': 'inv-42a', 'total_weight': '2500'})
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual((form.cleaned_data['vendor'], form.cleaned_data['invoice_no'], form.cleaned_data['vehicle_no']),
                         ('ABC TRADERS', 'INV-42A', 'AP16TA1234'))
        GradeOption.objects.create(name='EN8D')
        SizeOption.objects.create(value='1.200')
        lot = GateEntryLotForm({'company': 'tata steel', 'grade': 'EN8D', 'size': '1.2', 'no_of_coils': '3'})
        self.assertTrue(lot.is_valid(), lot.errors)
        self.assertEqual(lot.cleaned_data['company'], 'TATA STEEL')

    def test_the_pages_load_the_uppercase_script(self):
        from django.contrib.auth import get_user_model  # noqa: F401
        session = self.client.session
        session['employee_auth'] = True
        session.save()
        for name in ('gate_entry_form',):
            self.assertContains(self.client.get(reverse(name)), "autocapitalize', 'characters'")


    def test_heat_no_is_stored_in_capitals(self):
        from ..forms import MaterialForm
        GradeOption.objects.get_or_create(name='EN8D')
        SizeOption.objects.get_or_create(value='1.200')
        form = MaterialForm({'grade': 'EN8D', 'size': '1.2', 'vendor': 'ABC', 'company': 'TATA', 'quantity': '500', 'heat_no': ' 21e02404 '})
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data['heat_no'], '21E02404')
