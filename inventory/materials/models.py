import re
import uuid
from decimal import Decimal, ROUND_HALF_UP

from django.core.exceptions import ValidationError
from django.core.validators import FileExtensionValidator, MaxValueValidator, RegexValidator
from django.db import models
from django.contrib.auth.models import User
from django.db.models.functions import Coalesce
from django.db.models.signals import post_delete
from django.dispatch import receiver
from django.utils import timezone


DRAWING_EXTENSIONS = ['pdf', 'png', 'jpg', 'jpeg', 'webp', 'dwg', 'dxf', 'step', 'stp', 'igs', 'iges', 'zip']
DRAWING_MAX_BYTES = 10 * 1024 * 1024


def validate_drawing_size(upload):
    """A customer's drawing is uploaded through the public order form, so cap its size."""
    if upload.size > DRAWING_MAX_BYTES:
        raise ValidationError(f"The drawing is too large ({upload.size // (1024 * 1024)} MB): the limit is {DRAWING_MAX_BYTES // (1024 * 1024)} MB.")


def normalize_grade(value):
    """The one spelling of a grade: capital letters and digits only — no hyphens, spaces
    or other punctuation ("EN-8D", "en 8d" and "EN8D" are all "EN8D"; "EN-8D CR" is
    "EN8DCR"). Decided 2026-10-06; every grade in the app is stored this way."""
    return re.sub(r'[^A-Za-z0-9]', '', value).upper()


class GradeField(models.CharField):
    """A CharField that always holds the normalized grade (see normalize_grade): it is
    applied when a form or model is cleaned and again whenever the row is saved, so a
    grade can't be stored with a hyphen, space or lower-case letter whichever way it
    arrives (admin, quote form, WhatsApp bot, spreadsheet import, shell)."""

    def to_python(self, value):
        value = super().to_python(value)
        return normalize_grade(value) if isinstance(value, str) else value

    def pre_save(self, model_instance, add):
        value = super().pre_save(model_instance, add)
        if isinstance(value, str):
            normalized = normalize_grade(value)
            if normalized != value:
                setattr(model_instance, self.attname, normalized)
            return normalized
        return value


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
    grade = GradeField(max_length=10, null=True, blank=True)
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
    grade = GradeField(max_length=10, null=True, blank=True)
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

    def save(self, *args, **kwargs):
        # Heat numbers are kept in capitals however they arrive (employee page, admin, import, shell).
        if self.heat_no:
            self.heat_no = self.heat_no.strip().upper()
        super().save(*args, **kwargs)

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
    name = GradeField(max_length=20, unique=True)
    # The grade's number inside product codes (EN8D = 001 -> FBB001...). Handed out
    # automatically the first time a grade is used in a code and then kept, so a code
    # already printed on a document never changes meaning; editable in the admin.
    number = models.PositiveIntegerField(
        null=True, blank=True, unique=True,
        validators=[MaxValueValidator(999)],
        help_text="Used in product codes, 1-999. Assigned automatically when the grade is first used.",
    )
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


class ProductCategory(models.Model):
    """What the business calls a *product type*: Round Bright Bar, Key Steel,
    Flat Wire, ... (the menu on its website). A product code is one of these in
    a particular grade and size. NAMING: on screen this is "Product Type"; the
    model that holds product codes is `ProductType` (an older name kept so as
    not to rename every table, field and URL) — see CLAUDE.md."""
    name     = models.CharField(max_length=100, unique=True)
    # The 3 letters this type starts every product code with (FBB = Flat Bright Bar).
    # Explicit rather than initials, because initials clash (Square vs Shaped Bright Bar).
    code     = models.CharField(
        max_length=3, unique=True, null=True,
        validators=[RegexValidator(r'^[A-Z]{3}$', 'Exactly 3 capital letters, e.g. FBB.')],
        help_text="Exactly 3 capital letters; the start of every product code of this type.",
    )
    position = models.PositiveIntegerField(default=0, help_text="Order in menus and dropdowns (lowest first).")

    class Meta:
        ordering = ['position', 'name']
        verbose_name = 'product type'
        verbose_name_plural = 'product types'

    def __str__(self):
        return self.name


class ProductType(models.Model):
    """A **product code** (that's what the screens call it): e.g. FBB009 — Flat Bright
    Bar in EN-8D — defines the product, its grade, and which steps apply.

    A product code depends on two things: its product type (`category`: Round
    Bright Bar, Key Steel, ...) and grade. Those two identify exactly one code — a
    round and a hexagonal bar in the same grade are different codes. **Size is not
    part of it:** width and thickness vary per order, so they live on the query,
    quote line and order, not here.
    """
    category    = models.ForeignKey(
        ProductCategory, on_delete=models.PROTECT, null=True, related_name='product_codes',
        verbose_name="Product Type",
    )
    item_code   = models.CharField(max_length=100, verbose_name="Item Code")
    grade       = GradeField(max_length=20, blank=True, verbose_name="Grade")
    description = models.TextField(blank=True)

    class Meta:
        constraints = [
            # type + grade identify one code ...
            models.UniqueConstraint(fields=['category', 'grade'], name='unique_code_per_type_grade'),
            # ... and a database treats NULLs as distinct, so codes that don't have a type yet
            # (older rows) still can't repeat a grade.
            models.UniqueConstraint(
                fields=['grade'], condition=models.Q(category__isnull=True),
                name='unique_untyped_code_per_grade',
            ),
        ]
        verbose_name = 'product code'
        verbose_name_plural = 'product codes'

    def __str__(self):
        return self.item_code


class AllowedCoilSpec(models.Model):
    """Coil grades/sizes the admin approves for a given product type."""
    product_type = models.ForeignKey(ProductType, on_delete=models.CASCADE, related_name='allowed_specs')
    grade = GradeField(max_length=10, blank=True, verbose_name="Grade")
    size  = models.DecimalField(max_digits=10, decimal_places=3, null=True, blank=True, verbose_name="Size (mm)")
    raw_material_ratio = models.DecimalField(
        max_digits=6, decimal_places=3, default=Decimal('1.000'),
        verbose_name="Raw material ratio",
    )
    notes = models.CharField(max_length=100, blank=True)
    # Which ordered sizes this spec is for. Blank on both = any size (the original behaviour); set one or both
    # to make it apply only to orders of exactly that width / thickness.
    order_width = models.DecimalField(max_digits=10, decimal_places=3, null=True, blank=True, verbose_name="For ordered width (mm)")
    order_thickness = models.DecimalField(max_digits=10, decimal_places=3, null=True, blank=True, verbose_name="For ordered thickness (mm)")

    def is_generic(self):
        return self.order_width is None and self.order_thickness is None

    def applies_to(self, order):
        """A size-specific spec applies to an order of exactly that width/thickness (a blank side matches any)."""
        if self.order_width is not None and self.order_width != order.width:
            return False
        if self.order_thickness is not None and self.order_thickness != order.thickness:
            return False
        return True

    def __str__(self):
        parts = []
        if self.grade:
            parts.append(self.grade)
        if self.size:
            parts.append(f"{self.size} mm")
        text = f"{self.product_type.item_code} — {' / '.join(parts) or 'Any'}"
        if not self.is_generic():
            sizes = ' x '.join(format(v.normalize(), 'f') for v in (self.order_width, self.order_thickness) if v is not None)
            text += f" (for {sizes} mm orders)"
        return text


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
            for spec in self.order.applicable_specs():
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


# Shape of a GSTIN: state code, PAN, entity number, 'Z', checksum character.
# Format only — the checksum would need the GST portal to verify.
GSTIN_PATTERN = r'\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]'


class Query(models.Model):
    """An inbound sales inquiry, logged before any quote/order exists —
    phone calls, referrals, IndiaMART messages, WhatsApp, etc. Staff decide
    which ones to pursue by sending a quote; the quote form the customer
    eventually fills is pre-populated with whatever was already captured
    here. `company_name` is often blank at creation time — a manually
    logged query starts as just a phone number, and a WhatsApp-sourced one
    (see `materials/views/whatsapp.py::whatsapp_webhook`) only has a name if Meta's
    payload included one — it gets filled in later (via the admin) once
    staff actually know who they're talking to."""
    # The four a person can pick when logging a query by hand (MANUAL_SOURCES),
    # then two that only exist for other reasons: 'whatsapp' is what the bot
    # records for a cold inbound message, and 'call' is what older entries
    # were logged as — both stay valid so existing rows still display.
    SOURCE_CHOICES = [
        ('indiamart', 'IndiaMART'),
        ('google', 'Google'),
        ('referral', 'Referral'),
        ('other', 'Other'),
        ('whatsapp', 'WhatsApp'),
        ('call', 'Phone Call'),
    ]
    MANUAL_SOURCES = ('indiamart', 'google', 'referral', 'other')
    STATUS_CHOICES = [
        ('new', 'New'),
        ('quote_sent', 'Quote Sent'),
        ('converted', 'Converted'),
        ('not_interested', 'Not Interested'),
    ]
    DELIVERY_FORM_CHOICES = [('Coil', 'Coil'), ('Bar', 'Bar')]
    source        = models.CharField(max_length=20, choices=SOURCE_CHOICES)
    # Optional follow-ups to `source`: who referred them (source='referral'),
    # or where else they came from (source='other').
    referrer_name  = models.CharField(max_length=100, blank=True)
    referrer_phone = models.CharField(max_length=20, blank=True)
    source_detail  = models.CharField(max_length=200, blank=True)
    company_name  = models.CharField(max_length=100, blank=True)
    contact_phone = models.CharField(max_length=20, blank=True)
    contact_email = models.EmailField(blank=True)
    # What the customer wants to buy lives in the query's items (QueryItem below): one row per product.
    # Collected by the WhatsApp intake bot after email. One WhatsApp message
    # maps to one field: GST number + address arrive together and are split
    # by _parse_whatsapp_gst_details; the other combined questions are saved
    # as typed. The drawing/sample (an image/PDF the customer sends is saved
    # in `drawing`; drawing_notes holds either their reply if they didn't
    # attach one — typically "no" — or a short description) doubles via
    # drawing_notes as the "this question was actually asked and answered"
    # marker for _next_expected_query_field, since a FileField alone can't
    # distinguish "not asked yet" from "asked, no drawing".
    drawing       = models.FileField(upload_to='query_drawings/%Y/%m/', blank=True, null=True)
    drawing_notes = models.CharField(max_length=255, blank=True)
    # A reply of "no" is a real answer and is kept: a non-blank value is what
    # marks each question answered. gst_number is the one validated field: a
    # well-formed 15-character GSTIN is compulsory for every company, so there
    # is no "NA" (blank only means it hasn't been collected yet).
    gst_number             = models.CharField(
        max_length=15, blank=True,
        validators=[RegexValidator(rf'^{GSTIN_PATTERN}$', 'Enter a valid 15-character GST number.')],
    )
    gst_address            = models.TextField(blank=True)
    product_description    = models.TextField(blank=True)
    technical_requirements = models.TextField(blank=True)  # particular make, mechanical properties, process
    end_use                = models.TextField(blank=True)
    notes         = models.TextField(blank=True)
    status        = models.CharField(max_length=20, choices=STATUS_CHOICES, default='new', db_index=True)
    # Set only once a quote is actually sent — before that, a query is just
    # free-standing prospect info, not yet a Customer record (mirrors how
    # Customer itself is created on-demand in quick_send_quote).
    customer      = models.ForeignKey(Customer, on_delete=models.SET_NULL, null=True, blank=True, related_name='queries')
    created_at    = models.DateTimeField(auto_now_add=True)
    # WhatsApp bot bookkeeping (see views/whatsapp.py). `last_inbound_at` is when the customer's
    # newest processed message was *sent* (WhatsApp lets the bot send free-form text only for 24 hours
    # after it); `last_asked_field` is the question the bot sent last and nobody has answered yet, so a
    # replayed or duplicate message can't make it ask the same thing twice; `needs_review` flags a
    # conversation staff should look at (a reply arrived out of order, or the 24-hour window closed).
    last_inbound_at  = models.DateTimeField(null=True, blank=True, editable=False)
    last_asked_field = models.CharField(max_length=30, blank=True, editable=False)
    needs_review     = models.BooleanField(default=False)
    review_note      = models.CharField(max_length=255, blank=True)
    # The end-of-conversation review: once every question is answered the bot sends a summary and asks
    # the customer to confirm or change something (stage 'summary'); a change is held in `pending_value`
    # (and `pending_drawing` for a file) until they confirm it. '' = still collecting answers (or an old
    # query that finished before the review existed), 'done' = they confirmed.
    bot_stage        = models.CharField(max_length=20, blank=True, editable=False)
    edit_field       = models.CharField(max_length=30, blank=True, editable=False)
    pending_value    = models.JSONField(default=dict, blank=True, editable=False)
    pending_drawing  = models.FileField(upload_to='query_drawings/pending/%Y/%m/', blank=True, null=True, editable=False)
    intake_confirmed_at = models.DateTimeField(null=True, blank=True, editable=False)

    # (field, label) for the free-text answers above, in the order the bot asks
    # them — the one list the dashboard, the edit form and the bot all read.
    INTAKE_TEXT_FIELDS = [
        ('gst_number', 'GST number'),
        ('gst_address', 'GST address'),
        ('technical_requirements', 'Make / properties / process'),
        ('end_use', 'End use'),
    ]

    class Meta:
        ordering = ['-created_at']

    @classmethod
    def manual_source_choices(cls):
        return [choice for choice in cls.SOURCE_CHOICES if choice[0] in cls.MANUAL_SOURCES]

    # ── Quote tracking. Sending a quote again for the same query means the quote
    # was updated; every send is its own immutable Quotation, and these read
    # them back oldest-first (honouring the dashboard's prefetch) as revisions.
    def sent_quotations(self):
        return sorted((q for q in self.quotations.all() if q.status == 'sent'), key=lambda q: q.quotation_no)

    def latest_sent_quotation(self):
        sent = self.sent_quotations()
        return sent[-1] if sent else None

    def quote_revision(self):
        """0 for the original quote, then 1, 2, ... for each time it was updated and re-sent."""
        return max(len(self.sent_quotations()) - 1, 0)

    def quotation_history(self):
        """[{'quotation', 'revision'}], newest first: drafts (revision None), then every sent
        quote with its revision number (0 = the original)."""
        drafts = [{'quotation': q, 'revision': None} for q in self.quotations.all() if q.status == 'draft']
        sent = [{'quotation': q, 'revision': i} for i, q in enumerate(self.sent_quotations())]
        return drafts + list(reversed(sent))

    GST_FIELDS = ('gst_number', 'gst_address')

    def order_numbers(self):
        """["ORD-0003", ...] for the orders placed from this query's quote, oldest first. Numbers are
        read live (they close up when an earlier order is deleted), so this is always current."""
        return [f"ORD-{order.order_no:04d}" for order in sorted(self.orders.all(), key=lambda o: o.pk) if order.order_no]

    MAX_ITEMS = 5   # the bot offers 1 to 5 products; staff can add more by hand

    def item_list(self):
        """The products the customer asked about, in order (honouring a prefetch)."""
        return sorted(self.items.all(), key=lambda item: (item.position, item.pk))

    def gst_rows(self):
        """[(label, value)] for the GST answers, blank ones included (the detail page shows a dash)."""
        return [(label, getattr(self, f)) for f, label in self.INTAKE_TEXT_FIELDS if f in self.GST_FIELDS]

    def requirement_rows(self):
        """[(label, value)] for the product-related answers (everything except GST), blank ones included."""
        return [(label, getattr(self, f)) for f, label in self.INTAKE_TEXT_FIELDS if f not in self.GST_FIELDS]

    def __str__(self):
        label = self.company_name or self.contact_phone or f"Query #{self.pk}"
        return f"{label} — {self.get_source_display()}"


class QueryItem(models.Model):
    """One product a customer asked about in a query. A query holds one or more (up to 5 through the
    WhatsApp bot): the bot asks how many products first, then each question once with a comma-separated
    answer, one value per product, and fills the matching column of every item. The quote form opens
    with one line per item."""
    query            = models.ForeignKey(Query, on_delete=models.CASCADE, related_name='items')
    position         = models.PositiveSmallIntegerField(default=1)
    # The product type (family) — chosen from the bot's list, or set by staff.
    product_category = models.ForeignKey(ProductCategory, on_delete=models.SET_NULL, null=True, blank=True, verbose_name="Product Type")
    product_type     = models.ForeignKey(ProductType, on_delete=models.SET_NULL, null=True, blank=True)   # the product code
    grade            = GradeField(max_length=100, blank=True)
    width            = models.DecimalField(max_digits=10, decimal_places=3, null=True, blank=True, verbose_name="Width (mm)")
    thickness        = models.DecimalField(max_digits=10, decimal_places=3, null=True, blank=True, verbose_name="Thickness (mm)")
    quantity         = models.DecimalField(max_digits=10, decimal_places=3, null=True, blank=True, verbose_name="Quantity (kg)")
    delivery_form    = models.CharField(max_length=10, blank=True, choices=Query.DELIVERY_FORM_CHOICES)

    class Meta:
        ordering = ['position', 'pk']

    def __str__(self):
        return f"Product {self.position} of query {self.query_id}"

    def dimensions_text(self, separator=' x '):
        """"50 x 6.5 mm" — width and thickness without trailing zeros, '' if neither is known."""
        parts = [format(value.normalize(), 'f') for value in (self.width, self.thickness) if value is not None]
        return f"{separator.join(parts)} mm" if parts else ''

    def matching_product_code(self):
        """The catalogue product code for this item's product type + grade, or None
        (size plays no part in a code)."""
        if not (self.product_category_id and self.grade):
            return None
        from .product_codes import find_product_code
        return find_product_code(self.product_category, self.grade)

    def effective_product_code(self):
        """The code linked to the item, else the one its type + grade point to."""
        return self.product_type or self.matching_product_code()

    def summary_text(self):
        """"Flat Bright Bar · EN8D · 50 x 6.5 mm · 500 kg · Coil" — whatever is known about it."""
        quantity = f"{format(self.quantity.normalize(), 'f')} kg" if self.quantity is not None else ''
        parts = [self.product_category.name if self.product_category_id else '', self.grade, self.dimensions_text(),
                 quantity, self.delivery_form]
        return ' · '.join(part for part in parts if part)


class WhatsAppMessage(models.Model):
    """The id of every WhatsApp message the webhook has handled (Meta's `wamid`), so a message
    Meta delivers twice — it retries any delivery that wasn't acknowledged, and replays a backlog
    when the app comes back after being offline — is only ever processed once."""
    message_id  = models.CharField(max_length=128, unique=True)
    received_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.message_id


def _indian_number_to_words(n):
    """Converts a non-negative integer to words using the Indian numbering
    system (lakh/crore, not the Western thousand/million grouping) — e.g.
    197650 -> "One Lakh Ninety Seven Thousand Six Hundred Fifty", matching
    how the client's existing (non-app) quotations render amounts."""
    ones = ['', 'One', 'Two', 'Three', 'Four', 'Five', 'Six', 'Seven', 'Eight', 'Nine',
            'Ten', 'Eleven', 'Twelve', 'Thirteen', 'Fourteen', 'Fifteen', 'Sixteen',
            'Seventeen', 'Eighteen', 'Nineteen']
    tens = ['', '', 'Twenty', 'Thirty', 'Forty', 'Fifty', 'Sixty', 'Seventy', 'Eighty', 'Ninety']

    def two_digits(num):
        if num < 20:
            return ones[num]
        return (tens[num // 10] + (f" {ones[num % 10]}" if num % 10 else '')).strip()

    def three_digits(num):
        if num >= 100:
            return f"{ones[num // 100]} Hundred" + (f" {two_digits(num % 100)}" if num % 100 else '')
        return two_digits(num)

    if n == 0:
        return 'Zero'

    crore, n = divmod(n, 10_000_000)
    lakh, n = divmod(n, 100_000)
    thousand, n = divmod(n, 1000)
    hundred = n

    parts = []
    if crore:
        parts.append(f"{three_digits(crore)} Crore")
    if lakh:
        parts.append(f"{two_digits(lakh)} Lakh")
    if thousand:
        parts.append(f"{two_digits(thousand)} Thousand")
    if hundred:
        parts.append(three_digits(hundred))
    return ' '.join(parts)


class Quotation(models.Model):
    """A record of an official quotation actually sent to a customer —
    created every time Send Quote fires, from quotation_form (materials/views/
    quotations.py). Immutable once **sent** — correcting anything means sending
    a new quotation, not editing history, the same way Order itself is
    never silently rewritten. A quotation in 'draft' status is the one
    exception: an explicitly incomplete, still-being-edited quote that
    hasn't gone out yet, freely editable and discardable via quotation_form
    (edit mode) / quotation_discard until it's actually sent.

    quotation_no is only assigned when status flips to 'sent' (see save())
    — a draft that's abandoned and deleted must never have consumed a
    number, since QUO-#### is a permanent record of what was actually
    quoted, the same "never renumbered, never reused" guarantee order_no
    deliberately does NOT have (see Order.order_no's own docs).

    Mirrors the client's real, existing (previously non-app) quotation
    format: header fields (ref/rev numbers, sales person, subject), one or
    more QuotationLineItems, freight/P&F, and a same-state-driven GST split
    (CGST+SGST if the customer is in the same state as us, else IGST — see
    same_state_as_us and gst_total())."""
    STATUS_CHOICES = [
        ('draft', 'Draft'),
        ('sent',  'Sent'),
    ]

    customer         = models.ForeignKey(Customer, on_delete=models.CASCADE, related_name='quotations')
    source_query     = models.ForeignKey(Query, on_delete=models.SET_NULL, null=True, blank=True, related_name='quotations')
    status           = models.CharField(max_length=10, choices=STATUS_CHOICES, default='draft')
    quotation_no     = models.PositiveIntegerField(unique=True, editable=False, null=True)
    created_at       = models.DateTimeField(auto_now_add=True)
    updated_at       = models.DateTimeField(auto_now=True)
    # When it was actually sent (draft -> sent), set once and never changed —
    # created_at is when the draft was first started, updated_at moves on any save.
    sent_at          = models.DateTimeField(null=True, blank=True, editable=False)

    # Header fields — all optional, matching the reference template.
    ref_no           = models.CharField(max_length=50, blank=True)
    rev_no           = models.PositiveIntegerField(default=0)
    rev_date         = models.DateField(null=True, blank=True)
    sales_person     = models.CharField(max_length=100, blank=True)
    kind_attn        = models.CharField(max_length=100, blank=True)
    subject          = models.CharField(max_length=200, blank=True)
    # Captured at quote time rather than looked up live from Customer (which
    # has no address field of its own) — same reasoning as grade/size used
    # to be copied onto the old single-rate Quotation: a quote is a point-
    # in-time snapshot, not a live view of mutable customer data.
    customer_address = models.TextField(blank=True)
    customer_gstin   = models.CharField(
        max_length=15, blank=True,
        validators=[RegexValidator(rf'^{GSTIN_PATTERN}$', 'Enter a valid 15-character GST number.')],
    )

    same_state_as_us = models.BooleanField(
        default=True,
        help_text="Drives the GST split: CGST+SGST (half each) if checked, IGST if not.",
    )
    freight_amount   = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    pf_amount        = models.DecimalField(max_digits=10, decimal_places=2, default=0, verbose_name="P&F amount")

    # Terms & Conditions — editable per quote, defaulting to the client's
    # standard wording so staff don't have to retype them every time.
    price_basis      = models.CharField(max_length=200, blank=True, default='Ex-Works')
    gst_terms        = models.CharField(max_length=200, blank=True, default='Extra As Applicable')
    insurance_terms  = models.CharField(max_length=200, blank=True, default='Extra At actual to be borne by Customer')
    freight_terms    = models.CharField(max_length=200, blank=True, default='The Same Shall be in your scope')
    payment_terms    = models.CharField(max_length=200, blank=True, default='100% Advance')
    delivery_terms   = models.CharField(max_length=200, blank=True, default='')
    validity_terms   = models.CharField(max_length=200, blank=True, default='7 Days from date of offer')

    class Meta:
        ordering = ['-created_at']

    def save(self, *args, **kwargs):
        # Only a *sent* quotation ever gets a permanent number — a draft
        # that's edited/saved repeatedly, or abandoned and discarded, must
        # never consume one.
        if self.status == 'sent' and self.quotation_no is None:
            max_no = Quotation.objects.aggregate(models.Max('quotation_no'))['quotation_no__max'] or 0
            self.quotation_no = max_no + 1
        if self.status == 'sent' and self.sent_at is None:
            self.sent_at = timezone.now()
        super().save(*args, **kwargs)

    def formatted_no(self):
        if self.quotation_no is None:
            return 'DRAFT'
        return f"QUO-{self.quotation_no:04d}"

    def is_draft(self):
        return self.status == 'draft'

    def subtotal(self):
        return sum((item.amount() for item in self.line_items.all()), Decimal('0'))

    def tool_cost_total(self):
        return sum((item.tool_cost for item in self.line_items.all()), Decimal('0'))

    def gst_total(self):
        return sum((item.gst_amount() for item in self.line_items.all()), Decimal('0'))

    def cgst(self):
        return (self.gst_total() / 2) if self.same_state_as_us else Decimal('0')

    def sgst(self):
        return (self.gst_total() / 2) if self.same_state_as_us else Decimal('0')

    def igst(self):
        return Decimal('0') if self.same_state_as_us else self.gst_total()

    def total_amount(self):
        return (
            self.subtotal() + self.tool_cost_total()
            + self.freight_amount + self.pf_amount + self.gst_total()
        )

    def amount_in_words(self):
        """Rounds to the nearest rupee — the reference format has no paise
        in its words line ("... Six Hundred Fifty Only")."""
        rupees = int(self.total_amount().to_integral_value(rounding=ROUND_HALF_UP))
        return f"Rs. {_indian_number_to_words(rupees)} Only"

    def __str__(self):
        return f"{self.formatted_no()} — {self.customer.name}"


class QuotationLineItem(models.Model):
    """One priced item within a Quotation — a quote can cover several
    grade/size combinations at once (e.g. two different chamfer sizes),
    each with its own quantity, rate, HSN/SAC and GST%, matching the
    client's real quotation format.

    quantity/rate_per_kg are nullable specifically so a **draft**
    quotation can hold a row that's still being figured out (a product
    picked, rate not agreed yet) — quotation_form's full-validation path
    (actually sending) still requires both via the form layer; this is a
    DB-level relaxation for drafts only, not a sign either is optional
    once a quotation is actually sent."""
    quotation    = models.ForeignKey(Quotation, on_delete=models.CASCADE, related_name='line_items')
    order        = models.PositiveIntegerField(default=1, help_text="Display order (Sr. No.) within the quotation.")
    description  = models.CharField(max_length=255)
    category     = models.ForeignKey(ProductCategory, on_delete=models.SET_NULL, null=True, blank=True, verbose_name="Product Type")
    product_type = models.ForeignKey(ProductType, on_delete=models.SET_NULL, null=True, blank=True)
    grade        = GradeField(max_length=100, blank=True)
    width        = models.DecimalField(max_digits=10, decimal_places=3, null=True, blank=True, verbose_name="Width (mm)")
    thickness    = models.DecimalField(max_digits=10, decimal_places=3, null=True, blank=True, verbose_name="Thickness (mm)")
    quantity     = models.DecimalField(max_digits=10, decimal_places=3, null=True, blank=True)
    unit         = models.CharField(max_length=20, default='KGS')
    rate_per_kg  = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    discount_pct = models.DecimalField(max_digits=5, decimal_places=2, default=0, verbose_name="Discount %")
    hsn_sac      = models.CharField(max_length=20, blank=True, verbose_name="HSN/SAC")
    gst_pct      = models.DecimalField(max_digits=5, decimal_places=2, default=18, verbose_name="GST %")
    tool_cost    = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    moq          = models.DecimalField(max_digits=10, decimal_places=3, default=0, verbose_name="MOQ")

    class Meta:
        ordering = ['order']

    def width_text(self):
        return format(self.width.normalize(), 'f') if self.width is not None else ''

    def thickness_text(self):
        return format(self.thickness.normalize(), 'f') if self.thickness is not None else ''

    def product_type_name(self):
        """The product type (e.g. Flat Bright Bar): the line's own, else its product code's."""
        category = self.category or (self.product_type.category if self.product_type else None)
        return category.name if category else ''

    def spec_text(self):
        """"Flat Bright Bar / EN8D / 50 x 6.5 mm" — product type, grade and width x thickness,
        whichever are known. Shown on the PDF under the description and in the quote email."""
        dims = ' x '.join(format(value.normalize(), 'f') for value in (self.width, self.thickness) if value is not None)
        return ' / '.join(part for part in (self.product_type_name(), self.grade, f"{dims} mm" if dims else '') if part)

    def gross_amount(self):
        if self.quantity is None or self.rate_per_kg is None:
            return Decimal('0')
        return self.quantity * self.rate_per_kg

    def discount_amount(self):
        return self.gross_amount() * (self.discount_pct / Decimal('100'))

    def amount(self):
        """Net of the line's own discount — before GST, freight, P&F, or
        tool cost, which are all applied at the quotation level."""
        return self.gross_amount() - self.discount_amount()

    def gst_amount(self):
        return self.amount() * (self.gst_pct / Decimal('100'))

    def __str__(self):
        return f"{self.description} — {self.quantity} {self.unit}"


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
    product_type          = models.ForeignKey(ProductType, on_delete=models.SET_NULL, null=True, blank=True, related_name='orders', verbose_name="Product Code")
    # 1. Drawing / dimensions
    drawing_dimensions    = models.TextField(blank=True, verbose_name="Drawing / Dimensions")   # typed text: staff entry and older orders
    # What the customer attaches on the order form (instead of typing dimensions), and their tolerances:
    # a From and a To for the width and for the thickness (as the customer wrote them — either limits such
    # as 49.95 / 50.05 or offsets such as -0.05 / +0.05), plus free text for any other tolerance.
    drawing_file          = models.FileField(
        upload_to='order_drawings/%Y/%m/', blank=True, null=True, verbose_name="Drawing",
        validators=[FileExtensionValidator(DRAWING_EXTENSIONS), validate_drawing_size],
    )
    width_tol_from        = models.DecimalField(max_digits=8, decimal_places=3, null=True, blank=True, verbose_name="Width tolerance from")
    width_tol_to          = models.DecimalField(max_digits=8, decimal_places=3, null=True, blank=True, verbose_name="Width tolerance to")
    thickness_tol_from    = models.DecimalField(max_digits=8, decimal_places=3, null=True, blank=True, verbose_name="Thickness tolerance from")
    thickness_tol_to      = models.DecimalField(max_digits=8, decimal_places=3, null=True, blank=True, verbose_name="Thickness tolerance to")
    other_tolerances      = models.TextField(blank=True, verbose_name="Other tolerances")
    # Asked once the customer picks a delivery form: a bar needs its length (mm) with a tolerance From / To,
    # a coil needs an approximate weight (kg). Only the one for the chosen form is kept.
    bar_length            = models.DecimalField(max_digits=10, decimal_places=3, null=True, blank=True, verbose_name="Bar length (mm)")
    length_tol_from       = models.DecimalField(max_digits=8, decimal_places=3, null=True, blank=True, verbose_name="Length tolerance from")
    length_tol_to         = models.DecimalField(max_digits=8, decimal_places=3, null=True, blank=True, verbose_name="Length tolerance to")
    coil_weight           = models.DecimalField(max_digits=10, decimal_places=3, null=True, blank=True, verbose_name="Approx. coil weight (kg)")
    # 2. Grade, width & thickness (grade autofilled from the product code, editable)
    grade                 = GradeField(max_length=100, blank=True, verbose_name="Grade of Material")
    width                 = models.DecimalField(max_digits=10, decimal_places=3, null=True, blank=True, verbose_name="Width (mm)")
    thickness             = models.DecimalField(max_digits=10, decimal_places=3, null=True, blank=True, verbose_name="Thickness (mm)")
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

    def applicable_specs(self):
        """The allowed coil specs that govern this order: those made for its exact size (width/thickness) if any
        apply, otherwise the size-less ones. Empty when none is configured, or when specs exist only for other
        sizes (check `product_type.allowed_specs.exists()` to tell the two apart)."""
        if not self.product_type_id:
            return []
        specs = list(self.product_type.allowed_specs.all())
        specific = [spec for spec in specs if not spec.is_generic() and spec.applies_to(self)]
        return specific or [spec for spec in specs if spec.is_generic()]

    def available_raw_material_output(self):
        """Total finished-product output that could be made right now from
        in-stock raw material matching this order's product type — summed
        across every AllowedCoilSpec (grade/size + ratio), or any non-
        archived coil with remaining weight if none are configured (same
        wildcard fallback _coil_matches_order_specs uses in views/picking.py).
        None if there's no product type yet to check against. Used to warn
        an admin confirming an order that raw material may need reordering
        before production can actually happen."""
        if not self.product_type:
            return None
        specs = self.applicable_specs()
        if not specs and self.product_type.allowed_specs.exists():
            return Decimal('0')   # specs are configured but none is for this order's size: nothing qualifies
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

    def tolerance_lines(self):
        """["Width 49.95 to 50.05 mm", "Thickness ...", "Other: ..."] — only what was given."""
        def span(label, low, high):
            if low is None and high is None:
                return ''
            return f"{label} {format(low.normalize(), 'f') if low is not None else '…'} to {format(high.normalize(), 'f') if high is not None else '…'} mm"
        lines = [span('Width', self.width_tol_from, self.width_tol_to), span('Thickness', self.thickness_tol_from, self.thickness_tol_to),
                 span('Length', self.length_tol_from, self.length_tol_to)]
        if self.other_tolerances.strip():
            lines.append(f"Other: {self.other_tolerances.strip()}")
        return [line for line in lines if line]

    def width_text(self):
        return format(self.width.normalize(), 'f') if self.width is not None else ''

    def size_text(self):
        """"16 x 8": width x thickness in mm, whichever are known (the short form the picking pages show)."""
        return ' x '.join(part for part in (self.width_text(), self.thickness_text()) if part)

    def thickness_text(self):
        return format(self.thickness.normalize(), 'f') if self.thickness is not None else ''

    def quantity_text(self):
        return format(self.quantity.normalize(), 'f') if self.quantity is not None else ''

    def delivery_detail_text(self):
        """"Bar, 3000 mm long" / "Coil, approx. 2000 kg" — the delivery form with what was asked for it."""
        def number(value):
            return format(value.normalize(), 'f')
        if self.delivery_form == 'bar':
            return f"Bar, {number(self.bar_length)} mm long" if self.bar_length is not None else "Bar"
        if self.delivery_form == 'coil':
            return f"Coil, approx. {number(self.coil_weight)} kg" if self.coil_weight is not None else "Coil"
        return ''

    def product_type_name(self):
        """The product type (e.g. Flat Bright Bar), taken from the order's product code."""
        category = self.product_type.category if self.product_type_id else None
        return category.name if category else ''

    def spec_text(self):
        """"Flat Bright Bar / EN8D / 50 x 6.5 mm": product type, grade and width x thickness,
        whichever are known (the same line the quote PDF prints for the item)."""
        dims = ' x '.join(format(value.normalize(), 'f') for value in (self.width, self.thickness) if value is not None)
        return ' / '.join(part for part in (self.product_type_name(), self.grade, f"{dims} mm" if dims else '') if part)

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