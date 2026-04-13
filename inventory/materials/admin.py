from django.contrib import admin
from django.utils.html import format_html
from django.db.models import Count, Q
from .models import Material, CoilPart, ProductType, ProcessStep, ProductionJob, StepLog


# ── Inline steps inside ProductType ─────────────────────────

class ProcessStepInline(admin.TabularInline):
    model = ProcessStep
    extra = 3
    ordering = ['order']


# ── ProductType ──────────────────────────────────────────────

@admin.register(ProductType)
class ProductTypeAdmin(admin.ModelAdmin):
    inlines = [ProcessStepInline]
    list_display = ['name', 'step_count']

    def step_count(self, obj):
        return obj.steps.count()
    step_count.short_description = 'Steps'


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
    actions = ['mark_completed', 'mark_on_hold']

    def mark_completed(self, request, queryset):
        queryset.update(status='completed')
    mark_completed.short_description = 'Mark selected jobs as completed'

    def mark_on_hold(self, request, queryset):
        queryset.update(status='on_hold')
    mark_on_hold.short_description = 'Mark selected jobs as on hold'
    def coil_link(self, obj):
        coil = obj.part.coil
        return format_html(
            '<a href="/admin/materials/material/{}/change/">{}</a>',
            coil.pk,
            coil.formatted_coil()
        )
    coil_link.short_description = 'Coil'

    def part_link(self, obj):
        return format_html(
            '<a href="/admin/materials/coilpart/{}/change/">{}</a>',
            obj.part.pk,
            obj.part.part_no
        )
    part_link.short_description = 'Part'

    def progress_bar(self, obj):
        total_steps = obj.product_type.steps.count()
        if total_steps == 0:
            return '—'

        completed = 0
        in_progress = 0

        for step in obj.product_type.steps.all():
            latest = obj.step_logs.filter(step=step).order_by('-timestamp').first()
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


admin.site.register(Material)