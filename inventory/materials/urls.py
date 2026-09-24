from django.urls import path
from . import views

urlpatterns = [
    path("", views.home, name="home"),
    path("gate-entry/", views.gate_entry_form, name="gate_entry_form"),
    path("gate-entry/autocomplete/", views.material_field_autocomplete, name="material_field_autocomplete"),
    path("gate-entry/select/", views.select_gate_entry, name="select_gate_entry"),
    path("gate-entry/<int:gate_entry_pk>/", views.gate_entry_detail, name="gate_entry_detail"),
    path("gate-entry/<int:gate_entry_pk>/edit/", views.gate_entry_edit, name="gate_entry_edit"),
    path("gate-entry/<int:gate_entry_pk>/add-lot/", views.gate_entry_lot_form, name="gate_entry_lot_form"),
    path("gate-entry/lot/<int:lot_pk>/delete/", views.gate_entry_lot_delete, name="gate_entry_lot_delete"),
    path("gate-entry/lot/<int:lot_pk>/coil/", views.material_form, name="material_form"),
    path("admin-login/", views.admin_login, name="admin_login"),
    path("employee/", views.employee_landing, name="employee"),
    path("employee-login/", views.employee_login, name="employee_login"),
    path("employee-logout/", views.employee_logout, name="employee_logout"),
    path('coil/<int:pk>/tag/', views.coil_tag, name='coil_tag'),
    path('select-order/', views.select_order, name='select_order'),
    path('order/<int:order_pk>/select-coil/', views.select_coil_for_order, name='select_coil_for_order'),
    path('order/<int:order_pk>/pick-coil/<int:coil_pk>/', views.pick_coil_for_order, name='pick_coil_for_order'),
    path('production-board/', views.production_board, name='production_board'),
    path('scan-job/', views.select_job_for_coil, name='select_job_for_coil'),
    path('job/<int:pk>/', views.job_detail, name='job_detail'),
    path('queries/', views.query_dashboard, name='query_dashboard'),
    path('queries/<int:pk>/edit/', views.query_edit, name='query_edit'),
    path('queries/<int:pk>/send-quote/', views.query_send_quote, name='query_send_quote'),
    path('queries/<int:pk>/not-interested/', views.query_not_interested, name='query_not_interested'),
    # Public, unauthenticated — Meta's Cloud API calls this directly. Verified
    # via HMAC/verify-token inside the view, not Django auth. Reaches the
    # internet only via its own Tailscale Funnel path on the deployment
    # machine — see CLAUDE.md.
    path('webhooks/whatsapp/', views.whatsapp_webhook, name='whatsapp_webhook'),
    path('orders/', views.order_dashboard, name='order_dashboard'),
    path('orders/customer-autocomplete/', views.customer_autocomplete, name='customer_autocomplete'),
    path('orders/<int:pk>/confirm/', views.order_confirm, name='order_confirm'),
    path('orders/<int:pk>/reject/', views.order_reject, name='order_reject'),
    path('orders/<int:pk>/dispatch/', views.order_dispatch, name='order_dispatch'),
    path('orders/customer/<int:pk>/send-quote/', views.send_quote_email, name='send_quote_email'),
    path('orders/quick-send-quote/', views.quick_send_quote, name='quick_send_quote'),
    path('quote/<uuid:token>/', views.quote_form, name='quote_form'),
]