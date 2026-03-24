from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

from apps.clinic.models import Provider, Service


# CPT / fee rows for the doctor visit bill (Patient Bill style). Not shown on public booking.
def _bill_only_rows():
    """(billing_code, display_name, description, price, duration_minutes)"""
    return [
        ("40803", "Miscellaneous", "Miscellaneous", "55.00", 15),
        ("97010 GP 59", "Hot or Cold Pack Application", "Hot or Cold Pack Application", "10.00", 15),
        ("97012 GP 59", "Mechanical Traction", "Mechanical Traction", "28.00", 15),
        ("97014 GP", "E-Stim", "Electrical stimulation", "16.00", 15),
        ("97112 59", "KinesioTaping / Neuro-muscular re-education", "KinesioTaping — neuro-muscular re-education", "30.00", 15),
        ("97140 GP", "Massage manual therapy", "Massage manual therapy (97140)", "70.00", 15),
        (
            "97140 GP",
            "Massage manual therapy (extended)",
            "Massage manual therapy — extended session list price",
            "140.00",
            15,
        ),
        ("98940 AT", "Spinal manipulation 1–2 regions", "Spinal manipulation 1–2 areas", "40.00", 15),
        ("98941 AT", "Spinal manipulation 3–4 regions", "Spinal manipulation 3–4 areas", "50.00", 15),
        ("98942 AT", "Spinal manipulation 5 regions", "Spinal manipulation 5 regions", "70.00", 15),
        ("98943", "Extraspinal — limb / TMJ", "Extraspinal — limb, mandibular joint", "30.00", 15),
        ("99202 25", "New patient — expanded exam", "New patient — expanded", "80.00", 30),
        ("99203 25", "New patient — detailed exam", "New patient — detailed", "120.00", 45),
        ("99204 25", "New patient — extended exam", "New patient — extended", "180.00", 60),
        ("99211 25", "Established patient — basic exam", "Established patient — basic exam", "30.00", 15),
        ("99212 25", "Established patient — moderate exam", "Established patient — moderate exam", "50.00", 15),
        ("99213 25", "Established patient — extended exam", "Established patient — extended exam", "100.00", 30),
        ("NO SHOW", "No show — chiropractic visit", "No show fee for chiropractic visit", "55.00", 15),
        ("NO SHOW", "No show — 30 min massage", "No show fee for 30 minute massage", "65.00", 15),
        ("NO SHOW", "No show — 60 min massage", "No show fee for 60 minute massage", "115.00", 15),
        ("NO SHOW", "No show — 90 min massage", "No show fee for 90 minute massage", "145.00", 15),
        ("NO SHOW", "No show — new patient", "New patient no show fee", "105.00", 15),
        ("NO SHOW FEE", "No show — Medicare", "No show fee — Medicare", "35.00", 15),
    ]


class Command(BaseCommand):
    help = "Seed initial doctors, services, and owner admin account."

    def handle(self, *args, **options):
        self.stdout.write(self.style.NOTICE("Seeding initial clinic data..."))
        User = get_user_model()

        owner, _ = User.objects.update_or_create(
            username="owner_admin",
            defaults={
                "email": "owner@reliefchiropractic.local",
                "full_name": "Relief Chiropractic Admin",
                "role": "owner_admin",
                "is_staff": True,
                "is_superuser": True,
            },
        )
        owner.set_password("Admin123!")
        owner.save(update_fields=["password"])

        # Chiropractor: single provider; booking UI hides provider choice for chiropractic services.
        chiro_username, chiro_name = ("dr_russel_mead", "Dr. Russel Mead")
        chiro_user, _ = User.objects.update_or_create(
            username=chiro_username,
            defaults={
                "email": f"{chiro_username}@reliefchiropractic.local",
                "full_name": chiro_name,
                "role": "doctor",
                "is_staff": False,
                "is_superuser": False,
            },
        )
        chiro_user.set_password("Doctor123!")
        chiro_user.save(update_fields=["password"])
        Provider.objects.update_or_create(
            user=chiro_user,
            defaults={
                "title": "Chiropractor",
                "specialty": "Chiropractic",
                "active": True,
            },
        )

        # Massage: patient chooses among providers linked to each massage service (add more therapists in admin as needed).
        massage_accounts = [
            ("mrs_natalie_peck", "Mrs. Natalie Peck"),
        ]
        for username, full_name in massage_accounts:
            u, _ = User.objects.update_or_create(
                username=username,
                defaults={
                    "email": f"{username}@reliefchiropractic.local",
                    "full_name": full_name,
                    "role": "doctor",
                    "is_staff": False,
                    "is_superuser": False,
                },
            )
            u.set_password("Doctor123!")
            u.save(update_fields=["password"])
            Provider.objects.update_or_create(
                user=u,
                defaults={
                    "title": "Massage Therapist",
                    "specialty": "Massage Therapy",
                    "active": True,
                },
            )

        chiropractic_services = [
            ("Chiropractic initial visit", "98940", 60, "105.00"),
            ("Chiropractic appointment (follow-up)", "98941", 30, "55.00"),
        ]
        massage_services = [
            ("30 minute massage", "97140", 30, "65.00"),
            ("60 minute massage", "97140", 60, "115.00"),
            ("90 minute massage", "97140", 90, "145.00"),
        ]

        chiropractor = Provider.objects.get(user__username=chiro_username)
        massage_providers = list(Provider.objects.filter(user__username__in=[m[0] for m in massage_accounts]))

        for name, billing_code, duration, price in chiropractic_services:
            svc, _ = Service.objects.update_or_create(
                name=name,
                defaults={
                    "description": "Chiropractic service",
                    "billing_code": billing_code,
                    "duration_minutes": duration,
                    "price": price,
                    "is_active": True,
                    "show_in_public_booking": True,
                    "service_type": "chiropractic",
                },
            )
            svc.providers.set([chiropractor])

        for name, billing_code, duration, price in massage_services:
            svc, _ = Service.objects.update_or_create(
                name=name,
                defaults={
                    "description": "Massage service",
                    "billing_code": billing_code,
                    "duration_minutes": duration,
                    "price": price,
                    "is_active": True,
                    "show_in_public_booking": True,
                    "service_type": "massage",
                },
            )
            svc.providers.set(massage_providers)

        for code, title, desc, price, dur in _bill_only_rows():
            svc, _ = Service.objects.update_or_create(
                name=title,
                defaults={
                    "billing_code": code,
                    "description": desc,
                    "duration_minutes": dur,
                    "price": price,
                    "is_active": True,
                    "show_in_public_booking": False,
                    "service_type": Service.ServiceType.CHIROPRACTIC,
                },
            )
            # Bill-only lines: not linked to providers (still billable on any visit).
            svc.providers.clear()

        # Older demo seed used different service names; those rows stay in the DB and
        # still appeared in booking until deactivated.
        legacy_demo_service_names = [
            "Initial consultation and assessment",
            "Standard chiropractic adjustment",
            "Spinal decompression session",
            "Swedish massage (60 min)",
            "Deep tissue massage (45 min)",
        ]
        deactivated = Service.objects.filter(name__in=legacy_demo_service_names, is_active=True).update(
            is_active=False
        )
        if deactivated:
            self.stdout.write(self.style.WARNING(f"Deactivated {deactivated} legacy demo service(s)."))

        self.stdout.write(self.style.SUCCESS("Seeding complete."))
        self.stdout.write("Owner login: owner_admin / Admin123!")
        self.stdout.write("Chiropractor login: dr_russel_mead / Doctor123!")
        self.stdout.write("Massage therapist login: mrs_natalie_peck / Doctor123!")
