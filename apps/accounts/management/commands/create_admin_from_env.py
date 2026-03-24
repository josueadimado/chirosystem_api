"""
Create or update a Django superuser using environment variables.

Required:
  DJANGO_ADMIN_USERNAME
  DJANGO_ADMIN_PASSWORD

Optional:
  DJANGO_ADMIN_EMAIL   (default: <username>@localhost)
  DJANGO_ADMIN_FULL_NAME

Example (.env on the server, never commit real secrets):
  DJANGO_ADMIN_USERNAME=admin
  DJANGO_ADMIN_PASSWORD=your-secure-password

Run:
  python manage.py create_admin_from_env
"""

import os

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Create or update a superuser from DJANGO_ADMIN_USERNAME / DJANGO_ADMIN_PASSWORD."

    def handle(self, *args, **options):
        username = os.environ.get("DJANGO_ADMIN_USERNAME", "").strip()
        password = os.environ.get("DJANGO_ADMIN_PASSWORD", "")
        email = (os.environ.get("DJANGO_ADMIN_EMAIL") or "").strip()
        full_name = (os.environ.get("DJANGO_ADMIN_FULL_NAME") or "").strip()

        if not username:
            raise CommandError(
                "Set DJANGO_ADMIN_USERNAME (and DJANGO_ADMIN_PASSWORD) in the environment."
            )
        if not password:
            raise CommandError(
                "Set DJANGO_ADMIN_PASSWORD in the environment."
            )

        if not email:
            email = f"{username}@localhost"

        User = get_user_model()
        defaults = {
            "email": email,
            "is_staff": True,
            "is_superuser": True,
            "role": User.Roles.OWNER_ADMIN,
        }
        if full_name:
            defaults["full_name"] = full_name

        user, created = User.objects.update_or_create(username=username, defaults=defaults)
        user.set_password(password)
        user.save(update_fields=["password"])

        verb = "Created" if created else "Updated password for"
        self.stdout.write(self.style.SUCCESS(f"{verb} superuser '{username}'."))
