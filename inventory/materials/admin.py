from django.contrib import admin
from django.utils.html import format_html
from django.urls import reverse
from django.db.models import Sum
from .models import Material, CoilPart, ProductType, AllowedCoilSpec, ProcessStep, ProductionJob, StepLog, Customer, Order


# ── Inline steps inside ProductType ─────────────────────────

class ProcessStepInline(admin.TabularInline):
    model = ProcessStep
    extra = 3
    ordering = ['order']


class AllowedCoilSpecInline(admin.TabularInline):
    model = AllowedCoilSpec
    extra = 2
    fields = ['grade', 'size', 'notes']
    verbose_name = "Allowed Coil Spec"
    verbose_name_plural = "Allowed Coil Specs (leave empty to allow all coils)"


# ── ProductType ──────────────────────────────────────────────

@admin.register(ProductType)
class ProductTypeAdmin(admin.ModelAdmin):
    inlines = [ProcessStepInline, AllowedCoilSpecInline]
    list_display = ['name', 'grade', 'size', 'step_count', 'allowed_spec_summary']
    fields = ['name', 'grade', 'size', 'description']

    def step_count(self, obj):
        return obj.steps.count()
    step_count.short_description = 'Steps'

    def allowed_spec_summary(self, obj):
        specs = obj.allowed_specs.all()
        if not specs:
            return '— any coil —'
        return ', '.join(str(s) for s in specs)
    allowed_spec_summary.short_description = 'Allowed Coils'


# ── CoilPart ─────────────────────────────────────────────────

@admin.register(CoilPart)
class CoilPartAdmin(admin.ModelAdmin):
    list_display = ['part_no', 'coil', 'weight', 'length', 'cut_date', 'job_count']
    list_filter = ['cut_date']
    search_fields = ['part_no', 'coil__coil_no']

    def job_count(self, obj):
        return obj.jobs.count()
    job_count.short_description = 'Jobs'


# ── StepLog inline inside ProductionJob ─────────────────────

class StepLogInline(admin.TabularInline):
    model = StepLog
    extra = 0
    readonly_fields = ['step', 'status', 'updated_by', 'timestamp', 'notes']
    can_delete = False
    ordering = ['-timestamp']
    max_num = 0  # no adding via inline — only through the job_detail view

    def has_add_permission(self, request, obj=None):
        return False


# ── ProductionJob ────────────────────────────────────────────

@admin.register(ProductionJob)
class ProductionJobAdmin(admin.ModelAdmin):
    list_display = [
        'job_no',
        'coil_link',
        'part_link',
        'product_type',
        'progress_bar',
        'status_badge',
        'created_at',
    ]
    list_filter  = ['status', 'product_type', 'created_at']
    search_fields = ['job_no', 'part__part_no', 'part__coil__coil_no']
    readonly_fields = ['job_no', 'progress_bar', 'status_badge', 'created_at', 'updated_at']
    inlines = [StepLogInline]

    # ── Custom columns ───────────────────────────────────────
    def get_queryset(self, request):
        return super().get_queryset(request).prefetch_related(
            'step_logs', 'product_type__steps'
        )

    actions = ['mark_completed', 'mark_on_hold']

    def mark_completed(self, request, queryset):
        queryset.update(status='completed')
    mark_completed.short_description = 'Mark selected jobs as completed'

    def mark_on_hold(self, request, queryset):
        queryset.update(status='on_hold')
    mark_on_hold.short_description = 'Mark selected jobs as on hold'
    def coil_link(self, obj):
        coil = obj.part.coil
        url = reverse('admin:materials_material_change', args=[coil.pk])
        return format_html('<a href="{}">{}</a>', url, coil.formatted_coil())
    coil_link.short_description = 'Coil'

    def part_link(self, obj):
        url = reverse('admin:materials_coilpart_change', args=[obj.part.pk])
        return format_html('<a href="{}">{}</a>', url, obj.part.part_no)
    part_link.short_description = 'Part'

    def progress_bar(self, obj):
        steps = list(obj.product_type.steps.all())  # uses prefetch
        total_steps = len(steps)
        if total_steps == 0:
            return '—'

        # Group prefetched logs by step, newest first
        logs_by_step = {}
        for log in sorted(obj.step_logs.all(), key=lambda l: l.timestamp, reverse=True):
            logs_by_step.setdefault(log.step_id, log)

        completed = 0
        in_progress = 0
        for step in steps:
            latest = logs_by_step.get(step.id)
            if latest:
                if latest.status == 'completed':
                    completed += 1
                elif latest.status == 'in_progress':
                    in_progress += 1

        completed_pct  = int((completed / total_steps) * 100)
        inprogress_pct = int((in_progress / total_steps) * 100)

        return format_html(
            '''
            <div style="width:180px;">
              <div style="
                background:#e5e7eb;
                border-radius:999px;
                height:10px;
                overflow:hidden;
                display:flex;
              ">
                <div style="width:{}%; background:#16a34a; height:100%;"></div>
                <div style="width:{}%; background:#f59e0b; height:100%;"></div>
              </div>
              <div style="font-size:11px; color:#6b7280; margin-top:3px;">
                {}/{} steps done
              </div>
            </div>
            ''',
            completed_pct,
            inprogress_pct,
            completed,
            total_steps,
        )
    progress_bar.short_description = 'Progress'

    def status_badge(self, obj):
        colors = {
            'pending':     ('#fef3c7', '#92400e'),
            'in_progress': ('#dbeafe', '#1e40af'),
            'on_hold':     ('#fee2e2', '#991b1b'),
            'completed':   ('#dcfce7', '#166534'),
        }
        bg, text = colors.get(obj.status, ('#f3f4f6', '#374151'))
        return format_html(
            '<span style="'
            'background:{};color:{};'
            'padding:3px 10px;border-radius:999px;'
            'font-size:12px;font-weight:600;'
            '">{}</span>',
            bg, text,
            obj.get_status_display()
        )
    status_badge.short_description = 'Status'

    # ── Allow status to be changed directly from the detail page ──

    fieldsets = (
        ('Job info', {
            'fields': ('job_no', 'part', 'product_type', 'created_at', 'updated_at')
        }),
        ('Status', {
            'fields': ('status', 'progress_bar', 'notes')
        }),
    )


# ── StepLog — standalone so admin can see full history ───────

@admin.register(StepLog)
class StepLogAdmin(admin.ModelAdmin):
    list_display  = ['job', 'step', 'status_badge', 'updated_by', 'timestamp', 'notes']
    list_filter   = ['status', 'step__product_type', 'updated_by']
    search_fields = ['job__job_no', 'step__name']
    readonly_fields = ['timestamp']

    def status_badge(self, obj):
        colors = {
            'pending':     ('#fef3c7', '#92400e'),
            'in_progress': ('#dbeafe', '#1e40af'),
            'completed':   ('#dcfce7', '#166534'),
            'failed':      ('#fee2e2', '#991b1b'),
        }
        bg, text = colors.get(obj.status, ('#f3f4f6', '#374151'))
        return format_html(
            '<span style="background:{};color:{};padding:3px 10px;'
            'border-radius:999px;font-size:12px;font-weight:600;">{}</span>',
            bg, text, obj.get_status_display()
        )
    status_badge.short_description = 'Status'


@admin.register(Material)
class MaterialAdmin(admin.ModelAdmin):
    list_display = [
        'formatted_coil', 'date', 'grade', 'size',
        'company', 'vendor', 'quantity', 'heat_no',
        'parts_count', 'weight_remaining',
    ]
    list_filter   = ['grade', 'size', 'company']
    search_fields = ['coil_no', 'heat_no', 'vendor', 'company']
    ordering = ['-coil_no']

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(
            weight_used=Sum('parts__weight'),
            parts_total=Sum('parts__weight', distinct=False),
        ).prefetch_related('parts')

    def parts_count(self, obj):
        return obj.parts.count()
    parts_count.short_description = 'Parts'

    def weight_remaining(self, obj):
        if not obj.quantity:
            return '—'
        used = float(obj.weight_used or 0)
        remaining = float(obj.quantity) - used
        color = '#dc2626' if remaining <= 0 else '#166534'
        return format_html(
            '<span style="color:{}; font-weight:600;">{} kg</span>',
            color, f'{remaining:.1f}',
        )
    weight_remaining.short_description = 'Remaining'


# ── Customer & Order ─────────────────────────────────────────

class OrderInline(admin.TabularInline):
    model = Order
    extra = 0
    readonly_fields = ['created_at']
    fields = ['grade', 'quantity', 'delivery_form', 'frequency', 'delivery_date', 'status', 'created_at']


@admin.register(Customer)
class CustomerAdmin(admin.ModelAdmin):
    list_display  = ['name', 'email', 'phone', 'order_count', 'created_at']
    search_fields = ['name', 'email', 'phone']
    inlines       = [OrderInline]

    def order_count(self, obj):
        return obj.orders.count()
    order_count.short_description = 'Orders'


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display  = ['order_no', 'customer', 'grade', 'quantity', 'delivery_form', 'frequency', 'delivery_date', 'status_badge', 'created_at']
    list_filter   = ['status', 'delivery_form', 'frequency', 'customer']
    search_fields = ['customer__name', 'grade', 'mill_make']
    ordering      = ['-created_at']
    fieldsets = (
        ('Order Info', {
            'fields': ('customer', 'product_type', 'status', 'delivery_date', 'frequency', 'notes')
        }),
        ('Material Requirements', {
            'fields': ('grade', 'size', 'mill_make', 'drawing_dimensions', 'mechanical_properties', 'processes', 'end_usage')
        }),
        ('Quantity & Delivery', {
            'fields': ('quantity', 'delivery_form')
        }),
    )

    def order_no(self, obj):
        return f'ORD-{obj.pk:04d}'
    order_no.short_description = 'Order #'

    def status_badge(self, obj):
        colors = {
            'pending':       ('#fef3c7', '#92400e'),
            'confirmed':     ('#dbeafe', '#1e40af'),
            'in_production': ('#d1fae5', '#065f46'),
            'completed':     ('#dcfce7', '#166534'),
            'cancelled':     ('#fee2e2', '#991b1b'),
        }
        bg, text = colors.get(obj.status, ('#f3f4f6', '#374151'))
        return format_html(
            '<span style="background:{};color:{};padding:3px 10px;'
            'border-radius:999px;font-size:12px;font-weight:600;">{}</span>',
            bg, text, obj.get_status_display()
        )
    status_badge.short_description = 'Status'