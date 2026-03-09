from django.shortcuts import render
from django.contrib import messages
from .forms import MaterialForm


def index(request):

    if request.method == "POST":
        form = MaterialForm(request.POST)

        if form.is_valid():
            form.save()
            messages.success(request, "Form submitted successfully!")
            form = MaterialForm()

    else:
        form = MaterialForm()

    return render(request, "materials/index.html", {"form": form})