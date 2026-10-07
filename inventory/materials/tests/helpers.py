"""Shared helpers for the test modules."""




def quotation_item_post_data(**overrides):
    """Minimal valid POST data for quotation_form's one-item formset —
    every test that submits that form needs the management-form fields
    plus a single valid item row, so this is shared rather than
    reimplemented per test class."""
    data = {
        'item-TOTAL_FORMS': '1', 'item-INITIAL_FORMS': '0',
        'item-MIN_NUM_FORMS': '0', 'item-MAX_NUM_FORMS': '1000',
        'item-0-description': 'Steel Bar', 'item-0-width': '50', 'item-0-thickness': '6', 'item-0-quantity': '10',
        'item-0-rate_per_kg': '85.50', 'item-0-unit': 'KGS',
    }
    data.update(overrides)
    return data


ITEM_FIELDS = ('product_category', 'product_type', 'grade', 'width', 'thickness', 'quantity', 'delivery_form')


def create_query(**fields):
    """A Query plus, when product details are given, its first product (QueryItem): keeps tests that
    describe a query as one product short. Item fields go to the item, everything else to the query."""
    from ..models import Query, QueryItem
    item_fields = {name: fields.pop(name) for name in ITEM_FIELDS if name in fields}
    query = Query.objects.create(**fields)
    if item_fields:
        QueryItem.objects.create(query=query, position=1, **item_fields)
    return query


def item_formset(rows=(), initial=0):
    """POST data for the query Edit page's product rows (`rows` are dicts of field -> value; rows that
    already exist carry their 'id' and come first, `initial` of them)."""
    data = {'item-TOTAL_FORMS': str(len(rows)), 'item-INITIAL_FORMS': str(initial),
            'item-MIN_NUM_FORMS': '0', 'item-MAX_NUM_FORMS': '1000'}
    for number, row in enumerate(rows):
        for name, value in row.items():
            data[f'item-{number}-{name}'] = '' if value is None else str(value)
    return data


def item_row(item):
    """An existing QueryItem as a posted row, unchanged."""
    return {'id': item.pk, 'product_category': item.product_category_id or '', 'product_type': item.product_type_id or '',
            'grade': item.grade, 'width': item.width if item.width is not None else '',
            'thickness': item.thickness if item.thickness is not None else '',
            'quantity': item.quantity if item.quantity is not None else '', 'delivery_form': item.delivery_form}


def query_edit_data(query, rows=None, **fields):
    """POST data for the query Edit page: the query's own fields, plus its products — unchanged unless
    `rows` is given (then exactly those rows, existing ones first)."""
    data = {'company_name': query.company_name, 'contact_phone': query.contact_phone, 'contact_email': query.contact_email,
            'notes': query.notes}
    data.update({name: getattr(query, name) for name, _ in query.INTAKE_TEXT_FIELDS})
    if rows is None:
        rows = [item_row(item) for item in query.item_list()]
    rows = sorted(rows, key=lambda row: 0 if 'id' in row else 1)
    data.update(item_formset(rows, initial=sum(1 for row in rows if 'id' in row)))
    data.update(fields)
    return data
