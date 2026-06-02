from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("clinic", "0043_patient_document"),
    ]

    operations = [
        # ── Appointment ──────────────────────────────────────────────────────
        migrations.AddIndex(
            model_name="appointment",
            index=models.Index(
                fields=["provider_id", "appointment_date"],
                name="appt_provider_date_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="appointment",
            index=models.Index(
                fields=["patient_id", "appointment_date"],
                name="appt_patient_date_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="appointment",
            index=models.Index(
                fields=["appointment_date", "status"],
                name="appt_date_status_idx",
            ),
        ),
        # ── ProviderUnavailability ────────────────────────────────────────────
        migrations.AddIndex(
            model_name="providerunavailability",
            index=models.Index(
                fields=["provider_id", "block_date"],
                name="unavail_provider_date_idx",
            ),
        ),
        # ── Visit ─────────────────────────────────────────────────────────────
        migrations.AddIndex(
            model_name="visit",
            index=models.Index(
                fields=["provider_id", "status", "completed_at"],
                name="visit_prov_stat_done_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="visit",
            index=models.Index(
                fields=["patient_id", "status", "completed_at"],
                name="visit_pat_stat_done_idx",
            ),
        ),
        # ── VisitRenderedService ───────────────────────────────────────────────
        migrations.AddIndex(
            model_name="visitrenderedservice",
            index=models.Index(
                fields=["visit_id", "charges_patient"],
                name="rendered_svc_visit_charges_idx",
            ),
        ),
        # ── Invoice ───────────────────────────────────────────────────────────
        migrations.AddIndex(
            model_name="invoice",
            index=models.Index(
                fields=["status", "issued_at"],
                name="invoice_status_issued_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="invoice",
            index=models.Index(
                fields=["status", "paid_at"],
                name="invoice_status_paid_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="invoice",
            index=models.Index(
                fields=["kind", "status"],
                name="invoice_kind_status_idx",
            ),
        ),
        # ── Payment ───────────────────────────────────────────────────────────
        migrations.AddIndex(
            model_name="payment",
            index=models.Index(
                fields=["invoice_id", "status"],
                name="payment_invoice_status_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="payment",
            index=models.Index(
                fields=["paid_at"],
                name="payment_paid_at_idx",
            ),
        ),
        # ── PatientCreditTransaction ──────────────────────────────────────────
        migrations.AddIndex(
            model_name="patientcredittransaction",
            index=models.Index(
                fields=["patient_id", "created_at"],
                name="credit_txn_patient_date_idx",
            ),
        ),
        # ── PatientDocument ───────────────────────────────────────────────────
        migrations.AddIndex(
            model_name="patientdocument",
            index=models.Index(
                fields=["patient_id", "created_at"],
                name="patient_doc_patient_date_idx",
            ),
        ),
    ]
