from django.db import migrations


def create_profiles(apps, schema_editor):
    User = apps.get_model("users", "User")
    DoctorProfile = apps.get_model("appointments", "DoctorProfile")
    for user in User.objects.filter(role="doctor"):
        DoctorProfile.objects.get_or_create(user=user)


class Migration(migrations.Migration):
    dependencies = [("appointments", "0001_initial")]
    operations = [migrations.RunPython(create_profiles, migrations.RunPython.noop)]
