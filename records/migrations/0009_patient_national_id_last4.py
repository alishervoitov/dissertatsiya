import re

from django.db import migrations, models


def fill_last4(apps, schema_editor):
    """Mavjud bemorlar uchun: shifrlangan hujjat raqamidan oxirgi 4 raqam."""
    Patient = apps.get_model("records", "Patient")
    for p in Patient.objects.exclude(national_id="").only("id", "national_id").iterator():
        digits = re.sub(r"\D", "", p.national_id or "")
        if len(digits) >= 4:
            Patient.objects.filter(pk=p.pk).update(national_id_last4=digits[-4:])


class Migration(migrations.Migration):

    dependencies = [
        ("records", "0008_patient_photo"),
    ]

    operations = [
        migrations.AddField(
            model_name="patient",
            name="national_id_last4",
            field=models.CharField(blank=True, db_index=True, editable=False, max_length=4),
        ),
        migrations.RunPython(fill_last4, migrations.RunPython.noop),
    ]
