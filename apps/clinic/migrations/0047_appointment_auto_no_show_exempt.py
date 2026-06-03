from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("clinic", "0046_appointment_series"),
    ]

    operations = [
        migrations.AddField(
            model_name="appointment",
            name="auto_no_show_exempt",
            field=models.BooleanField(
                default=False,
                help_text="When true, this visit is skipped by automatic no-show (staff can still mark no-show manually).",
            ),
        ),
    ]
