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
    """A coil, with remaining weight computed the same way MaterialAdmin does."""
    coil_no_formatted = serializers.CharField(source='formatted_coil', read_only=True)
    weight_used = serializers.SerializerMethodField()
    weight_remaining = serializers.SerializerMethodField()

    class Meta:
        model = Material
        fields = [
            'coil_no', 'coil_no_formatted', 'date', 'grade', 'size', 'company',
            'vendor', 'quantity', 'heat_no', 'weight_used', 'weight_remaining',
        ]

    def get_weight_used(self, obj):
        return obj.parts.aggregate(total=Sum('weight'))['total'] or 0

    def get_weight_remaining(self, obj):
        used = self.get_weight_used(obj)
        return (obj.quantity or 0) - used


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
        return obj.jobs.aggregate(total=Sum('part__weight'))['total'] or 0
