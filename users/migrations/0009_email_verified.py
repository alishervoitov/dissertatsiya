from django.db import migrations, models


def grandfather_staff(apps, schema_editor):
    """Xodim emaillarini administrator kiritgan - ular tasdiqlangan hisoblanadi.
    Bemorlarning mavjud emaillari tekshirilmagan, ular o'zi tasdiqlaydi."""
    User = apps.get_model("users", "User")
    User.objects.exclude(role="patient").exclude(email="").update(email_verified=True)


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0008_temp_password"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="email_verified",
            field=models.BooleanField(default=False),
        ),
        migrations.RunPython(grandfather_staff, migrations.RunPython.noop),
    ]
