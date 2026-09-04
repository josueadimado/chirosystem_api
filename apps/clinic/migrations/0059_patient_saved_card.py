# Multi-card on file per patient.

from django.db import migrations, models
import django.db.models.deletion


def forwards_seed_saved_cards(apps, schema_editor):
    Patient = apps.get_model("clinic", "Patient")
    PatientSavedCard = apps.get_model("clinic", "PatientSavedCard")
    for p in Patient.objects.exclude(square_card_id="").iterator():
        card_id = (p.square_card_id or "").strip()
        if not card_id:
            continue
        PatientSavedCard.objects.get_or_create(
            patient_id=p.id,
            square_card_id=card_id,
            defaults={
                "card_brand": (p.card_brand or "")[:40],
                "card_last4": (p.card_last4 or "")[:4],
                "is_default": True,
                "enabled": True,
            },
        )


def backwards_noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("clinic", "0058_patient_profile_update_token"),
    ]

    operations = [
        migrations.CreateModel(
            name="PatientSavedCard",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("square_card_id", models.CharField(db_index=True, max_length=255)),
                ("card_brand", models.CharField(blank=True, default="", max_length=40)),
                ("card_last4", models.CharField(blank=True, default="", max_length=4)),
                ("is_default", models.BooleanField(db_index=True, default=False)),
                (
                    "enabled",
                    models.BooleanField(
                        default=True,
                        help_text="False when staff removed the card or Square disabled it.",
                    ),
                ),
                (
                    "patient",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="saved_cards",
                        to="clinic.patient",
                    ),
                ),
            ],
            options={
                "ordering": ["-is_default", "-created_at"],
            },
        ),
        migrations.AddConstraint(
            model_name="patientsavedcard",
            constraint=models.UniqueConstraint(
                fields=("patient", "square_card_id"),
                name="uniq_patient_square_card",
            ),
        ),
        migrations.AddIndex(
            model_name="patientsavedcard",
            index=models.Index(fields=["patient", "enabled", "is_default"], name="pat_card_en_def_idx"),
        ),
        migrations.RunPython(forwards_seed_saved_cards, backwards_noop),
    ]
