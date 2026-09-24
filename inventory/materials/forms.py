from django import forms
from django.forms import formset_factory
from .models import GateEntry, GateEntryLot, Material, GradeOption, SizeOption, Order


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
        exclude = ['coil_no', 'lot', 'invoice_weight']

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
            'product_type', 'drawing_dimensions', 'grade', 'size', 'mill_make',
            'mechanical_properties', 'processes', 'end_usage', 'delivery_form',
            'quantity', 'frequency', 'delivery_date', 'notes', 'purchase_order',
        ]