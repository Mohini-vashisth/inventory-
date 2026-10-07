from django.urls import path
from .views import auth, gate_entry, media, orders, picking, production, queries, quotations, whatsapp

urlpatterns = [
    path("", auth.home, name="home"),
    path("gate-entry/", gate_entry.gate_entry_form, name="gate_entry_form"),
    path("gate-entry/autocomplete/", gate_entry.material_field_autocomplete, name="material_field_autocomplete"),
    path("gate-entry/select/", gate_entry.select_gate_entry, name="select_gate_entry"),
    path("gate-entry/<int:gate_entry_pk>/", gate_entry.gate_entry_detail, name="gate_entry_detail"),
    path("gate-entry/<int:gate_entry_pk>/edit/", gate_entry.gate_entry_edit, name="gate_entry_edit"),
    path("gate-entry/<int:gate_entry_pk>/add-lot/", gate_entry.gate_entry_lot_form, name="gate_entry_lot_form"),
    path("gate-entry/lot/<int:lot_pk>/delete/", gate_entry.gate_entry_lot_delete, name="gate_entry_lot_delete"),
    path("gate-entry/lot/<int:lot_pk>/coil/", gate_entry.material_form, name="material_form"),
    path("admin-login/", auth.admin_login, name="admin_login"),
    path("employee/", auth.employee_landing, name="employee"),
    path("employee-login/", auth.employee_login, name="employee_login"),
    path("employee-logout/", auth.employee_logout, name="employee_logout"),
    path('coil/<int:pk>/tag/', gate_entry.coil_tag, name='coil_tag'),
    path('select-order/', picking.select_order, name='select_order'),
    path('order/<int:order_pk>/select-coil/', picking.select_coil_for_order, name='select_coil_for_order'),
    path('order/<int:order_pk>/pick-coil/<int:coil_pk>/', picking.pick_coil_for_order, name='pick_coil_for_order'),
    path('production-board/', production.production_board, name='production_board'),
    path('scan-job/', production.select_job_for_coil, name='select_job_for_coil'),
    path('job/<int:pk>/', production.job_detail, name='job_detail'),
    path('queries/', queries.query_dashboard, name='query_dashboard'),
    path('queries/<int:pk>/', queries.query_detail, name='query_detail'),
    path('queries/<int:pk>/edit/', queries.query_edit, name='query_edit'),
    path('queries/<int:pk>/not-interested/', queries.query_not_interested, name='query_not_interested'),
    path('queries/<int:pk>/clear-review/', queries.query_clear_review, name='query_clear_review'),
    path('quotations/new/', quotations.quotation_form, name='quotation_form'),
    path('product-codes/lookup/', quotations.product_code_lookup, name='product_code_lookup'),
    path('quotations/drafts/', quotations.quotation_drafts, name='quotation_drafts'),
    path('quotations/<int:pk>/edit/', quotations.quotation_form, name='quotation_edit'),
    path('quotations/<int:pk>/discard/', quotations.quotation_discard, name='quotation_discard'),
    path('quotations/<int:pk>/pdf/', quotations.quotation_pdf, name='quotation_pdf'),
    # Public, unauthenticated — Meta's Cloud API calls this directly. Verified
    # via HMAC/verify-token inside the view, not Django auth. Reaches the
    # internet only via its own Tailscale Funnel path on the deployment
    # machine — see CLAUDE.md.
    path('webhooks/whatsapp/', whatsapp.whatsapp_webhook, name='whatsapp_webhook'),
    path('orders/', orders.order_dashboard, name='order_dashboard'),
    path('orders/customer-autocomplete/', orders.customer_autocomplete, name='customer_autocomplete'),
    path('orders/<int:pk>/', orders.order_detail, name='order_detail'),
    path('orders/<int:pk>/confirm/', orders.order_confirm, name='order_confirm'),
    path('orders/<int:pk>/reject/', orders.order_reject, name='order_reject'),
    path('orders/<int:pk>/dispatch/', orders.order_dispatch, name='order_dispatch'),
    path('media/<path:path>', media.serve_media, name='serve_media'),
    path('quote/<uuid:token>/', orders.quote_form, name='quote_form'),
]