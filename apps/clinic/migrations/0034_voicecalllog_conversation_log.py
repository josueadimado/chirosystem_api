from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("clinic", "0033_service_public_booking_name"),
    ]

    operations = [
        migrations.AddField(
            model_name="voicecalllog",
            name="conversation_log",
            field=models.JSONField(blank=True, default=list),
        ),
    ]
