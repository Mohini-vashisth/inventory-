from django import forms
from .models import Material, GradeOption, SizeOption, Order


class MaterialForm(forms.ModelForm):
    class Meta:
        model = Material
        exclude = ['coil_no']

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


class OrderForm(forms.ModelForm):
    class Meta:
        model = Order
        fields = [
            'product_type', 'drawing_dimensions', 'grade', 'size', 'mill_make',
            'mechanical_properties', 'processes', 'end_usage', 'delivery_form',
            'quantity', 'frequency', 'delivery_date', 'notes',
        ]