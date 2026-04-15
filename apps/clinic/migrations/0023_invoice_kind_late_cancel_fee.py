from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("clinic", "0022_service_is_new_client_intake"),
    ]

    operations = [
        migrations.AlterField(
            model_name="invoice",
            name="kind",
            field=models.CharField(
                choices=[
                    ("visit", "Visit"),
                    ("no_show_fee", "No-show fee"),
                    ("late_cancel_fee", "Late cancellation fee"),
                ],
                default="visit",
                help_text="Visit = normal clinical invoice; no_show_fee / late_cancel_fee = policy penalties.",
                max_length=20,
            ),
        ),
    ]
