"""Shared helpers for the test modules."""




def quotation_item_post_data(**overrides):
    """Minimal valid POST data for quotation_form's one-item formset —
    every test that submits that form needs the management-form fields
    plus a single valid item row, so this is shared rather than
    reimplemented per test class."""
    data = {
        'item-TOTAL_FORMS': '1', 'item-INITIAL_FORMS': '0',
        'item-MIN_NUM_FORMS': '0', 'item-MAX_NUM_FORMS': '1000',
        'item-0-description': 'Steel Bar', 'item-0-quantity': '10',
        'item-0-rate_per_kg': '85.50', 'item-0-unit': 'KGS',
    }
    data.update(overrides)
    return data
