# Generated manually for patient vs insurance-only bill lines.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("clinic", "0027_appointment_reminder_sms_email_fields"),
    ]

    operations = [
        migrations.AddField(
            model_name="service",
            name="charges_patient",
            field=models.BooleanField(
                default=True,
                help_text="If True (typical visit/product), this line is included in the amount the patient pays. "
                "If False, the service still appears on the printed bill with CPT/description for insurance reimbursement, "
                "but its fee is not added to the invoice total.",
            ),
        ),
        migrations.AddField(
            model_name="visitrenderedservice",
            name="charges_patient",
            field=models.BooleanField(
                default=True,
                help_text="If False, this line is shown on the printed bill for insurance but not included in patient invoice totals.",
            ),
        ),
    ]
