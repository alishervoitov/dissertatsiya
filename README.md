# MedKarta — Bemor kasallik tarixi tizimi (Backend)

Tibbiy ma'lumotlar xavfsizligini ta'minlovchi bemorlar kasallik tarixini boshqarish tizimining backend qismi. Django REST Framework asosida qurilgan, magistrlik dissertatsiyasi doirasida ishlab chiqilgan.

## Texnologiyalar

- **Python 3.14** / **Django 6.0**
- **Django REST Framework** — RESTful API
- **PostgreSQL** — ma'lumotlar bazasi
- **djangorestframework-simplejwt** — JWT autentifikatsiya
- **cryptography (Fernet)** — maydon darajasida shifrlash (AES-128)
- **django-cors-headers** — CORS boshqaruvi

## Asosiy imkoniyatlar

- 🔐 **JWT autentifikatsiya** — qisqa muddatli access token, rotatsiyalanuvchi refresh token
- 👥 **Rolga asoslangan kirishni boshqarish (RBAC)** — bemor / shifokor / administrator
- 🔒 **Ma'lumotlarni shifrlash** — tashxis, davolash, shaxsiy ma'lumotlar bazada Fernet (AES-128) bilan shifrlangan holda saqlanadi
- 📋 **Audit jurnali** — barcha ko'rish/yaratish/tahrirlash harakatlari o'zgartirib bo'lmaydigan holda qayd etiladi (kim, qachon, qaysi IP'dan)
- 🛡 **Brute-force himoyasi** — login urinishlari tezlikni cheklash (throttling) orqali cheklangan
- 📊 **k-anonimlik moduli** — bemorlar ma'lumotlarini tadqiqot/statistika uchun anonimlashtirish (k-anonimlik, l-diversity ko'rsatkichlari bilan)

## O'rnatish

### 1. Repozitoriyni klonlash

\`\`\`bash
git clone <repo-url>
cd dissertatsiya
\`\`\`

### 2. Virtual muhit va kutubxonalar

\`\`\`bash
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -r requirements.txt
\`\`\`

### 3. PostgreSQL bazasini sozlash

PostgreSQL'da yangi baza yarating:

\`\`\`sql
CREATE DATABASE dissertatsiya;
\`\`\`

### 4. `.env` faylini sozlash

Loyiha ildizida `.env` fayl yarating (`.env.example`dan nusxa oling):

\`\`\`
SECRET_KEY=your-secret-key-here
DEBUG=True
FIELD_ENCRYPTION_KEY=your-fernet-key-here

DB_NAME=dissertatsiya
DB_USER=postgres
DB_PASSWORD=your-password
DB_HOST=127.0.0.1
DB_PORT=5432

CORS_ALLOWED_ORIGINS=http://localhost:3000,http://127.0.0.1:3000
\`\`\`

Shifrlash kalitini generatsiya qilish:
\`\`\`bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
\`\`\`

### 5. Migratsiya va superuser

\`\`\`bash
python manage.py migrate
python manage.py createsuperuser
\`\`\`

### 6. Serverni ishga tushirish

\`\`\`bash
python manage.py runserver
\`\`\`

API `http://127.0.0.1:8000/api/` manzilida ishga tushadi.

## Loyiha tuzilishi

\`\`\`
dissertatsiya/
├── config/              # Django sozlamalari (settings, urls)
├── users/                # Foydalanuvchi modeli, autentifikatsiya
│   ├── models.py         # User modeli (role: patient/doctor/admin)
│   ├── views.py          # Login, register, foydalanuvchi CRUD
│   └── permissions.py    # IsAdmin, IsAdminOrDoctor
├── records/              # Bemorlar, tibbiy yozuvlar, audit
│   ├── models.py         # Patient, MedicalRecord, AuditLog
│   ├── encryption.py     # EncryptedTextField, EncryptedCharField (Fernet)
│   ├── permissions.py    # Obyekt darajasidagi ruxsatlar
│   ├── audit.py           # Audit jurnali yordamchi funksiyasi
│   ├── anonymization.py  # k-anonimlik algoritmi
│   └── views.py
└── manage.py
\`\`\`

## API endpointlari (qisqacha)

| Metod | Endpoint | Tavsif | Ruxsat |
|---|---|---|---|
| POST | `/api/auth/register/` | Bemor sifatida ro'yxatdan o'tish | Ochiq |
| POST | `/api/auth/login/` | Tizimga kirish (JWT) | Ochiq |
| GET | `/api/auth/me/` | Joriy foydalanuvchi | Autentifikatsiya |
| POST | `/api/auth/users/` | Shifokor/admin yaratish | Faqat admin |
| GET | `/api/records/patients/` | Bemorlar ro'yxati | Shifokor/admin |
| GET/PATCH | `/api/records/patients/:id/` | Bemor kartasi | Rolga bog'liq |
| GET/POST | `/api/records/medical-records/` | Tibbiy yozuvlar | Rolga bog'liq |
| GET | `/api/records/audit-logs/` | Audit jurnali | Faqat admin |
| GET | `/api/records/export-anonymized/` | Anonimlashtirilgan eksport | Faqat admin |

## Xavfsizlik arxitekturasi

Loyiha ikki qatlamli himoya modeliga asoslangan:

1. **Operatsion xavfsizlik** — kundalik foydalanishda (RBAC, JWT, shifrlash, audit)
2. **Ikkilamchi foydalanish xavfsizligi** — ma'lumot tadqiqot/statistika uchun chiqarilganda (k-anonimlik asosidagi anonimlashtirish)
