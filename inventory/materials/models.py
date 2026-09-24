import uuid
from decimal import Decimal

from django.db import models
from django.contrib.auth.models import User
from django.db.models.functions import Coalesce
from django.db.models.signals import post_delete
from django.dispatch import receiver
from django.utils import timezone


class GateEntry(models.Model):
    """One truck's delivery, logged from its invoice before any coil is
    individually registered. `vendor` is the raw-material supplier who
    delivered the truck — one per delivery. A single truck can carry a
    mixed load of different coil brands though, so `company` (the brand of
    the coil, e.g. the mill) lives on GateEntryLot instead, not here.
    total_weight is the invoice figure for the *entire* delivery, used only
    to compute a rough average weight per coil across every lot."""
    date = models.DateField(default=timezone.now)
    vendor = models.CharField(max_length=50, null=True, blank=True)
    vehicle_no = models.CharField(max_length=20, null=True, blank=True)
    invoice_no = models.CharField(max_length=30, null=True, blank=True)
    total_weight = models.DecimalField(max_digits=10, decimal_places=3)
    created_at = models.DateTimeField(auto_now_add=True)

    def no_of_coils(self):
        return self.lots.aggregate(total=models.Sum('no_of_coils'))['total'] or 0

    def weight_per_coil(self):
        """Fixed invoice weight per coil — total_weight split evenly across
        every lot's coils. A rough average only: real coils vary, more so
        when a delivery mixes multiple grades/sizes across lots. Each coil's
        real weight is entered separately when it's actually registered."""
        n = self.no_of_coils()
        if not n:
            return Decimal('0')
        # total_weight may still be a plain int/float in memory (e.g. right
        # after .create(), before a DB round-trip coerces it to Decimal) —
        # str() first avoids both AttributeError and float-binary imprecision.
        return (Decimal(str(self.total_weight)) / n).quantize(Decimal('0.001'))

    def coils_registered(self):
        return sum(lot.coils_registered() for lot in self.lots.all())

    def coils_remaining(self):
        return max(self.no_of_coils() - self.coils_registered(), 0)

    def is_complete(self):
        return self.lots.exists() and self.coils_remaining() <= 0

    def __str__(self):
        return f"Gate Entry #{self.pk} — {self.vehicle_no or 'no vehicle no.'}"


class GateEntryLot(models.Model):
    """One brand/grade/size batch within a gate entry — e.g. 'lot 1: 3 coils
    of Tata Steel EN8D 1.2mm', 'lot 2: 2 coils of JSW SAE1008 6mm', both
    delivered on the same truck by the same vendor (see GateEntry.vendor).
    `company` (the coil's brand/mill) lives here rather than on GateEntry —
    a single delivery can carry more than one brand."""
    gate_entry = models.ForeignKey(GateEntry, on_delete=models.CASCADE, related_name='lots')
    company = models.CharField(max_length=100, null=True, blank=True)
    grade = models.CharField(max_length=10, null=True, blank=True)
    size = models.DecimalField(max_digits=10, decimal_places=3, null=True, blank=True)
    no_of_coils = models.PositiveIntegerField()

    def coils_registered(self):
        return self.coils.count()

    def coils_remaining(self):
        return max(self.no_of_coils - self.coils_registered(), 0)

    def is_complete(self):
        return self.coils_remaining() <= 0

    def __str__(self):
        return f"{self.gate_entry} — {self.grade}, {self.size} mm ({self.no_of_coils} coils)"


class Material(models.Model):
    coil_no = models.AutoField(primary_key=True)
    lot = models.ForeignKey(
        GateEntryLot, on_delete=models.PROTECT, null=True, blank=True, related_name='coils',
        help_text="The gate entry lot (grade/size batch within a truck delivery) this coil "
                  "was physically part of. Null for coils that predate gate entries "
                  "(imported/historical data).",
    )
    invoice_weight = models.DecimalField(
        max_digits=10, decimal_places=3, null=True, blank=True,
        help_text="Fixed per-coil weight from the gate entry's invoice (total_weight ÷ "
                  "total coils across all lots) — a rough average, not a measurement. "
                  "`quantity` below is the actual measured weight and is what all usage "
                  "is computed from.",
    )
    date = models.DateField("receipt date", null=True, blank=True)
    grade = models.CharField(max_length=10, null=True, blank=True)
    size = models.DecimalField(max_digits=10, decimal_places=3, null=True, blank=True)
    company = models.CharField(max_length=100, null=True, blank=True)
    vendor = models.CharField(max_length=50, null=True, blank=True)
    quantity = models.DecimalField(max_digits=10, decimal_places=3, null=True, blank=True)
    heat_no = models.CharField(max_length=8, null=True, blank=True)
    archived_at = models.DateTimeField(null=True, blank=True, db_index=True)
    legacy_used_weight = models.DecimalField(
        max_digits=10, decimal_places=3, default=0, blank=True,
        help_text="Weight already issued before this coil was tracked in the app — "
                  "imported from the spreadsheet's ISSUED QTY columns. Added to weight "
                  "cut via the app to compute total usage.",
    )

    def formatted_coil(self):
        return f"COIL{self.coil_no:04d}"

    def weight_used(self):
        """Total weight used: weight allocated to orders via picks, plus
        legacy_used_weight for usage that happened before this coil was
        tracked in the app. Single source of truth — admin, the REST API,
        and the coil-picking flow all call this rather than each computing
        their own aggregate.

        If the caller prefetched `order_picks` (e.g. .prefetch_related on a
        list), sum over the already-fetched rows instead of issuing a fresh
        aggregate query per coil — .aggregate() always hits the DB, bypassing
        the prefetch cache, which turned every coil list into an N+1."""
        if 'order_picks' in getattr(self, '_prefetched_objects_cache', {}):
            picks_total = sum((p.weight_allocated or Decimal('0') for p in self.order_picks.all()), Decimal('0'))
        else:
            picks_total = self.order_picks.aggregate(total=models.Sum('weight_allocated'))['total'] or Decimal('0')
        return picks_total + self.legacy_used_weight

    def weight_remaining(self):
        if not self.quantity:
            return 0
        return self.quantity - self.weight_used()

    def is_used_up(self):
        """A coil with no quantity on file is neither used nor unused — just unknown."""
        if not self.quantity:
            return False
        return self.weight_remaining() <= 0

    def is_archived(self):
        return self.archived_at is not None

    def __str__(self):
        return self.formatted_coil()


class GradeOption(models.Model):
    name = models.CharField(max_length=20, unique=True)
    class Meta:
        ordering = ['name']
    def __str__(self):
        return self.name


class SizeOption(models.Model):
    value = models.DecimalField(max_digits=10, decimal_places=3, unique=True)
    class Meta:
        ordering = ['value']
    def __str__(self):
        return f"{self.value} mm"


class ProductType(models.Model):
    """e.g. 'EN8D Bar 2.5mm' — defines the final product, its preset grade/size, and which steps apply.

    A grade/size combination identifies exactly one product type — the two
    can't be reused across different product types.
    """
    item_code   = models.CharField(max_length=100, verbose_name="Item Code")
    grade       = models.CharField(max_length=20, blank=True, verbose_name="Grade")
    size        = models.DecimalField(max_digits=10, decimal_places=3, null=True, blank=True, verbose_name="Size (mm)")
    description = models.TextField(blank=True)

    class Meta:
        unique_together = ['grade', 'size']

    def __str__(self):
        return self.item_code


class AllowedCoilSpec(models.Model):
    """Coil grades/sizes the admin approves for a given product type."""
    product_type = models.ForeignKey(ProductType, on_delete=models.CASCADE, related_name='allowed_specs')
    grade = models.CharField(max_length=10, blank=True, verbose_name="Grade")
    size  = models.DecimalField(max_digits=10, decimal_places=3, null=True, blank=True, verbose_name="Size (mm)")
    raw_material_ratio = models.DecimalField(
        max_digits=6, decimal_places=3, default=Decimal('1.000'),
        verbose_name="Raw material ratio",
        help_text="kg of this raw material needed to produce 1 kg of finished "
                  "product (e.g. 1.100 = 10% wastage). Used to convert an "
                  "order's required quantity into how much of this raw "
                  "material needs to be picked.",
    )
    notes = models.CharField(max_length=100, blank=True)

    def __str__(self):
        parts = []
        if self.grade: parts.append(self.grade)
        if self.size:  parts.append(f"{self.size} mm")
        return f"{self.product_type.item_code} — {' / '.join(parts) or 'Any'}"


class ProcessStep(models.Model):
    """A named step belonging to a product type, with a defined order."""
    product_type = models.ForeignKey(ProductType, on_delete=models.CASCADE, related_name='steps')
    name = models.CharField(max_length=100)   # e.g. "Blanking", "Forming", "Heat treat"
    order = models.PositiveIntegerField()      # 1, 2, 3 ...

    class Meta:
        ordering = ['order']
        unique_together = ['product_type', 'order']

    def __str__(self):
        return f"{self.product_type.item_code} — Step {self.order}: {self.name}"


class OrderCoilPick(models.Model):
    """One coil scanned/picked against an order's raw-material requirement.
    A coil isn't necessarily fully consumed by one order — weight_allocated
    can be less than the coil's full remaining weight, leaving the rest
    available for other orders, the same way coil weight tracking already
    works. Material.weight_used() sums these the same way it used to sum
    cut-part weights."""
    order = models.ForeignKey('Order', on_delete=models.CASCADE, related_name='coil_picks')
    coil = models.ForeignKey(Material, on_delete=models.PROTECT, related_name='order_picks')
    weight_allocated = models.DecimalField(max_digits=10, decimal_places=3)
    picked_at = models.DateTimeField(auto_now_add=True)

    def output_equivalent(self):
        """weight_allocated converted into finished-product terms, using the
        raw_material_ratio of whichever AllowedCoilSpec matches this coil's
        grade/size under the order's product type. Falls back to 1:1 if no
        spec matches (e.g. the product type has no specs configured)."""
        ratio = Decimal('1')
        if self.order.product_type:
            for spec in self.order.product_type.allowed_specs.all():
                grade_matches = not spec.grade or spec.grade.lower() == (self.coil.grade or '').lower()
                size_matches = not spec.size or spec.size == self.coil.size
                if grade_matches and size_matches:
                    ratio = spec.raw_material_ratio
                    break
        return self.weight_allocated / ratio

    def __str__(self):
        return f"{self.coil.formatted_coil()} → {self.order}"


class ProductionJob(models.Model):
    """Links a picked coil to a product type and tracks overall status."""
    STATUS_CHOICES = [
        ('pending',     'Pending'),
        ('in_progress', 'In Progress'),
        ('on_hold',     'On Hold'),
        ('completed',   'Completed'),
    ]

    pick         = models.ForeignKey(OrderCoilPick, on_delete=models.CASCADE, related_name='jobs')
    product_type = models.ForeignKey(ProductType, on_delete=models.PROTECT)
    order        = models.ForeignKey('Order', on_delete=models.CASCADE, related_name='jobs')
    job_no       = models.CharField(max_length=30, unique=True)   # e.g. JOB-0001
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending', db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    notes = models.TextField(blank=True)

    def __str__(self):
        return self.job_no

    def current_step(self):
        """Returns the latest StepLog entry for this job."""
        return self.step_logs.order_by('-timestamp').first()

    def recalculate_status(self):
        """Recomputes overall status from the latest StepLog per step and
        saves it. A failed step needs attention, so it takes priority over
        everything else — otherwise a step marked 'failed' (only possible via
        admin; the employee portal only ever logs 'in_progress'/'completed')
        would leave the job silently looking pending/in-progress forever.
        Single source of truth — called from the employee step-update view
        and from StepLogAdmin whenever a StepLog is added, changed, or
        deleted, so this stays correct regardless of where a log came from."""
        steps = list(self.product_type.steps.all())
        latest_by_step = {
            step.id: self.step_logs.filter(step=step).order_by('-timestamp').first()
            for step in steps
        }
        latest_statuses = [log.status for log in latest_by_step.values() if log]

        if 'failed' in latest_statuses:
            self.status = 'on_hold'
        elif steps and all(latest_by_step.get(step.id) and latest_by_step[step.id].status == 'completed'
                            for step in steps):
            self.status = 'completed'
        elif 'in_progress' in latest_statuses:
            self.status = 'in_progress'
        else:
            self.status = 'pending'
        self.save(update_fields=['status'])


class StepLog(models.Model):
    """Each time a step status changes, a row is written here."""
    STATUS_CHOICES = [
        ('pending',     'Pending'),
        ('in_progress', 'In Progress'),
        ('completed',   'Completed'),
        ('failed',      'Failed'),
    ]

    job = models.ForeignKey(ProductionJob, on_delete=models.CASCADE, related_name='step_logs')
    step = models.ForeignKey(ProcessStep, on_delete=models.PROTECT)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    updated_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True)
    timestamp = models.DateTimeField(auto_now_add=True)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ['-timestamp']

    def __str__(self):
        return f"{self.job.job_no} | {self.step.name} | {self.status}"


class Customer(models.Model):
    name        = models.CharField(max_length=100, unique=True)
    email       = models.EmailField(blank=True)
    phone       = models.CharField(max_length=20, blank=True)
    quote_token = models.UUIDField(default=uuid.uuid4, unique=True)
    created_at  = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name


class Query(models.Model):
    """An inbound sales inquiry, logged before any quote/order exists —
    phone calls, referrals, IndiaMART messages, WhatsApp, etc. Staff decide
    which ones to pursue by sending a quote; the quote form the customer
    eventually fills is pre-populated with whatever was already captured
    here. `company_name` is often blank at creation time — a manually
    logged query starts as just a phone number, and a WhatsApp-sourced one
    (see `materials/views.py::whatsapp_webhook`) only has a name if Meta's
    payload included one — it gets filled in later (via the admin) once
    staff actually know who they're talking to."""
    SOURCE_CHOICES = [
        ('call', 'Phone Call'),
        ('referral', 'Referral'),
        ('indiamart', 'IndiaMART'),
        ('whatsapp', 'WhatsApp'),
        ('other', 'Other'),
    ]
    STATUS_CHOICES = [
        ('new', 'New'),
        ('quote_sent', 'Quote Sent'),
        ('converted', 'Converted'),
        ('not_interested', 'Not Interested'),
    ]
    source        = models.CharField(max_length=20, choices=SOURCE_CHOICES)
    company_name  = models.CharField(max_length=100, blank=True)
    contact_phone = models.CharField(max_length=20, blank=True)
    contact_email = models.EmailField(blank=True)
    product_type  = models.ForeignKey(ProductType, on_delete=models.SET_NULL, null=True, blank=True)
    grade         = models.CharField(max_length=100, blank=True)
    size          = models.DecimalField(max_digits=10, decimal_places=3, null=True, blank=True)
    quantity      = models.DecimalField(max_digits=10, decimal_places=3, null=True, blank=True)
    notes         = models.TextField(blank=True)
    status        = models.CharField(max_length=20, choices=STATUS_CHOICES, default='new', db_index=True)
    # Set only once a quote is actually sent — before that, a query is just
    # free-standing prospect info, not yet a Customer record (mirrors how
    # Customer itself is created on-demand in quick_send_quote).
    customer      = models.ForeignKey(Customer, on_delete=models.SET_NULL, null=True, blank=True, related_name='queries')
    created_at    = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        label = self.company_name or self.contact_phone or f"Query #{self.pk}"
        return f"{label} — {self.get_source_display()}"


class Quotation(models.Model):
    """A record of an official per-kg rate quotation actually sent to a
    customer — created every time Send Quote fires, regardless of which
    entry point triggered it (query_send_quote, send_quote_email,
    quick_send_quote all funnel through _dispatch_quote_email). Immutable
    once created — correcting a rate means sending a new quotation, not
    editing history, the same way Order itself is never silently rewritten."""
    customer      = models.ForeignKey(Customer, on_delete=models.CASCADE, related_name='quotations')
    source_query  = models.ForeignKey(Query, on_delete=models.SET_NULL, null=True, blank=True, related_name='quotations')
    quotation_no  = models.PositiveIntegerField(unique=True, editable=False, null=True)
    rate_per_kg   = models.DecimalField(max_digits=10, decimal_places=2)
    product_type  = models.ForeignKey(ProductType, on_delete=models.SET_NULL, null=True, blank=True)
    grade         = models.CharField(max_length=100, blank=True)
    size          = models.DecimalField(max_digits=10, decimal_places=3, null=True, blank=True)
    created_at    = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def save(self, *args, **kwargs):
        if self.quotation_no is None:
            max_no = Quotation.objects.aggregate(models.Max('quotation_no'))['quotation_no__max'] or 0
            self.quotation_no = max_no + 1
        super().save(*args, **kwargs)

    def formatted_no(self):
        return f"QUO-{self.quotation_no:04d}"

    def __str__(self):
        return f"{self.formatted_no()} — {self.customer.name}"


class Order(models.Model):
    STATUS_CHOICES = [
        ('pending',       'Pending'),
        ('confirmed',     'Confirmed'),
        ('in_production', 'In Production'),
        ('completed',     'Completed'),
        ('cancelled',     'Cancelled'),
    ]
    DELIVERY_FORM_CHOICES = [
        ('coil', 'Coil'),
        ('bar',  'Bar'),
    ]
    FREQUENCY_CHOICES = [
        ('one_time',   'One Time'),
        ('monthly',    'Monthly'),
        ('quarterly',  'Quarterly'),
        ('as_required','As Required'),
    ]

    order_no = models.PositiveIntegerField(
        unique=True, editable=False, null=True,
        help_text="Displayed as ORD-####. Assigned sequentially on creation and "
                  "kept gap-free — deleting an order renumbers every order after "
                  "it down by one (see the post_delete receiver below), unlike "
                  "coil_no/job_no which are never reused.",
    )
    customer              = models.ForeignKey(Customer, on_delete=models.PROTECT, related_name='orders')
    source_query          = models.ForeignKey(Query, on_delete=models.SET_NULL, null=True, blank=True, related_name='orders')
    product_type          = models.ForeignKey(ProductType, on_delete=models.SET_NULL, null=True, blank=True, related_name='orders', verbose_name="Product Type")
    # 1. Drawing / dimensions
    drawing_dimensions    = models.TextField(blank=True, verbose_name="Drawing / Dimensions")
    # 2. Grade & size (autofilled from product type, editable)
    grade                 = models.CharField(max_length=100, blank=True, verbose_name="Grade of Material")
    size                  = models.DecimalField(max_digits=10, decimal_places=3, null=True, blank=True, verbose_name="Size (mm)")
    # 3. Mill make
    mill_make             = models.CharField(max_length=100, blank=True, verbose_name="Specific Mill Make")
    # 4. Mechanical properties
    mechanical_properties = models.TextField(blank=True, verbose_name="Mechanical Properties")
    # 5. Processes
    processes             = models.TextField(blank=True, verbose_name="Processes (drilling, tapping, etc.)")
    # 6. End usage
    end_usage             = models.TextField(blank=True, verbose_name="End Usage / Application")
    # 7. Delivery form
    delivery_form         = models.CharField(max_length=10, choices=DELIVERY_FORM_CHOICES, blank=True, verbose_name="Delivery Form")
    # 8. Quantity
    quantity              = models.DecimalField(max_digits=10, decimal_places=3, verbose_name="Required Quantity (kg)")
    # 9. Frequency
    frequency             = models.CharField(max_length=20, choices=FREQUENCY_CHOICES, blank=True, verbose_name="Frequency")

    delivery_date = models.DateField(null=True, blank=True)
    notes         = models.TextField(blank=True)
    purchase_order = models.FileField(
        upload_to='purchase_orders/%Y/%m/', blank=True, null=True,
        help_text="Customer's own PO, if they attached one when filling the quote form.",
    )
    status        = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending', db_index=True)
    created_at    = models.DateTimeField(auto_now_add=True)

    def save(self, *args, **kwargs):
        if self.order_no is None:
            max_no = Order.objects.aggregate(models.Max('order_no'))['order_no__max'] or 0
            self.order_no = max_no + 1
        super().save(*args, **kwargs)

    def __str__(self):
        return f"ORD-{self.order_no:04d} | {self.customer.name}"

    def picked_output_weight(self):
        """Sum of every coil pick's output_equivalent() — how much of this
        order's required quantity has been covered so far."""
        return sum((pick.output_equivalent() for pick in self.coil_picks.all()), Decimal('0'))

    def is_fully_picked(self):
        return self.picked_output_weight() >= self.quantity

    def available_raw_material_output(self):
        """Total finished-product output that could be made right now from
        in-stock raw material matching this order's product type — summed
        across every AllowedCoilSpec (grade/size + ratio), or any non-
        archived coil with remaining weight if none are configured (same
        wildcard fallback _coil_matches_order_specs uses in views.py).
        None if there's no product type yet to check against. Used to warn
        an admin confirming an order that raw material may need reordering
        before production can actually happen."""
        if not self.product_type:
            return None
        specs = list(self.product_type.allowed_specs.all())
        total = Decimal('0')
        for spec in (specs or [None]):  # None = wildcard, matches any coil
            coils_qs = Material.objects.filter(archived_at__isnull=True)
            ratio = Decimal('1')
            if spec is not None:
                if spec.grade:
                    coils_qs = coils_qs.filter(grade__iexact=spec.grade)
                if spec.size:
                    coils_qs = coils_qs.filter(size=spec.size)
                ratio = spec.raw_material_ratio
            agg = coils_qs.annotate(
                _weight_used=Coalesce(models.Sum('order_picks__weight_allocated'), models.Value(Decimal('0')), output_field=models.DecimalField())
                             + models.F('legacy_used_weight'),
            ).aggregate(
                total_remaining=models.Sum(models.F('quantity') - models.F('_weight_used')),
            )
            remaining = max(agg['total_remaining'] or Decimal('0'), Decimal('0'))
            total += remaining / ratio
        return total

    def has_sufficient_raw_material(self):
        """None if there's no product type set yet to check stock against."""
        available = self.available_raw_material_output()
        if available is None:
            return None
        return available >= self.quantity


@receiver(post_delete, sender=Order)
def _close_order_number_gap(sender, instance, **kwargs):
    """Deleting an order (a mistaken entry, test data, etc.) must not leave a
    permanent hole in the numbering — unlike coil_no/job_no, which are never
    reused because they may already be on a physical tag, an order number is
    just an internal reference nobody prints ahead of time. Renumbers every
    remaining order sequentially from 1, so gaps never persist.

    Processing in ascending order_no and assigning targets 1, 2, 3... is safe
    against the unique constraint: each target slot was either the original
    gap or was just vacated by the previous row in this same loop, so it's
    always free by the time it's claimed. Runs once per deleted row even in a
    bulk delete — redundant but harmless at this app's order volumes."""
    for i, order in enumerate(Order.objects.order_by('order_no'), start=1):
        if order.order_no != i:
            Order.objects.filter(pk=order.pk).update(order_no=i)