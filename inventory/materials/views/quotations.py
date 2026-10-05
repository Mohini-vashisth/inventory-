"""Official quotations: the Send Quote / Save Draft form, drafts, PDF, and the email."""

from django.utils import timezone
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.core.mail import EmailMessage
from django.conf import settings
from django.urls import reverse
from django.db import transaction
from django.http import HttpResponse, JsonResponse
from decimal import Decimal, InvalidOperation

from ..models import ProductCategory, ProductType, Customer, Query, Quotation, QuotationLineItem
from ..forms import QuotationForm, QuotationLineItemFormSet
from ..pdf import generate_quotation_pdf
from ..product_codes import describe_product_code, get_or_create_product_code
from ..decorators import staff_required
from .common import _first_form_error, _first_formset_error, _match_product_type, _safe_get


def _resolve_quotation_customer(customer_name, query, existing_customer, email, phone):
    """Creates or reuses the Customer a quotation attaches to — happens
    whether saving a draft or actually sending, since Quotation.customer
    is never nullable. Linking a Query to that customer (query.customer,
    query.status) is handled separately by the caller, only once the
    quotation is actually sent — a draft hasn't gone out to anyone yet, so
    the query shouldn't look like it has either."""
    if query:
        customer, _created = Customer.objects.get_or_create(name=customer_name)
        if query.contact_email:
            customer.email = query.contact_email
        if query.contact_phone:
            customer.phone = query.contact_phone
        customer.save(update_fields=['email', 'phone'])
    elif existing_customer:
        customer = existing_customer
        if email or phone:
            if email:
                customer.email = email
            if phone:
                customer.phone = phone
            customer.save(update_fields=['email', 'phone'])
    else:
        customer, _created = Customer.objects.get_or_create(name=customer_name)
        if email:
            customer.email = email
        if phone:
            customer.phone = phone
        customer.save(update_fields=['email', 'phone'])
    return customer


@staff_required(on_denied=lambda request: JsonResponse({'error': 'forbidden'}, status=403))
def product_code_lookup(request):
    """For the quote form's live preview: given a product type, grade and size, the
    existing product code, or the one sending the quote would create, or why none
    can be (see product_codes.describe_product_code). Read-only."""
    try:
        category = ProductCategory.objects.filter(pk=int(request.GET.get('category', ''))).first()
    except ValueError:
        category = None
    try:
        size = Decimal(request.GET.get('size', ''))
    except InvalidOperation:
        size = None
    return JsonResponse(describe_product_code(category, request.GET.get('grade', ''), size))


def _saved_values(form, is_draft):
    """What the header fields show on a normal page load: the draft's saved values, or
    whatever was prefilled (from a query, the customer's last quote, or a revision).
    Dates are ISO strings so <input type="date"> accepts them. Without this the
    template saw an empty `post` and showed every header field blank."""
    values = {key: (value.isoformat() if hasattr(value, 'isoformat') else value)
              for key, value in (form.initial or {}).items()}
    if not is_draft:
        values.setdefault('same_state_as_us', True)   # a new quotation starts as an in-state one
    return values


def _autofill_product_codes(item_dicts, create=False):
    """Give each item its product code without the owner choosing one: a code is
    its product type + grade + size, so once those are filled in it's either an
    existing code (matched) or, when the quote is actually being sent
    (`create=True`), a new one built in the agreed format (see product_codes.py).
    A code picked by hand is never overridden. Returns (codes created, warnings):
    warnings explain an item that couldn't get a code, e.g. a size finer than
    0.01 mm. An item left without a code gets one assigned when its order is
    confirmed."""
    created, warnings = [], []
    for item in item_dicts:
        if item.get('product_type'):
            continue
        category, grade, size = item.get('category'), item.get('grade'), item.get('size')
        if create and category:
            code, was_created = get_or_create_product_code(category, grade, size)
            if code:
                item['product_type'] = code
                if was_created:
                    created.append(code)
                continue
            reason = describe_product_code(category, grade, size)['reason']
            if reason:
                warnings.append(f"No product code for \"{item.get('description', '')}\": {reason}")
        item['product_type'] = _match_product_type(grade, size, category)
    return created, warnings


def _parse_draft_line_items(post_data):
    """Lenient parsing for the Save Draft path — unlike
    QuotationLineItemFormSet (used when actually sending, via
    formset.is_valid()), a draft row can have a product picked with
    quantity/rate not decided yet, and a draft can have zero fully-formed
    rows at all. A row with no description is skipped entirely (nothing
    worth keeping); any other field that doesn't parse is just left as
    None/a default rather than rejecting the whole save — the DB layer
    (QuotationLineItem.quantity/rate_per_kg nullable) exists specifically
    to hold this incomplete state."""
    def _dec(raw, default=None):
        try:
            return Decimal(raw)
        except (InvalidOperation, TypeError, ValueError):
            return default

    total = int(_dec(post_data.get('item-TOTAL_FORMS'), Decimal('0')))
    items = []
    for i in range(total):
        prefix = f'item-{i}-'
        description = post_data.get(prefix + 'description', '').strip()
        if not description:
            continue
        product_type_id = post_data.get(prefix + 'product_type') or None
        product_type = ProductType.objects.filter(pk=product_type_id).first() if product_type_id else None
        category_id = post_data.get(prefix + 'category') or None
        category = ProductCategory.objects.filter(pk=category_id).first() if category_id else None
        items.append({
            'description': description,
            'category': category,
            'product_type': product_type,
            'grade': post_data.get(prefix + 'grade', '').strip(),
            'size': _dec(post_data.get(prefix + 'size')),
            'quantity': _dec(post_data.get(prefix + 'quantity')),
            'unit': post_data.get(prefix + 'unit', '').strip() or 'KGS',
            'rate_per_kg': _dec(post_data.get(prefix + 'rate_per_kg')),
            'discount_pct': _dec(post_data.get(prefix + 'discount_pct'), Decimal('0')) or Decimal('0'),
            'hsn_sac': post_data.get(prefix + 'hsn_sac', '').strip(),
            'gst_pct': _dec(post_data.get(prefix + 'gst_pct'), Decimal('18')),
            'tool_cost': _dec(post_data.get(prefix + 'tool_cost'), Decimal('0')) or Decimal('0'),
            'moq': _dec(post_data.get(prefix + 'moq'), Decimal('0')) or Decimal('0'),
        })
    return items


@staff_required
def quotation_form(request, pk=None):
    """Builds and sends an official, multi-line-item Quotation — the one
    page every Send Quote action funnels into, reached three ways:
    ?query=<pk> (from the Query dashboard, prefilled from that lead's
    captured info), ?customer=<pk> (re-quoting an existing customer from
    the Orders dashboard), or with neither (a brand-new company — name/
    email/phone collected right here). With a pk, it instead resumes an
    existing **draft** (materials/urls.py's quotations/<pk>/edit/) — the
    customer is fixed to whatever the draft already has, same as the
    ?customer= case.

    Two submit actions, via POST 'action': 'save_draft' (lenient — no
    formset validation, quantity/rate can be left blank, zero fully-formed
    items is fine, no email sent, no quotation_no consumed) and 'send'
    (default — the original strict path: full form+formset validation,
    status flips to 'sent', which is what actually assigns quotation_no
    and triggers the email). Both happen atomically. GET never creates or
    changes anything, even with ?query= set or a draft pk."""

    draft = get_object_or_404(Quotation, pk=pk, status='draft') if pk else None

    query = draft.source_query if draft else _safe_get(Query.objects, request.GET.get('query') or request.POST.get('query'))
    existing_customer = draft.customer if draft else _safe_get(Customer.objects, request.GET.get('customer') or request.POST.get('customer'))
    # Always has all three keys (never a bare {}) — the template looks up
    # .name/.email/.phone on this unconditionally, and a key that's
    # genuinely missing (not just empty) raises during template rendering
    # under Django's test client.
    new_customer_initial = {'name': '', 'email': '', 'phone': ''}

    error = None
    if request.method == 'POST':
        action = request.POST.get('action', 'send')
        form = QuotationForm(request.POST)

        typed_name = request.POST.get('customer_name', '').strip()
        if query:
            customer_name = query.company_name or query.contact_phone or f"Query #{query.pk}"
        elif existing_customer:
            customer_name = existing_customer.name
        else:
            customer_name = typed_name

        # Always built, even for a draft save — the template renders
        # formset.management_form and iterates it unconditionally, and a
        # bound formset is also what redisplays whatever was typed if this
        # submission turns out to have an error. Only the 'send' path
        # actually enforces .is_valid() on it; a draft's items are parsed
        # leniently below instead (see _parse_draft_line_items).
        formset = QuotationLineItemFormSet(request.POST, prefix='item')
        if not customer_name:
            error = "Company name is required."
        elif not form.is_valid():
            error = _first_form_error(form)
        elif action == 'send' and not formset.is_valid():
            error = _first_formset_error(formset)

        if error is None:
            email = request.POST.get('customer_email', '').strip()
            phone = request.POST.get('customer_phone', '').strip()
            status = 'sent' if action == 'send' else 'draft'
            if action == 'send':
                item_dicts = [dict(item_data) for item_data in formset.cleaned_data]
            else:
                item_dicts = _parse_draft_line_items(request.POST)
            with transaction.atomic():
                created_codes, code_warnings = _autofill_product_codes(item_dicts, create=(action == 'send'))
                customer = _resolve_quotation_customer(customer_name, query, existing_customer, email, phone)

                if draft:
                    for field, value in form.cleaned_data.items():
                        setattr(draft, field, value)
                    draft.customer = customer
                    draft.status = status
                    draft.save()
                    quotation = draft
                    quotation.line_items.all().delete()
                else:
                    quotation = Quotation.objects.create(
                        customer=customer, source_query=query, status=status, **form.cleaned_data,
                    )
                for i, item_data in enumerate(item_dicts, start=1):
                    QuotationLineItem.objects.create(quotation=quotation, order=i, **item_data)

                if action == 'send' and query:
                    query.customer = customer
                    query.status = 'quote_sent'
                    query.save(update_fields=['customer', 'status'])

            if action == 'save_draft':
                messages.success(request, f"Draft saved for {customer.name}.")
                return redirect('quotation_edit', pk=quotation.pk)

            for code in created_codes:
                messages.success(
                    request,
                    f"New product code {code.item_code} created ({code.category.name}, {code.grade}, {code.size:g} mm) — "
                    f"it's in the admin under Product codes.",
                )
            for warning in code_warnings:
                messages.warning(request, warning)

            if customer.email:
                _dispatch_quote_email(request, customer, quotation)
            else:
                messages.warning(
                    request,
                    f"No email on file for {customer.name} — use “Copy Link” to share the quote form, "
                    f"or download the quotation PDF below to send manually.",
                )
            return redirect('query_dashboard' if query else 'order_dashboard')
    else:
        if draft:
            from django.forms.models import model_to_dict
            form = QuotationForm(initial=model_to_dict(draft))
            item_initial = [
                {
                    'description': item.description, 'category': item.category_id, 'product_type': item.product_type_id,
                    'grade': item.grade, 'size': item.size, 'quantity': item.quantity,
                    'unit': item.unit, 'rate_per_kg': item.rate_per_kg, 'discount_pct': item.discount_pct,
                    'hsn_sac': item.hsn_sac, 'gst_pct': item.gst_pct, 'tool_cost': item.tool_cost, 'moq': item.moq,
                }
                for item in draft.line_items.all()
            ] or [{}]
        else:
            item_initial = [{}]
            if query:
                item_initial = [{
                    'description': (
                        query.product_type.item_code if query.product_type
                        else (query.product_description.splitlines() or [''])[0][:255] or query.grade or 'Item'
                    ),
                    'category': query.product_category_id,
                    'product_type': query.product_type_id,
                    'grade': query.grade,
                    'size': query.size,
                    'quantity': query.quantity or Decimal('1'),
                }]
            elif not existing_customer:
                # A brand-new company — "Send Form to Them" on the Orders
                # dashboard carries over whatever was already typed into its
                # own Company Name/Email/Phone fields rather than losing it.
                new_customer_initial = {
                    'name': request.GET.get('name', ''),
                    'email': request.GET.get('email', ''),
                    'phone': request.GET.get('phone', ''),
                }
            # Address and GSTIN print as their own rows in the "Quotation to" box. From the
            # query when there is one (what the customer told the bot); otherwise from the
            # last quote sent to this customer, so a repeat quote doesn't start blank.
            form_initial = {}
            if query and (query.gst_address or query.gst_number):
                form_initial = {'customer_address': query.gst_address, 'customer_gstin': query.gst_number}
            elif existing_customer:
                last = existing_customer.quotations.filter(status='sent').order_by('-quotation_no').first()
                if last and (last.customer_address or last.customer_gstin):
                    form_initial = {'customer_address': last.customer_address, 'customer_gstin': last.customer_gstin}
            if query and query.sent_quotations():
                # Sending again means the quote is being updated: Rev. 1, 2, ... dated today.
                form_initial.update({'rev_no': len(query.sent_quotations()), 'rev_date': timezone.localdate()})
            form = QuotationForm(initial=form_initial or None)
        formset = QuotationLineItemFormSet(initial=item_initial, prefix='item')

    return render(request, 'materials/quotation_form.html', {
        # product type + grade + size -> product code, for the form's live auto-match (see _autofill_product_codes)
        'product_code_map': [
            {'pk': pt.pk, 'category': pt.category_id, 'grade': pt.grade.lower(), 'size': f"{pt.size:.3f}"}
            for pt in ProductType.objects.exclude(size=None)
        ],
        'form': form,
        'formset': formset,
        'query': query,
        'existing_customer': existing_customer,
        'new_customer_initial': new_customer_initial,
        'draft': draft,
        'error': error,
        'post': request.POST if error else _saved_values(form, is_draft=draft is not None),
    })


@staff_required
def quotation_drafts(request):
    """Every in-progress, not-yet-sent Quotation — 'Resume' picks up right
    where it was left off (quotation_form in edit mode); 'Discard' deletes
    it outright, safe because a draft never consumed a quotation_no."""
    drafts = Quotation.objects.filter(status='draft').select_related('customer').prefetch_related('line_items').order_by('-updated_at')
    return render(request, 'materials/quotation_drafts.html', {'drafts': drafts})


@staff_required
def quotation_discard(request, pk):
    if request.method != 'POST':
        return redirect('quotation_drafts')
    draft = get_object_or_404(Quotation, pk=pk, status='draft')
    customer_name = draft.customer.name
    draft.delete()
    messages.success(request, f"Discarded the draft quotation for {customer_name}.")
    return redirect('quotation_drafts')


def _public_quote_base_url(request):
    """The origin (scheme+host, no trailing slash) that any customer-facing
    quote link must be built from. Admins only ever reach this app over
    Tailscale — building a link from this request's own host would hand an
    external customer a private address they can never open. Used for both
    the emailed link (_dispatch_quote_email) and the dashboards' "Copy
    Link" fallback, which previously built its URL from request.get_host()
    directly and inherited that same bug."""
    return settings.PUBLIC_QUOTE_BASE_URL or request.build_absolute_uri('/').rstrip('/')


def _dispatch_quote_email(request, customer, quotation):
    """Emails the official quotation PDF, followed by the order-form link,
    to customer.email. Adds a Django message for success/failure."""
    if not settings.EMAIL_HOST_USER:
        messages.error(request, "Email is not configured — set EMAIL_HOST, EMAIL_HOST_USER, and EMAIL_HOST_PASSWORD in your .env file.")
        return

    quote_path = reverse('quote_form', kwargs={'token': customer.quote_token})
    quote_url = f"{_public_quote_base_url(request)}{quote_path}"
    try:
        pdf_bytes = generate_quotation_pdf(quotation)
        email = EmailMessage(
            subject=f"Quotation {quotation.formatted_no()} — {settings.COMPANY_NAME}",
            body=(
                f"Dear {customer.name},\n\n"
                f"Please find attached our official quotation {quotation.formatted_no()} "
                f"for your requirement.\n\n"
                f"If you wish to proceed, please log your order using the link below — "
                f"you're welcome to attach your own Purchase Order there too, if you have one:\n\n"
                f"{quote_url}\n\n"
                f"This link is personal to your company and can be used once, to place the order for "
                f"this quotation. For any later order we will send you a new quotation and link.\n\n"
                f"Regards,\n{settings.COMPANY_NAME}"
            ),
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=[customer.email],
        )
        email.attach(f"{quotation.formatted_no()}.pdf", pdf_bytes, "application/pdf")
        email.send()
        messages.success(request, f"Quotation {quotation.formatted_no()} sent to {customer.email}.")
    except Exception as e:
        messages.error(request, f"Failed to send email: {e}")


@staff_required
def quotation_pdf(request, pk):
    """Standalone download of a quotation's PDF — used for the Copy Link
    fallback (no email on file to attach it to) and for staff wanting to
    re-download/print one already sent."""
    quotation = get_object_or_404(Quotation, pk=pk)
    pdf_bytes = generate_quotation_pdf(quotation)
    response = HttpResponse(pdf_bytes, content_type='application/pdf')
    response['Content-Disposition'] = f'attachment; filename="{quotation.formatted_no()}.pdf"'
    return response
