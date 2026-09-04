# MedKarta — Bemor kasallik tarixi tizimi (Backend)

Tibbiy ma'lumotlar xavfsizligini ta'minlovchi bemorlar kasallik tarixini boshqarish tizimining backend qismi. Django REST Framework asosida qurilgan, magistrlik dissertatsiyasi doirasida ishlab chiqilgan.

## Texnologiyalar

- **Python 3.13** / **Django 6.0**
- **Django REST Framework** — RESTful API
- **PostgreSQL 16** — asosiy ma'lumotlar bazasi
- **Redis** — kesh va Celery uchun vositachi (broker)
- **Celery + Celery Beat** — fon vazifalari va davriy jarayonlar
- **djangorestframework-simplejwt** — JWT autentifikatsiya
- **cryptography (Fernet)** — maydon darajasida shifrlash (AES-128)
- **ReportLab** — PDF hujjatlar generatsiyasi
- **pytest-django** — avtomatik testlar
- **Docker / Docker Compose** — konteynerlashtirish

## Asosiy imkoniyatlar

- 🔐 **JWT autentifikatsiya** — qisqa muddatli access token, rotatsiyalanuvchi refresh token
- 👥 **Rolga asoslangan kirishni boshqarish (RBAC)** — bemor / shifokor / administrator
- 🔒 **Ma'lumotlarni shifrlash** — tashxis, davolash, retsept ko'rsatmalari, shaxsiy ma'lumotlar bazada Fernet (AES-128) bilan shifrlangan holda saqlanadi
- 📋 **Audit jurnali** — barcha ko'rish/yaratish/tahrirlash/eksport harakatlari o'zgartirib bo'lmaydigan holda qayd etiladi (kim, qachon, qaysi IP'dan)
- 🛡 **Ko'p qatlamli brute-force himoyasi** — IP darajasidagi tezlik cheklovi (throttling) + hisobni vaqtincha bloklash (account lockout)
- 🔑 **Parolni tiklash** — email orqali, Celery fon vazifasi sifatida yuboriladi
- 📎 **Fayl biriktirish** — rentgen, tahlil natijalari (JPEG/PNG/PDF), himoyalangan xotirada saqlanadi
- 💊 **Retsept moduli** — dori-darmon tayinlash, avtomatik muddat tugashi (Celery Beat orqali)
- 📄 **PDF eksport** — bemor kasallik tarixini yuklab olish
- 📊 **k-anonimlik moduli** — bemorlar ma'lumotlarini tadqiqot/statistika uchun anonimlashtirish (k-anonimlik, l-diversity ko'rsatkichlari, CSV eksport)

## O'rnatish — Docker orqali (tavsiya etiladi)

Butun tizim (PostgreSQL, Redis, Django, Celery worker, Celery beat) bitta buyruq bilan ishga tushadi:

```bash
git clone <repo-url>
cd dissertatsiya
cp .env.example .env.docker   # qiymatlarni to'ldiring
docker-compose up --build
```

Birinchi ishga tushirishdan keyin, yangi terminalda superuser yarating:
```bash
docker-compose exec backend python manage.py createsuperuser
```

- Backend: `http://localhost:8000`
- API: `http://localhost:8000/api/`

## O'rnatish — mahalliy (Docker'siz)

### 1. Virtual muhit va kutubxonalar

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -r requirements.txt
```

### 2. PostgreSQL va Redis'ni ishga tushiring

PostgreSQL'da baza yarating:
```sql
CREATE DATABASE dissertatsiya;
```

Redis (Docker orqali eng sodda yo'l):
```bash
docker run -d -p 6379:6379 --name redis redis:7-alpine
```

### 3. `.env` faylini sozlash

`.env.example`dan nusxa oling va to'ldiring:
SECRET_KEY=django-insecure-_l*f-w+hfl&p^e4sv_z^3lwety1xga-i*%wst$t%s1ber^7l$l
DEBUG=True
FIELD_ENCRYPTION_KEY=YlMT5644toOKzfltvg_G9KUQgyVBTYNKeGDnehsRjKc=

DB_NAME=dissertatsiya
DB_USER=111
DB_PASSWORD=2222
DB_HOST=127.0.0.1
DB_PORT=5432


Shifrlash kalitini generatsiya qilish:
```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

### 4. Migratsiya va superuser

```bash
python manage.py migrate
python manage.py createsuperuser
```

### 5. Serverni ishga tushirish

```bash
python manage.py runserver
```

Celery worker va beat (alohida terminallarda):
```bash
celery -A config worker -l info --pool=solo
celery -A config beat -l info
```

## Testlarni ishga tushirish

```bash
pytest
pytest --cov=users --cov=records --cov-report=term-missing
```

## Loyiha tuzilishi
dissertatsiya/
├── config/ # Django sozlamalari (settings, urls, celery)
├── users/ # Foydalanuvchi modeli, autentifikatsiya, 2FA infratuzilmasi
│ ├── models.py # User modeli (role, avatar, lockout maydonlari)
│ ├── views.py # Login, register, parolni tiklash, foydalanuvchi CRUD
│ ├── tasks.py # Celery: email yuborish
│ └── permissions.py
├── records/ # Bemorlar, tibbiy yozuvlar, retseptlar, audit
│ ├── models.py # Patient, MedicalRecord, Prescription, RecordAttachment, AuditLog
│ ├── encryption.py # EncryptedTextField/CharField (Fernet)
│ ├── anonymization.py # k-anonimlik algoritmi
│ ├── pdf_export.py # ReportLab asosida PDF generatsiya
│ ├── storage.py # Himoyalangan fayl xotirasi
│ ├── tasks.py # Celery: retseptlar muddatini tekshirish (davriy)
│ ├── permissions.py
│ └── views.py
├── docker-compose.yml
├── Dockerfile
├── entrypoint.sh
├── wait-for-db.sh
└── manage.py


## API endpointlari (qisqacha)

| Metod | Endpoint | Tavsif | Ruxsat |
|---|---|---|---|
| POST | `/api/auth/register/` | Bemor sifatida ro'yxatdan o'tish | Ochiq |
| POST | `/api/auth/login/` | Tizimga kirish (JWT) | Ochiq |
| POST | `/api/auth/password-reset/` | Parolni tiklash so'rovi | Ochiq |
| POST | `/api/auth/password-reset-confirm/` | Yangi parol o'rnatish | Ochiq |
| POST | `/api/auth/users/` | Shifokor/admin yaratish | Faqat admin |
| GET | `/api/records/patients/` | Bemorlar ro'yxati | Shifokor/admin |
| GET/PATCH | `/api/records/patients/:id/` | Bemor kartasi | Rolga bog'liq |
| GET/POST | `/api/records/medical-records/` | Tibbiy yozuvlar | Rolga bog'liq |
| GET/POST | `/api/records/prescriptions/` | Retseptlar | Rolga bog'liq |
| POST/GET/DELETE | `/api/records/medical-records/:id/attachments/` | Fayl biriktirish | Rolga bog'liq |
| GET | `/api/records/patients/:id/history-pdf/` | Kasallik tarixi PDF | Rolga bog'liq |
| GET | `/api/records/audit-logs/` | Audit jurnali | Faqat admin |
| GET | `/api/records/export-anonymized/` | Anonimlashtirilgan eksport | Faqat admin |

## Xavfsizlik arxitekturasi

Loyiha ikki qatlamli himoya modeliga asoslangan:

1. **Operatsion xavfsizlik** — kundalik foydalanishda (RBAC, JWT, shifrlash, audit, brute-force himoyasi)
2. **Ikkilamchi foydalanish xavfsizligi** — ma'lumot tadqiqot/statistika uchun chiqarilganda (k-anonimlik asosidagi anonimlashtirish)

## Muallif

[F.I.Sh. — shu yerga to'ldiring], magistrant
[Muassasa nomi — shu yerga to'ldiring]