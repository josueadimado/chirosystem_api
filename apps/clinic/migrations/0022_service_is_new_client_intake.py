from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("clinic", "0021_invoice_kind_no_show_fee"),
    ]

    operations = [
        migrations.AddField(
            model_name="service",
            name="is_new_client_intake",
            field=models.BooleanField(
                default=False,
                help_text="If True, online chiropractic booking allows patients returning after a long gap (e.g. 2+ years since last completed chiro visit). Mark one bookable visit type as new patient / reactivation intake.",
            ),
        ),
    ]
