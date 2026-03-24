# Generated manually for VoiceCallLog

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("clinic", "0012_providerunavailability"),
    ]

    operations = [
        migrations.CreateModel(
            name="VoiceCallLog",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("call_sid", models.CharField(db_index=True, max_length=64, unique=True)),
                ("from_number", models.CharField(blank=True, max_length=32)),
                ("transcript", models.TextField(blank=True)),
                (
                    "outcome",
                    models.CharField(
                        choices=[
                            ("prompted", "Greeting played"),
                            ("no_openai", "OpenAI not configured"),
                            ("empty_speech", "No speech detected"),
                            ("openai_failed", "Could not understand (AI)"),
                            ("intent_incomplete", "Missing name, service, or time"),
                            ("serializer_rejected", "Data did not validate"),
                            ("slot_or_rule_error", "Slot taken or not bookable"),
                            ("booked", "Appointment created"),
                            ("abandoned_retries", "Hung up after retries"),
                        ],
                        default="prompted",
                        max_length=32,
                    ),
                ),
                ("detail", models.TextField(blank=True)),
                (
                    "appointment",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="voice_call_logs",
                        to="clinic.appointment",
                    ),
                ),
            ],
            options={
                "ordering": ["-updated_at"],
            },
        ),
        migrations.AddIndex(
            model_name="voicecalllog",
            index=models.Index(fields=["created_at"], name="clinic_voicelog_created_at"),
        ),
        migrations.AddIndex(
            model_name="voicecalllog",
            index=models.Index(fields=["outcome", "created_at"], name="clinic_voicelog_outcome_created"),
        ),
    ]
