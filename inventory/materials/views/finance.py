"""The finance team's page: the supplier's bill for every arrival (a coil gate entry or a non-coil entry)."""

import mimetypes

from django.contrib import messages
from django.db import transaction
from django.db.models import Count
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from ..decorators import finance_required
from ..forms import BillLineForm, BillUploadForm
from ..models import GateEntry, NonCoilBillLine, NonCoilEntry

# Entries that already have a bill stay listed (newest first, after the ones still waiting), but only the
# latest few so the page stays short; the admin shows everything.
BILLED_ROWS_SHOWN = 50


def _waiting_first(queryset, order_by):
    """Entries still without a bill (all of them) followed by the latest billed ones."""
    waiting = list(queryset.filter(bill='').order_by(order_by))
    billed = list(queryset.exclude(bill='').order_by(order_by)[:BILLED_ROWS_SHOWN])
    return waiting, billed


@finance_required
def finance_dashboard(request):
    coil_waiting, coil_billed = _waiting_first(GateEntry.objects.all(), '-created_at')
    other_waiting, other_billed = _waiting_first(NonCoilEntry.objects.annotate(product_count=Count('lines')), '-arrived_at')
    return render(request, 'materials/finance_dashboard.html', {
        'coil_entries': coil_waiting + coil_billed,
        'coil_waiting_count': len(coil_waiting),
        'other_entries': other_waiting + other_billed,
        'other_waiting_count': len(other_waiting),
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
    return _save_bill(request, get_object_or_404(GateEntry, pk=pk))


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
