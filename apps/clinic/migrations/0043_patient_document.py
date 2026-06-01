import apps.clinic.models
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("clinic", "0042_auto_no_show_automation"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="PatientDocument",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "file",
                    models.FileField(upload_to=apps.clinic.models._patient_document_upload_path),
                ),
                ("original_filename", models.CharField(max_length=255)),
                (
                    "label",
                    models.CharField(
                        help_text="Short descriptive name shown in the chart (e.g. 'Blue Cross card front').",
                        max_length=200,
                    ),
                ),
                (
                    "doc_type",
                    models.CharField(
                        choices=[
                            ("insurance_card", "Insurance Card"),
                            ("x_ray", "X-Ray / Imaging"),
                            ("lab_result", "Lab Result"),
                            ("referral", "Referral Letter"),
                            ("intake_form", "Intake Form"),
                            ("other", "Other"),
                        ],
                        default="other",
                        max_length=30,
                    ),
                ),
                (
                    "patient",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="documents",
                        to="clinic.patient",
                    ),
                ),
                (
                    "uploaded_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "ordering": ["-created_at"],
            },
        ),
    ]
