from django.db import models


class Material(models.Model):
    coil_no=models.AutoField(primary_key=True)
    date = models.DateField("reciept date ")
    grade = models.CharField(max_length=10)
    size = models.DecimalField(max_digits=10, decimal_places=3)
    company = models.CharField(max_length=10)
    vendor = models.CharField(max_length=50)
    quantity= models.IntegerField()
    heat_no = models.CharField(max_length=8)

