from django.urls import path
from .views import index

urlpatterns = [
    path('', index, name='material_form'),
]