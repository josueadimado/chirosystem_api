"""
Add performance indexes. Uses IF NOT EXISTS so deploy succeeds when indexes were
already created (e.g. partial prior migrate or manual DBA work).
"""

from django.db import migrations, models


FORWARD_SQL = """
CREATE INDEX IF NOT EXISTS appt_provider_date_idx ON clinic_appointment (provider_id, appointment_date);
CREATE INDEX IF NOT EXISTS appt_patient_date_idx ON clinic_appointment (patient_id, appointment_date);
CREATE INDEX IF NOT EXISTS appt_date_status_idx ON clinic_appointment (appointment_date, status);
CREATE INDEX IF NOT EXISTS unavail_provider_date_idx ON clinic_providerunavailability (provider_id, block_date);
CREATE INDEX IF NOT EXISTS visit_prov_stat_done_idx ON clinic_visit (provider_id, status, completed_at);
CREATE INDEX IF NOT EXISTS visit_pat_stat_done_idx ON clinic_visit (patient_id, status, completed_at);
CREATE INDEX IF NOT EXISTS rendered_svc_visit_charges_idx ON clinic_visitrenderedservice (visit_id, charges_patient);
CREATE INDEX IF NOT EXISTS invoice_status_issued_idx ON clinic_invoice (status, issued_at);
CREATE INDEX IF NOT EXISTS invoice_status_paid_idx ON clinic_invoice (status, paid_at);
CREATE INDEX IF NOT EXISTS invoice_kind_status_idx ON clinic_invoice (kind, status);
CREATE INDEX IF NOT EXISTS payment_invoice_status_idx ON clinic_payment (invoice_id, status);
CREATE INDEX IF NOT EXISTS payment_paid_at_idx ON clinic_payment (paid_at);
CREATE INDEX IF NOT EXISTS credit_txn_patient_date_idx ON clinic_patientcredittransaction (patient_id, created_at);
CREATE INDEX IF NOT EXISTS patient_doc_patient_date_idx ON clinic_patientdocument (patient_id, created_at);
"""

REVERSE_SQL = """
DROP INDEX IF EXISTS appt_provider_date_idx;
DROP INDEX IF EXISTS appt_patient_date_idx;
DROP INDEX IF EXISTS appt_date_status_idx;
DROP INDEX IF EXISTS unavail_provider_date_idx;
DROP INDEX IF EXISTS visit_prov_stat_done_idx;
DROP INDEX IF EXISTS visit_pat_stat_done_idx;
DROP INDEX IF EXISTS rendered_svc_visit_charges_idx;
DROP INDEX IF EXISTS invoice_status_issued_idx;
DROP INDEX IF EXISTS invoice_status_paid_idx;
DROP INDEX IF EXISTS invoice_kind_status_idx;
DROP INDEX IF EXISTS payment_invoice_status_idx;
DROP INDEX IF EXISTS payment_paid_at_idx;
DROP INDEX IF EXISTS credit_txn_patient_date_idx;
DROP INDEX IF EXISTS patient_doc_patient_date_idx;
"""


class Migration(migrations.Migration):

    dependencies = [
        ("clinic", "0043_patient_document"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunSQL(FORWARD_SQL, REVERSE_SQL),
            ],
            state_operations=[
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
                migrations.AddIndex(
                    model_name="providerunavailability",
                    index=models.Index(
                        fields=["provider_id", "block_date"],
                        name="unavail_provider_date_idx",
                    ),
                ),
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
                migrations.AddIndex(
                    model_name="visitrenderedservice",
                    index=models.Index(
                        fields=["visit_id", "charges_patient"],
                        name="rendered_svc_visit_charges_idx",
                    ),
                ),
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
                migrations.AddIndex(
                    model_name="patientcredittransaction",
                    index=models.Index(
                        fields=["patient_id", "created_at"],
                        name="credit_txn_patient_date_idx",
                    ),
                ),
                migrations.AddIndex(
                    model_name="patientdocument",
                    index=models.Index(
                        fields=["patient_id", "created_at"],
                        name="patient_doc_patient_date_idx",
                    ),
                ),
            ],
        ),
    ]
