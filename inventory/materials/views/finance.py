"""The finance team's page: the supplier's bill for every arrival (a coil gate entry or a non-coil entry)."""

import mimetypes

from django.contrib import messages
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from ..decorators import finance_required
from ..forms import BillUploadForm
from ..models import GateEntry, NonCoilEntry

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
    other_waiting, other_billed = _waiting_first(NonCoilEntry.objects.all(), '-arrived_at')
    return render(request, 'materials/finance_dashboard.html', {
        'coil_entries': coil_waiting + coil_billed,
        'coil_waiting_count': len(coil_waiting),
        'other_entries': other_waiting + other_billed,
        'other_waiting_count': len(other_waiting),
    })


def _save_bill(request, entry):
    """Store the uploaded file as the entry's bill (uploading again replaces it), then back to the list."""
    if request.method != 'POST':
        return redirect('finance')
    form = BillUploadForm(request.POST, request.FILES)
    if form.is_valid():
        replaced = entry.has_bill()
        entry.bill = form.cleaned_data['bill']
        entry.bill_uploaded_at = timezone.now()
        entry.save(update_fields=['bill', 'bill_uploaded_at'])
        messages.success(request, "Bill replaced." if replaced else "Bill uploaded.")
    else:
        messages.error(request, form.errors['bill'][0] if 'bill' in form.errors else "Please choose a file.")
    return redirect('finance')


@finance_required
def finance_upload_coil_bill(request, pk):
    return _save_bill(request, get_object_or_404(GateEntry, pk=pk))


@finance_required
def finance_upload_other_bill(request, pk):
    return _save_bill(request, get_object_or_404(NonCoilEntry, pk=pk))


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
