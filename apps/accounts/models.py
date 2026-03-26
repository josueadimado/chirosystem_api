from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    class Roles(models.TextChoices):
        OWNER_ADMIN = "owner_admin", "Owner/Admin"
        DOCTOR = "doctor", "Doctor"
        STAFF = "staff", "Staff"

    full_name = models.CharField(max_length=200, blank=True)
    phone = models.CharField(max_length=20, blank=True, default="")
    role = models.CharField(max_length=20, choices=Roles.choices, default=Roles.STAFF)

    def __str__(self) -> str:
        return self.full_name or self.username
