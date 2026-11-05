"""The finance team's page: the supplier's bill for every arrival (a coil gate entry or a non-coil entry)."""

import mimetypes

from django.contrib import messages
from django.db import transaction
from django.db.models import Count, Q
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.dateparse import parse_date

from ..decorators import finance_required
from decimal import Decimal, InvalidOperation

from ..forms import BillLineForm, BillUploadForm, ChargeForm
from ..models import GateEntry, GateEntryCharge, NonCoilBillLine, NonCoilEntry

# Entries that already have a bill stay listed (newest first, after the ones still waiting), but only the
# latest few so the page stays short; the admin shows everything.
BILLED_ROWS_SHOWN = 50


def _waiting_first(queryset, order_by, limit_billed=True):
    """Entries still without a bill (all of them) followed by the latest billed ones (all of them when filtered)."""
    waiting = list(queryset.filter(bill='').order_by(order_by))
    billed = queryset.exclude(bill='').order_by(order_by)
    return waiting, list(billed[:BILLED_ROWS_SHOWN] if limit_billed else billed)


def _date(value):
    try:
        return parse_date(value)
    except ValueError:  # well-formed but impossible, e.g. 2026-02-31
        return None


def _rate(value):
    try:
        return Decimal(value)
    except InvalidOperation:
        return None


def _filtered(model, fields, date_field, company_field, params, extra=Q()):
    """The search box matches every word (in any of `fields`); company, dates and `extra` narrow it further."""
    cond = Q(extra)
    if params['company']:
        cond &= Q(**{company_field + '__icontains': params['company']})
    for word in params['q'].split():
        cond &= Q(*[Q(**{f + '__icontains': word}) for f in fields], _connector=Q.OR)
    if params['start']:
        cond &= Q(**{date_field + '__date__gte': params['start']})
    if params['end']:
        cond &= Q(**{date_field + '__date__lte': params['end']})
    # pk__in keeps one row per entry however many lots or products matched
    return model.objects.filter(pk__in=model.objects.filter(cond).values('pk'))


@finance_required
def finance_dashboard(request):
    g = request.GET
    params = {'q': g.get('q', '').strip(), 'company': g.get('company', '').strip(),
              'start': _date(g.get('from', '')), 'end': _date(g.get('to', '')),
              'rate_min': _rate(g.get('rate_min', '')), 'rate_max': _rate(g.get('rate_max', ''))}
    filtering = any(v is not None and v != '' for v in params.values())
    # the rate is on a coil lot (one lot must satisfy both limits); other arrivals have none
    rate = Q()
    if params['rate_min'] is not None:
        rate &= Q(lots__rate__gte=params['rate_min'])
    if params['rate_max'] is not None:
        rate &= Q(lots__rate__lte=params['rate_max'])
    coil_qs = _filtered(GateEntry, ['vendor', 'invoice_no', 'vehicle_no', 'lots__company', 'lots__grade',
                                    'charges__description'], 'created_at', 'lots__company', params, rate)
    other_qs = _filtered(NonCoilEntry, ['category', 'company', 'lines__description'], 'arrived_at', 'company', params)
    if rate:
        other_qs = other_qs.none()
    other_qs = other_qs.annotate(product_count=Count('lines'))
    coil_waiting, coil_billed = _waiting_first(coil_qs, '-created_at', not filtering)
    other_waiting, other_billed = _waiting_first(other_qs, '-arrived_at', not filtering)
    return render(request, 'materials/finance_dashboard.html', {
        'coil_entries': coil_waiting + coil_billed,
        'coil_waiting_count': len(coil_waiting),
        'other_entries': other_waiting + other_billed,
        'other_waiting_count': len(other_waiting),
        'q': params['q'], 'company': params['company'], 'rate_min': g.get('rate_min', ''),
        'rate_max': g.get('rate_max', ''), 'start': g.get('from', ''), 'end': g.get('to', ''),
        'filtering': filtering,
    })


def _save_bill(request, entry, after_upload='finance'):
    """Store the uploaded file as the entry's bill (uploading again replaces it), then on to `after_upload` (the list,
    or for a non-coil entry the page where the products on the bill are entered); a refused file goes back to the list."""
    if request.method != 'POST':
        return redirect('finance')
    form = BillUploadForm(request.POST, request.FILES)
    if form.is_valid():
        replaced = entry.has_bill()
        entry.bill = form.cleaned_data['bill']
        entry.bill_uploaded_at = timezone.now()
        entry.save(update_fields=['bill', 'bill_uploaded_at'])
        messages.success(request, "Bill replaced." if replaced else "Bill uploaded.")
        if after_upload != 'finance':
            return redirect(after_upload, pk=entry.pk)
    else:
        messages.error(request, form.errors['bill'][0] if 'bill' in form.errors else "Please choose a file.")
    return redirect('finance')


@finance_required
def finance_upload_coil_bill(request, pk):
    return _save_bill(request, get_object_or_404(GateEntry, pk=pk), after_upload='finance_coil_bill_details')


@finance_required
def finance_upload_other_bill(request, pk):
    return _save_bill(request, get_object_or_404(NonCoilEntry, pk=pk), after_upload='finance_bill_details')


def _show_bill(entry):
    """Serves the bill file inline (every allowed type is a PDF or an image, which a browser only displays)."""
    if not entry.has_bill():
        raise Http404("No bill uploaded yet.")
    content_type = mimetypes.guess_type(entry.bill.name)[0] or 'application/octet-stream'
    response = FileResponse(entry.bill.open('rb'), content_type=content_type)
    response['Content-Disposition'] = 'inline'
    return response


# Bills are money documents and /media/ is staff-only, so the finance team opens them through these.
@finance_required
def finance_coil_bill(request, pk):
    return _show_bill(get_object_or_404(GateEntry, pk=pk))


@finance_required
def finance_other_bill(request, pk):
    return _show_bill(get_object_or_404(NonCoilEntry, pk=pk))


MAX_BILL_LINES = 200


def _row_values(line_form):
    return {'description': line_form['description'].value() or '', 'quantity': line_form['quantity'].value() or '',
            'unit': line_form['unit'].value() or '', 'errors': [e for errs in line_form.errors.values() for e in errs]}


def _parse_bill_lines(post):
    """The product rows posted from the details page (rows are added and removed in the browser, so they arrive as
    line-<i>-description/quantity/unit with a line-TOTAL_FORMS count). A row left entirely blank is ignored; a row
    with something filled in must be complete. Returns (valid forms, rows to show again, whether anything is wrong)."""
    try:
        total = min(max(int(post.get('line-TOTAL_FORMS', 0)), 0), MAX_BILL_LINES)
    except ValueError:
        total = 0
    valid, shown, wrong = [], [], False
    for index in range(total):
        line_form = BillLineForm(post, prefix=f'line-{index}')
        if not line_form.has_changed():
            continue
        if line_form.is_valid():
            valid.append(line_form)
        else:
            wrong = True
        shown.append(_row_values(line_form))
    return valid, shown, wrong


@finance_required
def finance_bill_details(request, pk):
    """The products on a non-coil entry's bill, entered by hand from the bill: description, quantity, unit. Reached
    right after the bill is uploaded, and again from the list's Details button. Saving replaces the whole list."""
    entry = get_object_or_404(NonCoilEntry, pk=pk)
    if not entry.has_bill():
        messages.error(request, "Upload the bill first; its products are entered from it.")
        return redirect('finance')
    if request.method == 'POST':
        valid, shown, wrong = _parse_bill_lines(request.POST)
        if not wrong:
            with transaction.atomic():
                entry.lines.all().delete()
                NonCoilBillLine.objects.bulk_create([
                    NonCoilBillLine(entry=entry, position=position, **line_form.cleaned_data)
                    for position, line_form in enumerate(valid, start=1)])
            messages.success(request, f"Saved {len(valid)} product{'' if len(valid) == 1 else 's'} for {entry}.")
            return redirect('finance')
        rows, error = shown, "Please complete or remove the rows marked below."
    else:
        rows = [{'description': line.description, 'quantity': format(line.quantity.normalize(), 'f'),
                 'unit': line.unit, 'errors': []} for line in entry.lines.all()]
        error = None
    return render(request, 'materials/finance_bill_details.html', {'entry': entry, 'rows': rows, 'error': error})


def _parse_charges(post):
    """The 'other amounts' rows (charge-<i>-description/amount with charge-TOTAL_FORMS), like the product rows on a
    non-coil bill: blank rows are ignored, a half-filled one is an error. Returns (valid forms, rows to show, wrong)."""
    try:
        total = min(max(int(post.get('charge-TOTAL_FORMS', 0)), 0), MAX_BILL_LINES)
    except ValueError:
        total = 0
    valid, shown, wrong = [], [], False
    for index in range(total):
        form = ChargeForm(post, prefix=f'charge-{index}')
        if not form.has_changed():
            continue
        if form.is_valid():
            valid.append(form)
        else:
            wrong = True
        shown.append({'description': form['description'].value() or '', 'amount': form['amount'].value() or '',
                      'errors': [e for errs in form.errors.values() for e in errs]})
    return valid, shown, wrong


def _parse_rates(post, lots):
    """Each lot's rate per kg from rate-<lot id>; blank clears it. Returns ({lot id: Decimal|None}, {lot id: error})."""
    rates, errors = {}, {}
    for lot in lots:
        raw = (post.get(f'rate-{lot.pk}') or '').strip()
        if not raw:
            rates[lot.pk] = None
            continue
        try:
            value = Decimal(raw)
            if value <= 0 or value.as_tuple().exponent < -2:
                raise InvalidOperation
        except InvalidOperation:
            errors[lot.pk] = "Enter a rate above 0 with at most 2 decimals."
            continue
        rates[lot.pk] = value
    return rates, errors


@finance_required
def finance_coil_bill_details(request, pk):
    """What a coil delivery's bill charges: the delivery's lots are filled in automatically, each gets a rate per kg,
    and any other amounts on the bill (freight...) are added below. Reached right after the bill is uploaded and
    from the list's Details button. Saving replaces the rates and the list of other amounts."""
    entry = get_object_or_404(GateEntry, pk=pk)
    if not entry.has_bill():
        messages.error(request, "Upload the bill first; its rates are entered from it.")
        return redirect('finance')
    lots = list(entry.lots.order_by('pk'))
    error, rate_errors = None, {}
    if request.method == 'POST':
        rates, rate_errors = _parse_rates(request.POST, lots)
        valid, shown, wrong = _parse_charges(request.POST)
        if not rate_errors and not wrong:
            with transaction.atomic():
                for lot in lots:
                    lot.rate = rates[lot.pk]
                    lot.save(update_fields=['rate'])
                entry.charges.all().delete()
                GateEntryCharge.objects.bulk_create([
                    GateEntryCharge(gate_entry=entry, position=position, **form.cleaned_data)
                    for position, form in enumerate(valid, start=1)])
            messages.success(request, f"Saved the bill details for gate entry #{entry.pk}.")
            return redirect('finance')
        error = "Please fix the rows marked below."
        for lot in lots:   # show what was typed, not what is stored
            lot.rate_shown = request.POST.get(f'rate-{lot.pk}', '')
        charge_rows = shown
    else:
        for lot in lots:
            lot.rate_shown = format(lot.rate.normalize(), 'f') if lot.rate else ''
        charge_rows = [{'description': c.description, 'amount': format(c.amount, 'f'), 'errors': []} for c in entry.charges.all()]
    for lot in lots:
        lot.error = rate_errors.get(lot.pk)
    return render(request, 'materials/finance_coil_bill_details.html', {
        'entry': entry, 'lots': lots, 'charge_rows': charge_rows, 'error': error,
    })
