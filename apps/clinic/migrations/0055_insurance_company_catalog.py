# Generated manually for insurance company catalog + patient assignment

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("clinic", "0053_patient_insurance_claim_fields"),
    ]

    operations = [
        migrations.CreateModel(
            name="InsuranceCompany",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("name", models.CharField(max_length=200, unique=True)),
                (
                    "claim_email",
                    models.EmailField(
                        blank=True,
                        default="",
                        help_text="Default email for sending CMS-1500 claims to this payer.",
                        max_length=254,
                    ),
                ),
                ("phone", models.CharField(blank=True, default="", max_length=40)),
                ("notes", models.CharField(blank=True, default="", max_length=500)),
                (
                    "default_plan_type",
                    models.CharField(
                        blank=True,
                        choices=[
                            ("", "—"),
                            ("medicare", "Medicare"),
                            ("medicaid", "Medicaid"),
                            ("tricare", "TRICARE"),
                            ("champva", "CHAMPVA"),
                            ("group", "Group health plan"),
                            ("feca", "FECA"),
                            ("other", "Other"),
                        ],
                        default="group",
                        help_text="Suggested CMS-1500 box 1 type when this payer is assigned to a patient.",
                        max_length=20,
                    ),
                ),
                ("is_active", models.BooleanField(default=True)),
            ],
            options={
                "verbose_name": "Insurance company",
                "verbose_name_plural": "Insurance companies",
                "ordering": ["name"],
            },
        ),
        migrations.AddField(
            model_name="patient",
            name="insurance_company",
            field=models.ForeignKey(
                blank=True,
                help_text="Catalog insurance payer assigned to this patient (optional). Name is copied to insurance_payer_name for claims.",
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="patients",
                to="clinic.insurancecompany",
            ),
        ),
    ]
