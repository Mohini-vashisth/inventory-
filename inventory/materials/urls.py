from django.urls import path
from . import views

urlpatterns = [
    path("", views.home, name="home"),
    path("material-form/", views.material_form, name="material_form"),
    path("admin-login/", views.admin_login, name="admin_login"),
    path("employee/", views.employee_landing, name="employee"),  # ← changed this
    path('coil/<int:pk>/tag/', views.coil_tag, name='coil_tag'),
    path('coil/<int:coil_pk>/parts/', views.coil_parts, name='coil_parts'),
    path('part/<int:part_pk>/new-job/', views.create_job, name='create_job'),
    path('job/<int:pk>/', views.job_detail, name='job_detail'),
]