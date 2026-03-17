from django.urls import path
from . import views

urlpatterns = [
    path("", views.home, name="home"),
    path("material-form/", views.material_form, name="material_form"),
    path("materials-table/", views.material_table, name="materials_table"),
    path("admin-dashboard/", views.admin_dashboard, name="admin_dashboard"),
    path("admin-login/", views.admin_login, name="admin_login"),
    path("employee/", views.material_form, name="employee")
]