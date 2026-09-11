from django.db.models import Sum
from rest_framework import serializers

from .models import Customer, Material, Order, ProcessStep, ProductionJob, ProductType, StepLog


class ProcessStepSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProcessStep
        fields = ['id', 'name', 'order']


class ProductTypeSerializer(serializers.ModelSerializer):
    steps = ProcessStepSerializer(many=True, read_only=True)

    class Meta:
        model = ProductType
        fields = ['id', 'name', 'grade', 'size', 'description', 'steps']


class MaterialSerializer(serializers.ModelSerializer):
    """A coil. weight_used/weight_remaining/is_used_up all come from
    Material's own methods — the single source of truth also used by the
    admin and the part-cutting form."""
    coil_no_formatted = serializers.CharField(source='formatted_coil', read_only=True)
    weight_used = serializers.SerializerMethodField()
    weight_remaining = serializers.SerializerMethodField()
    is_used_up = serializers.SerializerMethodField()
    is_archived = serializers.SerializerMethodField()

    class Meta:
        model = Material
        fields = [
            'coil_no', 'coil_no_formatted', 'date', 'grade', 'size', 'company',
            'vendor', 'quantity', 'heat_no', 'weight_used', 'weight_remaining',
            'is_used_up', 'is_archived', 'archived_at', 'legacy_used_weight',
        ]

    def get_weight_used(self, obj):
        return obj.weight_used()

    def get_weight_remaining(self, obj):
        return obj.weight_remaining()

    def get_is_archived(self, obj):
        return obj.is_archived()

    def get_is_used_up(self, obj):
        return obj.is_used_up()


class StepLogSerializer(serializers.ModelSerializer):
    step_name = serializers.CharField(source='step.name', read_only=True)

    class Meta:
        model = StepLog
        fields = ['id', 'step', 'step_name', 'status', 'timestamp', 'notes']


class ProductionJobSerializer(serializers.ModelSerializer):
    product_type_name = serializers.CharField(source='product_type.name', read_only=True)
    part_no = serializers.CharField(source='part.part_no', read_only=True)
    coil_no = serializers.CharField(source='part.coil.formatted_coil', read_only=True)
    latest_logs = serializers.SerializerMethodField()

    class Meta:
        model = ProductionJob
        fields = [
            'id', 'job_no', 'status', 'product_type', 'product_type_name',
            'part_no', 'coil_no', 'order', 'created_at', 'updated_at', 'latest_logs',
        ]

    def get_latest_logs(self, obj):
        """Most recent StepLog per step — the same status a step shows on the job detail page."""
        logs_by_step = {}
        for log in sorted(obj.step_logs.all(), key=lambda l: l.timestamp, reverse=True):
            logs_by_step.setdefault(log.step_id, log)
        return StepLogSerializer(logs_by_step.values(), many=True).data


class CustomerSerializer(serializers.ModelSerializer):
    class Meta:
        model = Customer
        fields = ['id', 'name', 'email', 'phone', 'created_at']


class OrderSerializer(serializers.ModelSerializer):
    customer_name = serializers.CharField(source='customer.name', read_only=True)
    product_type_name = serializers.CharField(source='product_type.name', read_only=True)
    weight_cut = serializers.SerializerMethodField()

    class Meta:
        model = Order
        fields = [
            'id', 'customer', 'customer_name', 'product_type', 'product_type_name',
            'grade', 'size', 'quantity', 'delivery_form', 'frequency', 'delivery_date',
            'status', 'created_at', 'weight_cut',
        ]

    def get_weight_cut(self, obj):
        # OrderViewSet annotates _weight_cut so this is one query for the
        # whole list, not one aggregate per order — fall back to a direct
        # aggregate if the serializer is ever used on an unannotated queryset.
        annotated = getattr(obj, '_weight_cut', None)
        return annotated if annotated is not None else (
            obj.jobs.aggregate(total=Sum('part__weight'))['total'] or 0
        )
