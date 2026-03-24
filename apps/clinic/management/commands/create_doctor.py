"""
Create a doctor user with a password for testing.
Usage:
  python manage.py create_doctor <username> <password> [full_name]

Example:
  python manage.py create_doctor dr_smith MyPassword123! "Dr. Smith"
"""
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

from apps.clinic.models import Provider, Service


class Command(BaseCommand):
    help = "Create a doctor user with password and link to Provider."

    def add_arguments(self, parser):
        parser.add_argument("username", type=str, help="Login username (e.g. dr_smith)")
        parser.add_argument("password", type=str, help="Password for the doctor")
        parser.add_argument(
            "full_name",
            type=str,
            nargs="?",
            default="",
            help="Display name (e.g. Dr. Smith). If omitted, derived from username.",
        )

    def handle(self, *args, **options):
        User = get_user_model()
        username = options["username"]
        password = options["password"]
        full_name = options["full_name"] or username.replace("_", " ").title()

        doctor, created = User.objects.update_or_create(
            username=username,
            defaults={
                "email": f"{username}@reliefchiropractic.local",
                "full_name": full_name,
                "role": "doctor",
                "is_staff": False,
                "is_superuser": False,
            },
        )
        doctor.set_password(password)
        doctor.save(update_fields=["password"])

        provider, _ = Provider.objects.update_or_create(
            user=doctor,
            defaults={
                "title": "Doctor",
                "specialty": "Chiropractic",
                "active": True,
            },
        )

        # Assign all active services to this provider
        services = Service.objects.filter(is_active=True)
        provider.services.set(services)

        if created:
            self.stdout.write(self.style.SUCCESS(f"Created doctor: {username} / {full_name}"))
        else:
            self.stdout.write(self.style.SUCCESS(f"Updated doctor: {username} / {full_name}"))

        self.stdout.write(f"Login: {username} / [your password]")
        self.stdout.write("Use the sign-in page to log in as this doctor.")
