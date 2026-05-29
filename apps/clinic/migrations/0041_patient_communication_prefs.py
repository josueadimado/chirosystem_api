from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("clinic", "0040_fix_no_show_appointment_status"),
    ]

    operations = [
        migrations.AddField(
            model_name="patient",
            name="notify_booking",
            field=models.CharField(
                choices=[
                    ("sms", "Text (SMS) only"),
                    ("email", "Email only"),
                    ("both", "Text and email"),
                ],
                default="sms",
                help_text="Booking / reschedule / cancel confirmations. sms = text only, email = email only, both = send on both channels when contact info exists.",
                max_length=10,
            ),
        ),
        migrations.AddField(
            model_name="patient",
            name="notify_reminders",
            field=models.CharField(
                choices=[
                    ("sms", "Text (SMS) only"),
                    ("email", "Email only"),
                    ("both", "Text and email"),
                ],
                default="sms",
                help_text="Day-before and same-day appointment reminders. SMS also requires SMS consent. sms = text only, email = email only, both = send on both channels when contact info exists.",
                max_length=10,
            ),
        ),
        migrations.AddField(
            model_name="patient",
            name="notify_bills",
            field=models.CharField(
                choices=[
                    ("sms", "Text (SMS) only"),
                    ("email", "Email only"),
                    ("both", "Text and email"),
                ],
                default="email",
                help_text="Paid receipt / patient bill email from the portal. sms = text only, email = email only, both = send on both channels when contact info exists.",
                max_length=10,
            ),
        ),
    ]
