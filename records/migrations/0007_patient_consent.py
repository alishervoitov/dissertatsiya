# Bemor roziligi (CareGrant) va favqulodda kirish (EmergencyAccess).
# Mavjud davolash munosabatlari (shifokor yozuv/retsept yozgan yoki asosiy shifokor bo'lgan bemorlar)
# muddatsiz ruxsat sifatida ko'chiriladi - joylashtirilganda shifokorlar o'z bemorlarini yo'qotmaydi.

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def backfill_existing_care(apps, schema_editor):
    Patient = apps.get_model("records", "Patient")
    MedicalRecord = apps.get_model("records", "MedicalRecord")
    Prescription = apps.get_model("records", "Prescription")
    CareGrant = apps.get_model("records", "CareGrant")
    User = apps.get_model("users", "User")
    doctors = set(User.objects.filter(role="doctor").values_list("id", flat=True))
    pairs = set()
    pairs |= {(p, d) for p, d in MedicalRecord.objects.values_list("patient_id", "created_by_id") if d in doctors}
    pairs |= {(p, d) for p, d in Prescription.objects.values_list("patient_id", "prescribed_by_id") if d in doctors}
    pairs |= {(p, d) for p, d in Patient.objects.exclude(primary_doctor=None).values_list("id", "primary_doctor_id") if d in doctors}
    CareGrant.objects.bulk_create([
        CareGrant(patient_id=p, doctor_id=d, note="Mavjud davolash munosabati (migratsiya)") for p, d in sorted(pairs)
    ])


class Migration(migrations.Migration):

    dependencies = [
        ("records", "0006_audit_hash_chain"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="CareGrant",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("note", models.CharField(blank=True, max_length=100)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "expires_at",
                    models.DateTimeField(
                        blank=True, help_text="Bo'sh - muddatsiz", null=True
                    ),
                ),
                ("revoked_at", models.DateTimeField(blank=True, null=True)),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "doctor",
                    models.ForeignKey(
                        limit_choices_to={"role": "doctor"},
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="care_grants",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "patient",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="care_grants",
                        to="records.patient",
                    ),
                ),
            ],
            options={
                "ordering": ["-created_at"],
                "indexes": [
                    models.Index(
                        fields=["doctor", "patient", "revoked_at"],
                        name="records_car_doctor__3eee0c_idx",
                    )
                ],
            },
        ),
        migrations.CreateModel(
            name="EmergencyAccess",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("reason", models.TextField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("expires_at", models.DateTimeField()),
                (
                    "doctor",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="emergency_accesses",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "patient",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="emergency_accesses",
                        to="records.patient",
                    ),
                ),
            ],
            options={
                "ordering": ["-created_at"],
                "indexes": [
                    models.Index(
                        fields=["doctor", "patient", "expires_at"],
                        name="records_eme_doctor__c37c72_idx",
                    )
                ],
            },
        ),
        migrations.RunPython(backfill_existing_care, migrations.RunPython.noop),
    ]
