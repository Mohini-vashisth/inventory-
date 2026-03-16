from django.contrib import admin
from .models import Material


@admin.register(Material)
class MaterialAdmin(admin.ModelAdmin):

    list_display = (
        "coil_no",
        "date",
        "grade",
        "size",
        "company",
        "vendor",
        "quantity",
        "heat_no",
    )

    list_filter = (
        "grade",
        "company",
        "vendor",
        "date",
    )

    search_fields = (
        "grade",
        "company",
        "vendor",
        "heat_no",
    )