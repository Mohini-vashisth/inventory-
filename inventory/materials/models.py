from django.db import models


class Material(models.Model):
    coil_no=models.AutoField(primary_key=True)
    date = models.DateField("reciept date ",null=True, blank=True)
    grade = models.CharField(max_length=10 ,null=True, blank=True)
    size = models.DecimalField(max_digits=10, decimal_places=3 ,null=True, blank=True)
    company = models.CharField(max_length=10 ,null=True, blank=True)
    vendor = models.CharField(max_length=50, null=True, blank=True)
    quantity= models.IntegerField(null=True, blank=True)
    heat_no = models.CharField(max_length=8,null=True, blank=True)

    def formatted_coil(self):
        return f"COIL{self.coil_no:04d}"

