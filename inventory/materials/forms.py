from django import forms
from django.forms import formset_factory
from .models import GateEntry, Material, GradeOption, SizeOption, Order


class GateEntryForm(forms.ModelForm):
    class Meta:
        model = GateEntry
        fields = ['date', 'vendor', 'vehicle_no', 'invoice_no', 'total_weight']

    def clean_vehicle_no(self):
        """The form already forces uppercase as the employee types — this is
        just a safety net for anything submitted without JS (a direct API
        call, JS disabled, etc.)."""
        vehicle_no = self.cleaned_data['vehicle_no']
        return vehicle_no.upper() if vehicle_no else vehicle_no

    def clean_total_weight(self):
        total_weight = self.cleaned_data['total_weight']
        if total_weight <= 0:
            raise forms.ValidationError("Total weight must be greater than zero.")
        return total_weight


class GateEntryLotForm(forms.Form):
    """A single lot row — company/grade/size/no_of_coils. Used both as a
    standalone form (adding one more lot to an existing gate entry) and,
    via GateEntryLotFormSet, as a repeatable row on the gate entry creation
    page so a mixed-brand/grade/size truck can be logged in one submission."""
    company = forms.CharField(max_length=100, required=False)
    grade = forms.CharField(max_length=10)
    size = forms.DecimalField(max_digits=10, decimal_places=3)
    no_of_coils = forms.IntegerField(min_value=1)

    def clean_grade(self):
        grade = self.cleaned_data['grade']
        if not GradeOption.objects.filter(name=grade).exists():
            raise forms.ValidationError("Select a grade from the list.")
        return grade

    def clean_size(self):
        size = self.cleaned_data['size']
        if not SizeOption.objects.filter(value=size).exists():
            raise forms.ValidationError("Select a size from the list.")
        return size


GateEntryLotFormSet = formset_factory(GateEntryLotForm, extra=0, min_num=1, validate_min=True)


class MaterialForm(forms.ModelForm):
    class Meta:
        model = Material
        # archived_at/legacy_used_weight are excluded alongside the other
        # server-assigned fields — a coil being newly registered here was
        # never archived and has no pre-app legacy usage to speak of; both
        # only get set through the admin (archiving) or import_excel (legacy
        # usage from the spreadsheet), never through this form.
        exclude = ['coil_no', 'lot', 'invoice_weight', 'archived_at', 'legacy_used_weight']

    def clean_grade(self):
        grade = self.cleaned_data['grade']
        if not GradeOption.objects.filter(name=grade).exists():
            raise forms.ValidationError("Select a grade from the list.")
        return grade

    def clean_size(self):
        size = self.cleaned_data['size']
        if not SizeOption.objects.filter(value=size).exists():
            raise forms.ValidationError("Select a size from the list.")
        return size

    def clean_quantity(self):
        quantity = self.cleaned_data['quantity']
        if quantity <= 0:
            raise forms.ValidationError("Weight must be greater than zero.")
        return quantity


class OrderForm(forms.ModelForm):
    class Meta:
        model = Order
        fields = [
            'drawing_dimensions', 'grade', 'size', 'mill_make',
            'mechanical_properties', 'processes', 'end_usage', 'delivery_form',
            'quantity', 'frequency', 'delivery_date', 'notes', 'purchase_order',
        ]


class OrderItemForm(forms.ModelForm):
    """One quoted item on the customer's order form. Product code, grade and
    size are deliberately NOT fields here — the view takes them from the
    quotation line item (the one `line_item` points at), so the customer can
    neither pick a code nor change the spec the quote was priced for."""
    line_item = forms.IntegerField(widget=forms.HiddenInput)

    class Meta:
        model = Order
        fields = [
            'drawing_dimensions', 'mill_make', 'mechanical_properties', 'processes',
            'end_usage', 'delivery_form', 'quantity', 'frequency', 'delivery_date', 'notes',
        ]
        widgets = {
            'delivery_date': forms.DateInput(attrs={'type': 'date'}),
            'drawing_dimensions': forms.Textarea(attrs={'rows': 2, 'placeholder': 'Describe dimensions or reference a drawing...'}),
            'mechanical_properties': forms.Textarea(attrs={'rows': 2, 'placeholder': 'e.g. Tensile: 700 MPa, Hardness: 200 HB'}),
            'mill_make': forms.TextInput(attrs={'placeholder': 'e.g. SAIL, Tata, Any'}),
            'processes': forms.TextInput(attrs={'placeholder': 'e.g. Drilling, Tapping, Machining, Heat treatment'}),
            'end_usage': forms.TextInput(attrs={'placeholder': 'e.g. Automotive axle, Gear shaft'}),
            'notes': forms.TextInput(attrs={'placeholder': 'Any other requirements'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name in ('delivery_form', 'frequency'):
            self.fields[name].choices = [('', '— select —')] + [c for c in self.fields[name].choices if c[0]]


OrderItemFormSet = formset_factory(OrderItemForm, extra=0)


class QuotationLineItemForm(forms.Form):
    """One priced item within a quotation — company/grade/size, qty, rate,
    and the GST/discount/HSN details a real quotation needs. Used as a
    repeatable row via QuotationLineItemFormSet, the same "collapsible
    repeatable row" pattern GateEntryLotFormSet already established for a
    gate entry's lots."""
    description  = forms.CharField(max_length=255)
    product_type = forms.ModelChoiceField(queryset=None, required=False)
    grade        = forms.CharField(max_length=100, required=False)
    size         = forms.DecimalField(max_digits=10, decimal_places=3, required=False)
    quantity     = forms.DecimalField(max_digits=10, decimal_places=3, min_value=0.001)
    unit         = forms.CharField(max_length=20, required=False, initial='KGS')
    rate_per_kg  = forms.DecimalField(max_digits=10, decimal_places=2, min_value=0.01)
    discount_pct = forms.DecimalField(max_digits=5, decimal_places=2, required=False, initial=0, min_value=0, max_value=100)
    hsn_sac      = forms.CharField(max_length=20, required=False)
    gst_pct      = forms.DecimalField(max_digits=5, decimal_places=2, required=False, initial=18, min_value=0, max_value=100)
    tool_cost    = forms.DecimalField(max_digits=10, decimal_places=2, required=False, initial=0, min_value=0)
    moq          = forms.DecimalField(max_digits=10, decimal_places=3, required=False, initial=0, min_value=0)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Imported here (not at module level) to dodge a circular-import
        # headache with .models — ProductType is only needed for this one
        # queryset assignment.
        from .models import ProductType
        self.fields['product_type'].queryset = ProductType.objects.order_by('item_code')

    def clean_unit(self):
        return self.cleaned_data.get('unit') or 'KGS'

    def clean_discount_pct(self):
        return self.cleaned_data.get('discount_pct') or 0

    def clean_gst_pct(self):
        value = self.cleaned_data.get('gst_pct')
        return value if value is not None else 18

    def clean_tool_cost(self):
        return self.cleaned_data.get('tool_cost') or 0

    def clean_moq(self):
        return self.cleaned_data.get('moq') or 0


QuotationLineItemFormSet = formset_factory(QuotationLineItemForm, extra=0, min_num=1, validate_min=True)


class QuotationForm(forms.Form):
    """The header fields for a new quotation — everything except the
    customer (resolved separately in the view, from a Query/Customer/raw
    name+email+phone depending on how the form was reached) and the line
    items (a separate formset). Every field here is optional: a quotation
    with none of them filled in is still valid, just sparse, the same way
    the PDF already renders any of these as blank/omitted."""
    ref_no          = forms.CharField(max_length=50, required=False)
    rev_no          = forms.IntegerField(required=False, min_value=0, initial=0)
    rev_date        = forms.DateField(required=False)
    sales_person    = forms.CharField(max_length=100, required=False)
    kind_attn       = forms.CharField(max_length=100, required=False)
    subject         = forms.CharField(max_length=200, required=False)
    customer_address = forms.CharField(widget=forms.Textarea, required=False)
    same_state_as_us = forms.BooleanField(required=False, initial=True)
    freight_amount  = forms.DecimalField(max_digits=10, decimal_places=2, required=False, initial=0, min_value=0)
    pf_amount       = forms.DecimalField(max_digits=10, decimal_places=2, required=False, initial=0, min_value=0)
    price_basis     = forms.CharField(max_length=200, required=False, initial='Ex-Works')
    gst_terms       = forms.CharField(max_length=200, required=False, initial='Extra As Applicable')
    insurance_terms = forms.CharField(max_length=200, required=False, initial='Extra At actual to be borne by Customer')
    freight_terms   = forms.CharField(max_length=200, required=False, initial='The Same Shall be in your scope')
    payment_terms   = forms.CharField(max_length=200, required=False, initial='100% Advance')
    delivery_terms  = forms.CharField(max_length=200, required=False)
    validity_terms  = forms.CharField(max_length=200, required=False, initial='7 Days from date of offer')

    def clean_rev_no(self):
        # IntegerField(required=False) yields None when left blank, but
        # Quotation.rev_no isn't nullable (PositiveIntegerField, default 0).
        return self.cleaned_data.get('rev_no') or 0

    def clean_freight_amount(self):
        return self.cleaned_data.get('freight_amount') or 0

    def clean_pf_amount(self):
        return self.cleaned_data.get('pf_amount') or 0