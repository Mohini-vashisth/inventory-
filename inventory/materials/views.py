from django.shortcuts import render
from django.shortcuts import render, redirect
from django.contrib.auth import authenticate, login
from .models import Material
from django.shortcuts import render
from .models import Material
from django.db.models import Sum
from datetime import datetime


def admin_dashboard(request):

    total_materials = Material.objects.count()

    total_quantity = Material.objects.aggregate(
        Sum("quantity")
    )["quantity__sum"] or 0

    current_month = datetime.now().month

    materials_this_month = Material.objects.filter(
        date__month=current_month
    ).count()

    context = {
        "total_materials": total_materials,
        "total_quantity": total_quantity,
        "materials_this_month": materials_this_month,
    }

    return render(request,"materials/admin_dashboard.html",context)

def material_table(request):

    materials = Material.objects.all()

    return render(request, "materials/material_table.html", {
        "materials": materials
    })

def home(request):
    return render(request, "home.html")

def material_form(request):
    return render(request, "index.html")


def admin_login(request):

    if request.method == "POST":
        username = request.POST.get("username")
        password = request.POST.get("password")

        user = authenticate(request, username=username, password=password)

        if user is not None:
            login(request, user)
            return redirect("/admin/")
        else:
            return render(request, "admin_login.html", {"error":"Invalid credentials"})

    return render(request, "admin_login.html")