# Audit jurnali uchun HMAC xesh zanjiri (records/audit_chain.py).
# 1) mavjud yozuvlarga zanjir hisoblanadi; 2) Postgres'da UPDATE/DELETE/TRUNCATE ni
#    taqiqlovchi trigger o'rnatiladi (faqat FK'larni NULL qilishga ruxsat - foydalanuvchi o'chirilganda).

from django.db import migrations, models


GUARD_SQL = """
CREATE OR REPLACE FUNCTION records_auditlog_guard() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' OR TG_OP = 'TRUNCATE' THEN
        RAISE EXCEPTION 'Audit jurnali faqat qo''shish uchun: % taqiqlangan', TG_OP;
    END IF;
    IF (NEW.id, NEW.action, NEW.actor_snapshot, NEW.patient_ref, NEW.record_ref, NEW.detail,
        NEW.ip_address, NEW.user_agent, NEW."timestamp", NEW.prev_hash, NEW.entry_hash)
       IS DISTINCT FROM
       (OLD.id, OLD.action, OLD.actor_snapshot, OLD.patient_ref, OLD.record_ref, OLD.detail,
        OLD.ip_address, OLD.user_agent, OLD."timestamp", OLD.prev_hash, OLD.entry_hash) THEN
        RAISE EXCEPTION 'Audit yozuvini o''zgartirib bo''lmaydi (id=%)', OLD.id;
    END IF;
    -- Bog'langan foydalanuvchi/bemor/yozuv o'chirilganda FK NULL bo'ladi - bunga ruxsat
    IF (NEW.actor_id IS NOT NULL AND NEW.actor_id IS DISTINCT FROM OLD.actor_id)
       OR (NEW.patient_id IS NOT NULL AND NEW.patient_id IS DISTINCT FROM OLD.patient_id)
       OR (NEW.medical_record_id IS NOT NULL AND NEW.medical_record_id IS DISTINCT FROM OLD.medical_record_id) THEN
        RAISE EXCEPTION 'Audit yozuvini o''zgartirib bo''lmaydi (id=%)', OLD.id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS records_auditlog_guard_row ON records_auditlog;
CREATE TRIGGER records_auditlog_guard_row BEFORE UPDATE OR DELETE ON records_auditlog
    FOR EACH ROW EXECUTE FUNCTION records_auditlog_guard();
DROP TRIGGER IF EXISTS records_auditlog_guard_truncate ON records_auditlog;
CREATE TRIGGER records_auditlog_guard_truncate BEFORE TRUNCATE ON records_auditlog
    FOR EACH STATEMENT EXECUTE FUNCTION records_auditlog_guard();
"""

DROP_SQL = """
DROP TRIGGER IF EXISTS records_auditlog_guard_row ON records_auditlog;
DROP TRIGGER IF EXISTS records_auditlog_guard_truncate ON records_auditlog;
DROP FUNCTION IF EXISTS records_auditlog_guard();
"""


def backfill_chain(apps, schema_editor):
    from records import audit_chain

    AuditLog = apps.get_model("records", "AuditLog")
    prev = audit_chain.GENESIS
    for entry in AuditLog.objects.select_related("actor").order_by("id").iterator(chunk_size=1000):
        entry.actor_snapshot = audit_chain.actor_snapshot(entry.actor)
        entry.patient_ref = entry.patient_id
        entry.record_ref = entry.medical_record_id
        entry.detail = (entry.detail or "")[:255]
        entry.prev_hash = prev
        entry.entry_hash = audit_chain.compute_hash(entry, prev)
        AuditLog.objects.filter(pk=entry.pk).update(
            actor_snapshot=entry.actor_snapshot, patient_ref=entry.patient_ref, record_ref=entry.record_ref,
            prev_hash=entry.prev_hash, entry_hash=entry.entry_hash,
        )
        prev = entry.entry_hash


def _run_sql(schema_editor, sql):
    # Parametrsiz bajariladi: plpgsql dagi RAISE '%' belgilari psycopg placeholder deb o'qilmasin
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(sql)


def create_guard_trigger(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        _run_sql(schema_editor, GUARD_SQL)


def drop_guard_trigger(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        _run_sql(schema_editor, DROP_SQL)


class Migration(migrations.Migration):

    dependencies = [
        ("records", "0005_prescription"),
    ]

    operations = [
        migrations.CreateModel(
            name="AuditCheckpoint",
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
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("last_id", models.BigIntegerField()),
                ("last_hash", models.CharField(max_length=64)),
                ("count", models.BigIntegerField()),
            ],
            options={
                "ordering": ["-id"],
            },
        ),
        migrations.AddField(
            model_name="auditlog",
            name="actor_snapshot",
            field=models.CharField(blank=True, editable=False, max_length=150),
        ),
        migrations.AddField(
            model_name="auditlog",
            name="entry_hash",
            field=models.CharField(
                blank=True, db_index=True, editable=False, max_length=64
            ),
        ),
        migrations.AddField(
            model_name="auditlog",
            name="patient_ref",
            field=models.IntegerField(blank=True, editable=False, null=True),
        ),
        migrations.AddField(
            model_name="auditlog",
            name="prev_hash",
            field=models.CharField(blank=True, editable=False, max_length=64),
        ),
        migrations.AddField(
            model_name="auditlog",
            name="record_ref",
            field=models.IntegerField(blank=True, editable=False, null=True),
        ),
        migrations.AlterField(
            model_name="auditlog",
            name="timestamp",
            field=models.DateTimeField(editable=False),
        ),
        migrations.RunPython(backfill_chain, migrations.RunPython.noop),
        migrations.RunPython(create_guard_trigger, drop_guard_trigger),
    ]
