from django.urls import path
from . import views

urlpatterns = [
    path("admin-dashboard/", views.admin_dashboard, name="admin_dashboard"),
    path('materials-table/', views.material_table, name='materials_table'),
    path('', views.home, name="home"),
    path('employee/', views.material_form, name="employee"),
    path('admin-login/', views.admin_login, name="admin_login"),
]