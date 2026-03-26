from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("clinic", "0018_provider_primary_service_type"),
    ]

    operations = [
        migrations.AddField(
            model_name="service",
            name="visible_to_chiropractic_staff",
            field=models.BooleanField(
                default=True,
                help_text="If True, chiropractic doctors see this service in the in-room bill picker.",
            ),
        ),
        migrations.AddField(
            model_name="service",
            name="visible_to_massage_staff",
            field=models.BooleanField(
                default=True,
                help_text="If True, massage therapists see this service in the in-room bill picker.",
            ),
        ),
    ]
