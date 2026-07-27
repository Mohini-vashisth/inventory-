from django.urls import path
from . import views

urlpatterns = [
    path("", views.home, name="home"),
    path("material-form/", views.material_form, name="material_form"),
    path("admin-login/", views.admin_login, name="admin_login"),
    path("employee/", views.employee_landing, name="employee"),
    path("employee-login/", views.employee_login, name="employee_login"),
    path("employee-logout/", views.employee_logout, name="employee_logout"),
    path('coil/<int:pk>/tag/', views.coil_tag, name='coil_tag'),
    path('coil/<int:coil_pk>/parts/', views.coil_parts, name='coil_parts'),
    path('select-order/', views.select_order, name='select_order'),
    path('order/<int:order_pk>/select-coil/', views.select_coil_for_order, name='select_coil_for_order'),
    path('production-board/', views.production_board, name='production_board'),
    path('job/<int:pk>/', views.job_detail, name='job_detail'),
    path('orders/', views.order_dashboard, name='order_dashboard'),
    path('orders/customer-autocomplete/', views.customer_autocomplete, name='customer_autocomplete'),
    path('orders/<int:pk>/confirm/', views.order_confirm, name='order_confirm'),
    path('orders/<int:pk>/reject/', views.order_reject, name='order_reject'),
    path('orders/<int:pk>/dispatch/', views.order_dispatch, name='order_dispatch'),
    path('orders/customer/<int:pk>/send-quote/', views.send_quote_email, name='send_quote_email'),
    path('orders/quick-send-quote/', views.quick_send_quote, name='quick_send_quote'),
    path('quote/<uuid:token>/', views.quote_form, name='quote_form'),
]