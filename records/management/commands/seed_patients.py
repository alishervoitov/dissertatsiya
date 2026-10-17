import random
import secrets
import string
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from records.models import MedicalRecord, Patient, Prescription
from users.models import Role, User

DOCTORS = [
    ("doctor1", "Aziza", "Karimova"),
    ("doctor2", "Bobur", "Rahmonov"),
    ("doctor3", "Dilfuza", "Yusupova"),
    ("doctor4", "Sardor", "Nazarov"),
]
MALE = ["Jasur", "Aziz", "Bekzod", "Sardor", "Farrux", "Otabek", "Sherzod", "Diyor", "Umid", "Rustam"]
FEMALE = ["Malika", "Nilufar", "Gulnora", "Sevara", "Zarina", "Dilnoza", "Kamola", "Feruza", "Madina", "Shoira"]
SURNAMES = ["Toshmatov", "Karimov", "Yusupov", "Rahimov", "Aliyev", "Nazarov", "Xolmatov", "Saidov", "Yoqubov", "Mirzayev"]
BLOOD = ["O+", "A+", "B+", "AB+", "O-", "A-", "B-", "AB-"]
ALLERGIES = ["", "", "Penitsillin", "Changga allergiya", "Yong'oqqa allergiya", "Chang va gul changi"]
CITIES = ["Toshkent", "Andijon", "Samarqand", "Buxoro", "Namangan", "Farg'ona"]

RECORDS = [
    ("diagnosis", "Gripp tashxisi", "O'tkir respirator virusli infeksiya", "Dam olish, ko'p suyuqlik ichish"),
    ("visit", "Profilaktik ko'rik", "Sog'lom", "Tavsiyalar berildi"),
    ("lab_result", "Qon tahlili", "Ko'rsatkichlar me'yorida", "Qo'shimcha davolash talab etilmaydi"),
    ("diagnosis", "Gipertoniya nazorati", "Arterial gipertenziya, I daraja", "Qon bosimini kuzatish, tuz iste'molini cheklash"),
    ("procedure", "Inyeksiya muolajasi", "Mushak ichiga yuborish", "Kurs 5 kun"),
]
MEDS = [
    ("Amoksitsillin", "500mg", "daily_3", 7, "Ovqatdan keyin, ko'p suyuqlik bilan"),
    ("Paratsetamol", "500mg", "as_needed", 5, "Harorat 38 dan oshganda"),
    ("Ibuprofen", "200mg", "daily_2", 5, "Ovqat bilan birga"),
    ("Vitamin D3", "1000 IU", "daily_1", 30, "Ertalab"),
    ("Lizinopril", "10mg", "daily_1", 30, "Bir vaqtda, har kuni"),
]


def make_password():
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(14))


class Command(BaseCommand):
    help = "Demo uchun 4 shifokor va N ta bemor (yozuvlar va retseptlar bilan) yaratadi"

    def add_arguments(self, parser):
        parser.add_argument("--patients", type=int, default=20)

    @transaction.atomic
    def handle(self, *args, **options):
        today = timezone.now().date()
        now = timezone.now()
        creds = []

        doctors = []
        for username, first, last in DOCTORS:
            user = User.objects.filter(username=username).first()
            if user is None:
                password = make_password()
                user = User(username=username, first_name=first, last_name=last,
                            email=f"{username}@example.com", role=Role.DOCTOR)
                user.set_password(password)
                user.save()
                creds.append(("SHIFOKOR", username, password))
            doctors.append(user)

        created = 0
        for n in range(1, options["patients"] + 1):
            username = f"patient{n:02d}"
            if User.objects.filter(username=username).exists():
                continue
            male = random.random() < 0.5
            first = random.choice(MALE if male else FEMALE)
            surname = random.choice(SURNAMES)
            last = surname if male else surname + "a"

            password = make_password()
            user = User(username=username, first_name=first, last_name=last,
                        email=f"{username}@example.com", role=Role.PATIENT)
            user.set_password(password)
            user.save()  # Patient profili signal orqali avtomatik yaratiladi
            creds.append(("BEMOR", username, password))

            doctor = doctors[(n - 1) % len(doctors)]
            patient = Patient.objects.get(user=user)
            age = random.randint(3, 85)
            patient.date_of_birth = today - timedelta(days=age * 365 + random.randint(0, 364))
            patient.gender = "M" if male else "F"
            patient.blood_type = random.choice(BLOOD)
            patient.allergies = random.choice(ALLERGIES)
            patient.national_id = f"DEMO-{n:06d}"
            patient.address = f"{random.choice(CITIES)}, {random.randint(1, 60)}-uy"
            patient.primary_doctor = doctor
            patient.save()

            for _ in range(random.randint(1, 3)):
                rtype, title, diagnosis, treatment = random.choice(RECORDS)
                MedicalRecord.objects.create(
                    patient=patient, created_by=doctor, record_type=rtype, title=title,
                    diagnosis=diagnosis, treatment=treatment,
                    visit_date=now - timedelta(days=random.randint(1, 400), hours=random.randint(0, 8)),
                )

            if random.random() < 0.6:
                name, dosage, freq, days, instr = random.choice(MEDS)
                Prescription.objects.create(
                    patient=patient, prescribed_by=doctor, medication_name=name, dosage=dosage,
                    frequency=freq, duration_days=days, instructions=instr,
                    start_date=today - timedelta(days=random.randint(0, 40)),
                )
            created += 1

        new_doctors = sum(1 for c in creds if c[0] == "SHIFOKOR")
        self.stdout.write(self.style.SUCCESS(
            f"Yaratildi: {len(doctors)} shifokor (yangi: {new_doctors}), {created} bemor."))
        if creds:
            self.stdout.write("\nHISOBLAR (parollar faqat hozir ko'rsatiladi, saqlab qo'ying):")
            for role, username, password in creds:
                self.stdout.write(f"{role:9} {username:12} {password}")