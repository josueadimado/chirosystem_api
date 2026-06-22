from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("clinic", "0049_systemerrorlog"),
    ]

    operations = [
        migrations.AddField(
            model_name="clinicsettings",
            name="error_tracker_password_hash",
            field=models.CharField(
                blank=True,
                default="",
                help_text="Hashed password for the owner-only admin error tracker page.",
                max_length=128,
            ),
        ),
    ]
