from django.contrib import admin
from django.utils import timezone
from django.utils.html import format_html
from django.urls import reverse
from decimal import Decimal

from django.db.models import DecimalField, F, Q, Sum, Value
from django.db.models.functions import Coalesce
from .models import GateEntry, GateEntryLot, Material, OrderCoilPick, GradeOption, SizeOption, ProductType, AllowedCoilSpec, ProcessStep, ProductionJob, StepLog, Customer, Query, Order


class GateEntryLotInline(admin.TabularInline):
    model = GateEntryLot
    extra = 0
    readonly_fields = ['company', 'grade', 'size', 'no_of_coils']

    def has_add_permission(self, request, obj=None):
        return False  # lots are added via the employee portal, one at a time


@admin.register(GateEntry)
class GateEntryAdmin(admin.ModelAdmin):
    list_display = [
        'id', 'date', 'vehicle_no', 'vendor', 'invoice_no',
        'total_weight', 'no_of_coils', 'weight_per_coil', 'coils_registered', 'status_badge',
    ]
    list_filter = ['vendor']
    search_fields = ['vehicle_no', 'vendor', 'invoice_no']
    ordering = ['-created_at']
    inlines = [GateEntryLotInline]

    def status_badge(self, obj):
        if obj.is_complete():
            bg, text, label = '#dcfce7', '#166534', 'Complete'
        else:
            bg, text, label = '#fef3c7', '#92400e', f'{obj.coils_remaining()} left'
        return format_html(
            '<span style="background:{};color:{};padding:2px 10px;'
            'border-radius:999px;font-size:12px;font-weight:600;">{}</span>',
            bg, text, label,
        )
    status_badge.short_description = 'Status'


@admin.register(GateEntryLot)
class GateEntryLotAdmin(admin.ModelAdmin):
    list_display = ['id', 'gate_entry', 'company', 'grade', 'size', 'no_of_coils', 'coils_registered', 'status_badge']
    list_filter = ['grade', 'size']
    search_fields = ['gate_entry__vehicle_no', 'gate_entry__vendor', 'company']

    def status_badge(self, obj):
        if obj.is_complete():
            bg, text, label = '#dcfce7', '#166534', 'Complete'
        else:
            bg, text, label = '#fef3c7', '#92400e', f'{obj.coils_remaining()} left'
        return format_html(
            '<span style="background:{};color:{};padding:2px 10px;'
            'border-radius:999px;font-size:12px;font-weight:600;">{}</span>',
            bg, text, label,
        )
    status_badge.short_description = 'Status'


@admin.register(GradeOption)
class GradeOptionAdmin(admin.ModelAdmin):
    list_display = ['name']
    ordering = ['name']


@admin.register(SizeOption)
class SizeOptionAdmin(admin.ModelAdmin):
    list_display = ['value']
    ordering = ['value']


# ── Inline steps inside ProductType ─────────────────────────

class ProcessStepInline(admin.TabularInline):
    model = ProcessStep
    extra = 3
    ordering = ['order']


class AllowedCoilSpecInline(admin.TabularInline):
    model = AllowedCoilSpec
    extra = 2
    fields = ['grade', 'size', 'raw_material_ratio', 'notes']
    verbose_name = "Allowed Coil Spec"
    verbose_name_plural = "Allowed Coil Specs (leave empty to allow all coils)"


# ── ProductType ──────────────────────────────────────────────

@admin.register(ProductType)
class ProductTypeAdmin(admin.ModelAdmin):
    inlines = [ProcessStepInline, AllowedCoilSpecInline]
    list_display = ['item_code', 'grade', 'size', 'step_count', 'allowed_spec_summary']
    fields = ['item_code', 'grade', 'size', 'description']

    def step_count(self, obj):
        return obj.steps.count()
    step_count.short_description = 'Steps'

    def allowed_spec_summary(self, obj):
        specs = obj.allowed_specs.all()
        if not specs:
            return '— any coil —'
        return ', '.join(str(s) for s in specs)
    allowed_spec_summary.short_description = 'Allowed Coils'


# ── OrderCoilPick ────────────────────────────────────────────

@admin.register(OrderCoilPick)
class OrderCoilPickAdmin(admin.ModelAdmin):
    """Created only through the employee coil-picking flow — this is a
    read view of that history, not a manual-entry screen."""
    list_display = ['coil', 'order', 'weight_allocated', 'picked_at', 'job_count']
    list_filter = ['picked_at']
    search_fields = ['coil__coil_no', 'order__customer__name']

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
        'order',
        'product_type',
        'progress_bar',
        'status_badge',
        'created_at',
    ]
    list_filter  = ['status', 'product_type', 'created_at']
    search_fields = ['job_no', 'pick__coil__coil_no']
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
        coil = obj.pick.coil
        url = reverse('admin:materials_material_change', args=[coil.pk])
        return format_html('<a href="{}">{}</a>', url, coil.formatted_coil())
    coil_link.short_description = 'Coil'

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
            'fields': ('job_no', 'pick', 'order', 'product_type', 'created_at', 'updated_at')
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

    # Job status is a rollup of its StepLogs' statuses (see
    # ProductionJob.recalculate_status) — the employee portal keeps it in
    # sync on its own, but a StepLog added, changed, or deleted directly
    # here (e.g. marking a step 'failed', which only exists as an admin/API
    # concept — the portal never logs it) needs the same recalculation or
    # the job's status silently goes stale.
    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        obj.job.recalculate_status()

    def delete_model(self, request, obj):
        job = obj.job
        super().delete_model(request, obj)
        job.recalculate_status()

    def delete_queryset(self, request, queryset):
        jobs = {log.job for log in queryset}
        super().delete_queryset(request, queryset)
        for job in jobs:
            job.recalculate_status()

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


class UsedStatusFilter(admin.SimpleListFilter):
    """Filters against the _weight_used annotation MaterialAdmin already adds
    to its queryset — no per-row Python computation, just SQL."""
    title = 'used status'
    parameter_name = 'used_status'

    def lookups(self, request, model_admin):
        return [('used', 'Fully used'), ('unused', 'Has stock remaining')]

    def queryset(self, request, queryset):
        if self.value() == 'used':
            return queryset.filter(quantity__gt=0, _weight_used__gte=F('quantity'))
        if self.value() == 'unused':
            return queryset.filter(
                Q(quantity__isnull=True) | Q(quantity=0) | Q(_weight_used__lt=F('quantity'))
            )
        return queryset


class ArchivedFilter(admin.SimpleListFilter):
    """Archived coils are hidden by default so mistaken entries don't clutter
    the day-to-day list — but nothing is ever lost, just a filter click away."""
    title = 'archived'
    parameter_name = 'archived'

    def lookups(self, request, model_admin):
        return [('yes', 'Archived only'), ('all', 'All (including archived)')]

    def queryset(self, request, queryset):
        if self.value() == 'yes':
            return queryset.filter(archived_at__isnull=False)
        if self.value() == 'all':
            return queryset
        return queryset.filter(archived_at__isnull=True)


@admin.register(Material)
class MaterialAdmin(admin.ModelAdmin):
    list_display = [
        'formatted_coil', 'date', 'grade', 'size',
        'company', 'vendor', 'quantity', 'invoice_weight', 'heat_no', 'lot',
        'picks_count', 'weight_remaining', 'status_badge', 'archived_badge',
    ]
    list_filter   = ['grade', 'size', 'company', UsedStatusFilter, ArchivedFilter]
    search_fields = ['coil_no', 'heat_no', 'vendor', 'company', 'lot__gate_entry__vehicle_no']
    ordering = ['-coil_no']
    actions = ['archive_coils', 'unarchive_coils']

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(
            _weight_used=Coalesce(Sum('order_picks__weight_allocated'), Value(Decimal('0')), output_field=DecimalField())
                         + F('legacy_used_weight'),
        ).select_related('lot__gate_entry').prefetch_related('order_picks')

    def picks_count(self, obj):
        return obj.order_picks.count()
    picks_count.short_description = 'Picks'

    def weight_remaining(self, obj):
        if not obj.quantity:
            return '—'
        used = float(obj._weight_used or 0)
        remaining = float(obj.quantity) - used
        color = '#dc2626' if remaining <= 0 else '#166534'
        return format_html(
            '<span style="color:{}; font-weight:600;">{} kg</span>',
            color, f'{remaining:.1f}',
        )
    weight_remaining.short_description = 'Remaining'

    def status_badge(self, obj):
        if not obj.quantity:
            return format_html('<span style="color:#aaa;">—</span>')
        used_up = float(obj._weight_used or 0) >= float(obj.quantity)
        bg, text, label = ('#fee2e2', '#991b1b', 'Used') if used_up else ('#dcfce7', '#166534', 'Unused')
        return format_html(
            '<span style="background:{};color:{};padding:2px 10px;'
            'border-radius:999px;font-size:12px;font-weight:600;">{}</span>',
            bg, text, label,
        )
    status_badge.short_description = 'Status'

    def archived_badge(self, obj):
        if not obj.archived_at:
            return ''
        return format_html(
            '<span style="background:#f3f4f6;color:#6b7280;padding:2px 10px;'
            'border-radius:999px;font-size:12px;font-weight:600;">Archived {}</span>',
            obj.archived_at.strftime('%d %b %Y'),
        )
    archived_badge.short_description = 'Archived'

    def archive_coils(self, request, queryset):
        count = queryset.filter(archived_at__isnull=True).update(archived_at=timezone.now())
        self.message_user(request, f"Archived {count} coil(s).")
    archive_coils.short_description = "Archive selected coils"

    def unarchive_coils(self, request, queryset):
        count = queryset.filter(archived_at__isnull=False).update(archived_at=None)
        self.message_user(request, f"Unarchived {count} coil(s).")
    unarchive_coils.short_description = "Unarchive selected coils"


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


@admin.register(Query)
class QueryAdmin(admin.ModelAdmin):
    list_display  = ['display_name', 'source', 'status', 'product_type', 'created_at']
    list_filter   = ['source', 'status']
    search_fields = ['company_name', 'contact_email', 'contact_phone']
    ordering      = ['-created_at']

    @admin.display(description='Company / Contact')
    def display_name(self, obj):
        return obj.company_name or obj.contact_phone or f"Query #{obj.pk}"


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display  = ['order_number', 'customer', 'grade', 'quantity', 'delivery_form', 'frequency', 'delivery_date', 'status_badge', 'created_at']
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

    def order_number(self, obj):
        return f'ORD-{obj.order_no:04d}'
    order_number.short_description = 'Order #'

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