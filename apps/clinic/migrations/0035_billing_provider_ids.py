from django.db import migrations, models


def copy_employer_id_to_provider_billing_id(apps, schema_editor):
    ClinicSettings = apps.get_model("clinic", "ClinicSettings")
    for row in ClinicSettings.objects.all():
        if not (row.provider_billing_id or "").strip() and (row.employer_tax_id or "").strip():
            row.provider_billing_id = row.employer_tax_id
            row.save(update_fields=["provider_billing_id"])


class Migration(migrations.Migration):
    dependencies = [
        ("clinic", "0034_voicecalllog_conversation_log"),
    ]

    operations = [
        migrations.AddField(
            model_name="clinicsettings",
            name="provider_billing_id",
            field=models.CharField(
                blank=True,
                default="",
                help_text="Clinic-wide billing provider ID printed on all patient bills (e.g. NPI). "
                "Used when a doctor has no per-provider ID set.",
                max_length=32,
            ),
        ),
        migrations.AddField(
            model_name="provider",
            name="billing_provider_id",
            field=models.CharField(
                blank=True,
                default="",
                help_text="Optional. Billing provider ID (e.g. NPI) printed on this doctor's patient bills. "
                "When blank, the clinic-wide Provider ID from Settings is used.",
                max_length=32,
            ),
        ),
        migrations.RunPython(copy_employer_id_to_provider_billing_id, migrations.RunPython.noop),
    ]
