import uuid
from django.db import models
from django.contrib.auth.models import User


class Material(models.Model):
    coil_no = models.AutoField(primary_key=True)
    date = models.DateField("receipt date", null=True, blank=True)
    grade = models.CharField(max_length=10, null=True, blank=True)
    size = models.DecimalField(max_digits=10, decimal_places=3, null=True, blank=True)
    company = models.CharField(max_length=100, null=True, blank=True)
    vendor = models.CharField(max_length=50, null=True, blank=True)
    quantity = models.DecimalField(max_digits=10, decimal_places=3, null=True, blank=True)
    heat_no = models.CharField(max_length=8, null=True, blank=True)

    def formatted_coil(self):
        return f"COIL{self.coil_no:04d}"

    def __str__(self):
        return self.formatted_coil()


class CoilPart(models.Model):
    """One physical piece cut from a coil."""
    coil = models.ForeignKey(Material, on_delete=models.CASCADE, related_name='parts')
    part_no = models.CharField(max_length=20, unique=True)  # e.g. COIL0001-A
    weight = models.DecimalField(max_digits=10, decimal_places=3, null=True, blank=True)
    length = models.DecimalField(max_digits=10, decimal_places=3, null=True, blank=True)
    cut_date = models.DateField(null=True, blank=True)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.part_no


class ProductType(models.Model):
    """e.g. 'EN8D Bar 2.5mm' — defines the final product, its preset grade/size, and which steps apply."""
    name        = models.CharField(max_length=100)
    grade       = models.CharField(max_length=20, blank=True, verbose_name="Grade")
    size        = models.DecimalField(max_digits=10, decimal_places=3, null=True, blank=True, verbose_name="Size (mm)")
    description = models.TextField(blank=True)

    def __str__(self):
        return self.name


class AllowedCoilSpec(models.Model):
    """Coil grades/sizes the admin approves for a given product type."""
    product_type = models.ForeignKey(ProductType, on_delete=models.CASCADE, related_name='allowed_specs')
    grade = models.CharField(max_length=10, blank=True, verbose_name="Grade")
    size  = models.DecimalField(max_digits=10, decimal_places=3, null=True, blank=True, verbose_name="Size (mm)")
    notes = models.CharField(max_length=100, blank=True)

    def __str__(self):
        parts = []
        if self.grade: parts.append(self.grade)
        if self.size:  parts.append(f"{self.size} mm")
        return f"{self.product_type.name} — {' / '.join(parts) or 'Any'}"


class ProcessStep(models.Model):
    """A named step belonging to a product type, with a defined order."""
    product_type = models.ForeignKey(ProductType, on_delete=models.CASCADE, related_name='steps')
    name = models.CharField(max_length=100)   # e.g. "Blanking", "Forming", "Heat treat"
    order = models.PositiveIntegerField()      # 1, 2, 3 ...

    class Meta:
        ordering = ['order']
        unique_together = ['product_type', 'order']

    def __str__(self):
        return f"{self.product_type.name} — Step {self.order}: {self.name}"


class ProductionJob(models.Model):
    """Links a coil part to a product type and tracks overall status."""
    STATUS_CHOICES = [
        ('pending',     'Pending'),
        ('in_progress', 'In Progress'),
        ('on_hold',     'On Hold'),
        ('completed',   'Completed'),
    ]

    part         = models.ForeignKey(CoilPart, on_delete=models.CASCADE, related_name='jobs')
    product_type = models.ForeignKey(ProductType, on_delete=models.PROTECT)
    order        = models.ForeignKey('Order', on_delete=models.SET_NULL, null=True, blank=True, related_name='jobs')
    job_no       = models.CharField(max_length=30, unique=True)   # e.g. JOB-0001
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    notes = models.TextField(blank=True)

    def __str__(self):
        return self.job_no

    def current_step(self):
        """Returns the latest StepLog entry for this job."""
        return self.step_logs.order_by('-timestamp').first()


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

    customer              = models.ForeignKey(Customer, on_delete=models.PROTECT, related_name='orders')
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
    status        = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    created_at    = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"ORD-{self.pk:04d} | {self.customer.name}"