from django.db import models
from django.contrib.auth.models import User


class Material(models.Model):
    coil_no = models.AutoField(primary_key=True)
    date = models.DateField("receipt date", null=True, blank=True)
    grade = models.CharField(max_length=10, null=True, blank=True)
    size = models.DecimalField(max_digits=10, decimal_places=3, null=True, blank=True)
    company = models.CharField(max_length=10, null=True, blank=True)
    vendor = models.CharField(max_length=50, null=True, blank=True)
    quantity = models.IntegerField(null=True, blank=True)
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
    """e.g. 'Bracket A', 'Shaft B' — defines which steps apply."""
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True)

    def __str__(self):
        return self.name


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

    part = models.ForeignKey(CoilPart, on_delete=models.CASCADE, related_name='jobs')
    product_type = models.ForeignKey(ProductType, on_delete=models.PROTECT)
    job_no = models.CharField(max_length=30, unique=True)   # e.g. JOB-0001
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