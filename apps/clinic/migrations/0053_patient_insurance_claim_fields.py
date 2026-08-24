# Generated manually for CMS-1500 insurance claim patient fields

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("clinic", "0052_patient_digital_intake"),
    ]

    operations = [
        migrations.AddField(
            model_name="patient",
            name="sex",
            field=models.CharField(
                blank=True,
                choices=[("", "—"), ("M", "Male"), ("F", "Female")],
                default="",
                help_text="Used on insurance claims (CMS-1500 box 3).",
                max_length=1,
            ),
        ),
        migrations.AddField(
            model_name="patient",
            name="insurance_payer_name",
            field=models.CharField(
                blank=True,
                default="",
                help_text="Insurance company / payer name for claims.",
                max_length=200,
            ),
        ),
        migrations.AddField(
            model_name="patient",
            name="insurance_member_id",
            field=models.CharField(
                blank=True,
                default="",
                help_text="Insured’s ID / member number (CMS-1500 box 1a).",
                max_length=64,
            ),
        ),
        migrations.AddField(
            model_name="patient",
            name="insurance_group_number",
            field=models.CharField(
                blank=True,
                default="",
                help_text="Group or plan number (CMS-1500 box 11).",
                max_length=64,
            ),
        ),
        migrations.AddField(
            model_name="patient",
            name="insurance_plan_type",
            field=models.CharField(
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
                help_text="CMS-1500 box 1 insurance type.",
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="patient",
            name="insurance_relationship",
            field=models.CharField(
                blank=True,
                choices=[
                    ("", "—"),
                    ("self", "Self"),
                    ("spouse", "Spouse"),
                    ("child", "Child"),
                    ("other", "Other"),
                ],
                default="self",
                help_text="Patient relationship to insured (CMS-1500 box 6).",
                max_length=10,
            ),
        ),
        migrations.AddField(
            model_name="patient",
            name="insured_name",
            field=models.CharField(
                blank=True,
                default="",
                help_text="Insured’s name if different from the patient. Leave blank when relationship is Self.",
                max_length=200,
            ),
        ),
    ]
