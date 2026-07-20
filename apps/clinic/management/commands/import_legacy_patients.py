"""
CLI wrapper around apps.clinic.legacy_patient_import.

  python manage.py import_legacy_patients --file "/path/to/patients.xlsx" --dry-run
  python manage.py import_legacy_patients --file "/path/to/patients.xlsx" --commit
"""

from __future__ import annotations

import csv
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from apps.clinic.legacy_patient_import import LegacyImportError, run_legacy_patient_import


class Command(BaseCommand):
    help = "Import legacy Excel patients (skip existing; add missing with historical last visit)."

    def add_arguments(self, parser):
        parser.add_argument("--file", required=True, help="Path to the .xlsx export")
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Analyze only; do not write (default if --commit is omitted).",
        )
        parser.add_argument(
            "--commit",
            action="store_true",
            help="Actually create patients and historical visits.",
        )
        parser.add_argument(
            "--provider-id",
            type=int,
            default=None,
            help="Provider PK for historical visits (default: first active chiropractor).",
        )
        parser.add_argument(
            "--report",
            default="",
            help="Optional CSV path for a summary (counts only; full detail is in API sample).",
        )

    def handle(self, *args, **options):
        path = Path(options["file"]).expanduser()
        if not path.is_file():
            raise CommandError(f"File not found: {path}")

        commit = bool(options["commit"])
        dry_run = bool(options["dry_run"]) or not commit
        if commit and options["dry_run"]:
            raise CommandError("Use either --dry-run or --commit, not both.")

        try:
            result = run_legacy_patient_import(
                path=path,
                dry_run=dry_run,
                provider_id=options["provider_id"],
            )
        except LegacyImportError as exc:
            raise CommandError(str(exc)) from exc

        counts = result["counts"]
        mode = "DRY-RUN (nothing saved)" if dry_run else "COMMIT"
        self.stdout.write(self.style.SUCCESS(f"\n=== {mode} ==="))
        self.stdout.write(
            f"Provider: #{result['provider_id']} ({result['provider_name']})"
        )
        for k, v in counts.items():
            self.stdout.write(f"  {k}: {v}")

        report_path = options["report"] or str(
            path.with_name(path.stem + ("_import_dry_run.csv" if dry_run else "_import_commit.csv"))
        )
        with open(report_path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(
                f,
                fieldnames=[
                    "row",
                    "legacy_id",
                    "first_name",
                    "last_name",
                    "dob",
                    "phone",
                    "last_visit",
                    "action",
                    "detail",
                ],
            )
            w.writeheader()
            w.writerows(result.get("sample_rows") or [])
        self.stdout.write(f"Sample report: {report_path}")
        if dry_run:
            self.stdout.write(
                self.style.NOTICE("\nReview the counts, then re-run with --commit to import.")
            )
