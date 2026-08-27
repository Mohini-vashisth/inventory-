"""
Read-only REST API over the core domain (orders, coils, jobs, product types).

Deliberately read-only: the mutating rules for this domain — an order can't
be confirmed without a product type, a coil part can't be cut past its
remaining weight, a production step only unlocks once every step before it
is completed — live in materials.views and are exercised through the
guarded web forms. Re-exposing writes here would mean re-implementing every
one of those guards a second time, which is exactly how they'd eventually
drift out of sync. Consumers that need to change state use the existing
endpoints; this surface is for reading.
"""
from decimal import Decimal

from django.db.models import DecimalField, F, Sum, Value
from django.db.models.functions import Coalesce
from rest_framework import viewsets

from .models import Material, Order, ProductionJob, ProductType
from .serializers import (
    MaterialSerializer, OrderSerializer, ProductionJobSerializer, ProductTypeSerializer,
)


class ProductTypeViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = ProductType.objects.prefetch_related('steps').order_by('name')
    serializer_class = ProductTypeSerializer


class MaterialViewSet(viewsets.ReadOnlyModelViewSet):
    """Coils. Supports ?remaining=true to only show coils with weight left to cut."""
    serializer_class = MaterialSerializer

    def get_queryset(self):
        qs = Material.objects.prefetch_related('parts').order_by('-coil_no')
        if self.request.query_params.get('remaining') == 'true':
            qs = (qs.annotate(weight_used=Coalesce(
                        Sum('parts__weight'), Value(Decimal('0')), output_field=DecimalField()))
                    .filter(quantity__gt=F('weight_used')))
        return qs


class ProductionJobViewSet(viewsets.ReadOnlyModelViewSet):
    """Supports ?status=in_progress and ?order=<id> filters."""
    serializer_class = ProductionJobSerializer

    def get_queryset(self):
        qs = (ProductionJob.objects
              .select_related('product_type', 'part__coil', 'order')
              .prefetch_related('step_logs__step')
              .order_by('-created_at'))
        status_param = self.request.query_params.get('status')
        if status_param:
            qs = qs.filter(status=status_param)
        order_param = self.request.query_params.get('order')
        if order_param:
            qs = qs.filter(order_id=order_param)
        return qs


class OrderViewSet(viewsets.ReadOnlyModelViewSet):
    """Supports ?status=in_production."""
    serializer_class = OrderSerializer

    def get_queryset(self):
        qs = (Order.objects
              .select_related('customer', 'product_type')
              .order_by('-created_at'))
        status_param = self.request.query_params.get('status')
        if status_param:
            qs = qs.filter(status=status_param)
        return qs
