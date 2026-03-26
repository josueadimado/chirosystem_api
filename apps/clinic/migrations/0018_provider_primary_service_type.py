from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("clinic", "0017_provider_verbose_name_doctor"),
    ]

    operations = [
        migrations.AddField(
            model_name="provider",
            name="primary_service_type",
            field=models.CharField(
                choices=[("chiropractic", "Chiropractic"), ("massage", "Massage")],
                default="chiropractic",
                help_text="Which online visit types this doctor is listed under by default (chiropractic vs massage).",
                max_length=20,
            ),
        ),
    ]
