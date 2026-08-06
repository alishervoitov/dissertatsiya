import random
from datetime import date, timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from records.models import MedicalRecord, Patient
from users.models import Role, User

FIRST_NAMES_M = ["Jasur", "Aziz", "Bekzod", "Sardor", "Farrux", "Otabek", "Sherzod", "Diyor", "Umid", "Rustam"]
FIRST_NAMES_F = ["Malika", "Nilufar", "Gulnora", "Sevara", "Zarina", "Dilnoza", "Kamola", "Feruza", "Madina", "Shoira"]
LAST_NAMES = ["Toshev", "Karimov", "Yusupov", "Rahimov", "Aliyev", "Nazarov", "Xolmatov", "Saidov", "Yoqubov", "Mirzayev"]
BLOOD_TYPES = ["O+", "A+", "B+", "AB+", "O-", "A-", "B-", "AB-"]
GENDERS = ["M", "F"]
RECORD_TITLES = [
    ("diagnosis", "Gripp tashxisi", "O'tkir respirator virusli infeksiya", "Dam olish, ko'p suyuqlik ichish"),
    ("visit", "Profilaktik ko'rik", "Sog'lom", "Tavsiyalar berildi"),
    ("lab_result", "Qon tahlili", "Ko'rsatkichlar me'yorida", "Qo'shimcha davolash talab etilmaydi"),
    ("prescription", "Antibiotik retsepti", "Bakterial infeksiya", "Amoksitsillin 7 kun"),
]


class Command(BaseCommand):
    help = "k-anonimlik sinovlari uchun test bemorlar va tibbiy yozuvlar yaratadi"

    def add_arguments(self, parser):
        parser.add_argument("--count", type=int, default=20, help="Nechta bemor yaratish kerak")

    def handle(self, *args, **options):
        count = options["count"]
        doctor = User.objects.filter(role=Role.DOCTOR).first()
        if not doctor:
            self.stdout.write(self.style.ERROR("Avval kamida bitta shifokor yarating."))
            return

        created = 0
        for i in range(count):
            gender = random.choice(GENDERS)
            first_name = random.choice(FIRST_NAMES_M if gender == "M" else FIRST_NAMES_F)
            last_name = random.choice(LAST_NAMES)
            username = f"test_patient_{i}_{random.randint(1000,9999)}"

            if User.objects.filter(username=username).exists():
                continue

            user = User(
                username=username, first_name=first_name, last_name=last_name,
                email=f"{username}@example.com", role=Role.PATIENT,
            )
            user.set_password("TestPass123!")
            user.save()

            # Patient profili signal orqali avtomatik yaratiladi (post_save)
            patient = Patient.objects.get(user=user)
            age = random.randint(5, 75)
            patient.date_of_birth = date.today() - timedelta(days=age * 365)
            patient.gender = gender
            patient.blood_type = random.choice(BLOOD_TYPES)
            patient.save()

            # Har bir bemorga 1-3 ta tasodifiy tibbiy yozuv
            for _ in range(random.randint(1, 3)):
                record_type, title, diagnosis, treatment = random.choice(RECORD_TITLES)
                MedicalRecord.objects.create(
                    patient=patient, created_by=doctor, record_type=record_type,
                    title=title, diagnosis=diagnosis, treatment=treatment,
                    visit_date=timezone.now() - timedelta(days=random.randint(1, 300)),
                )

            created += 1

        self.stdout.write(self.style.SUCCESS(f"{created} ta test bemor muvaffaqiyatli yaratildi."))