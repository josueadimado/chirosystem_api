from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("clinic", "0038_add_timezone_to_clinic_settings"),
    ]

    operations = [
        migrations.CreateModel(
            name="DiagnosisCode",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("code", models.CharField(max_length=32, unique=True)),
                ("description", models.CharField(max_length=500)),
                ("is_active", models.BooleanField(default=True)),
            ],
            options={
                "verbose_name": "Diagnosis code",
                "verbose_name_plural": "Diagnosis codes",
                "ordering": ["code"],
            },
        ),
        migrations.CreateModel(
            name="VisitDiagnosis",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("code", models.CharField(max_length=32)),
                ("description", models.CharField(max_length=500)),
                (
                    "diagnosis",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="visit_rows",
                        to="clinic.diagnosiscode",
                    ),
                ),
                (
                    "visit",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="visit_diagnoses",
                        to="clinic.visit",
                    ),
                ),
            ],
            options={
                "ordering": ["id"],
            },
        ),
        migrations.AlterField(
            model_name="visit",
            name="diagnosis",
            field=models.TextField(
                blank=True,
                help_text="Formatted diagnosis lines for bills and chart (synced from visit_diagnoses when using the catalog).",
            ),
        ),
    ]
