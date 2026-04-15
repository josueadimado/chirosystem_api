from decimal import Decimal

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("clinic", "0020_merge_confirmed_into_booked"),
    ]

    operations = [
        migrations.AddField(
            model_name="clinicsettings",
            name="no_show_fee",
            field=models.DecimalField(
                decimal_places=2,
                default=Decimal("25.00"),
                help_text="Amount charged when staff marks no-show (USD). Set to 0 to skip fee and invoice. If a card is on file, it is charged automatically; otherwise the visit stays in Awaiting payment.",
                max_digits=10,
            ),
        ),
        migrations.AddField(
            model_name="invoice",
            name="kind",
            field=models.CharField(
                choices=[("visit", "Visit"), ("no_show_fee", "No-show fee")],
                default="visit",
                help_text="Visit = normal clinical invoice; no_show_fee = missed-appointment fee.",
                max_length=20,
            ),
        ),
    ]
