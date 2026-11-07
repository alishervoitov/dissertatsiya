# MedKarta — Bemor kasallik tarixi tizimi (Backend)

[![Backend CI](https://github.com/alishervoitov/dissertatsiya/actions/workflows/ci.yml/badge.svg)](https://github.com/alishervoitov/dissertatsiya/actions/workflows/ci.yml) [![Frontend CI](https://github.com/alishervoitov/frontend/actions/workflows/ci.yml/badge.svg)](https://github.com/alishervoitov/frontend/actions/workflows/ci.yml)

Tibbiy ma'lumotlar xavfsizligini ta'minlovchi bemorlar kasallik tarixini boshqarish tizimining backend qismi. Django REST Framework asosida qurilgan, magistrlik dissertatsiyasi doirasida ishlab chiqilgan.

## Texnologiyalar

- **Python 3.13** / **Django 6.0**
- **Django REST Framework** — RESTful API
- **PostgreSQL 16** — asosiy ma'lumotlar bazasi
- **Redis** — kesh va Celery uchun vositachi (broker)
- **Celery + Celery Beat** — fon vazifalari va davriy jarayonlar
- **djangorestframework-simplejwt** — JWT autentifikatsiya (refresh token httpOnly cookie'da)
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

Testlar Postgres, Redis va media papkaga tegmaydigan `config/settings_test.py` bilan ishlaydi (SQLite xotirada):

```bash
export SECRET_KEY=test FIELD_ENCRYPTION_KEY=$(python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
python manage.py test --settings=config.settings_test
```

PostgreSQL bilan (audit jurnali triggeri ham tekshiriladi): `CI_POSTGRES=1 DB_HOST=... DB_USER=... DB_PASSWORD=...` qo'shing.

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

### Audit langari (Telegram)

Audit jurnali HMAC xesh zanjiri bilan himoyalangan, lekin kalit serverda turadi: serverni to'liq egallagan
buzg'unchi yozuvni o'zgartirib, zanjirni kalit bilan qayta hisoblashi mumkin. Buni sezish uchun har kuni
02:10 (Toshkent) da tekshiruvdan keyin zanjir oxiri — oxirgi yozuv raqami, uning xeshi va yozuvlar soni —
administratorning Telegram chatiga yuboriladi. Xabarda bemor ma'lumoti yo'q. Zanjir buzilgan bo'lsa,
langar o'rniga ogohlantirish keladi.

Sozlash (`.env.docker`): `TELEGRAM_BOT_TOKEN` (@BotFather bergan token) va `TELEGRAM_AUDIT_CHAT_ID`.
Darhol sinash: `python manage.py verify_audit_log --telegram`.

Tekshirish — langar xabaridagi buyruqni serverda ishga tushiring:

```bash
python manage.py verify_audit_anchor <oxirgi_id> <xesh> --count <soni>
```

Langargacha bo'lgan biror yozuv o'zgartirilgan, o'chirilgan yoki zanjir qayta yozilgan bo'lsa, buyruq
«LANGAR MOS EMAS» deydi. Eslatma: `rekey_audit_chain` dan keyin barcha xeshlar o'zgaradi, shuning uchun
eski langarlar mos kelmaydi — kalit almashtirilgach darhol `verify_audit_log --checkpoint --telegram`
bilan yangi langar yuboring.

### Xavfsizlik ogohlantirishlari (Telegram)

Shu bot shubhali hodisalarni ham darhol administratorga yuboradi (`monitoring/notify.py`):

- yangi ogohlantirish: qisqa vaqtda ko'p bemor kartasi, tungi murojaat, ko'p eksport, favqulodda kirish;
- login bloklanishi (5 marta noto'g'ri parol) — sayt va Django admin; hisob mavjud yoki taxmin qilinayotgani ko'rsatiladi;
- 2FA kodi 5 marta noto'g'ri — parol to'g'ri kiritilgan, ya'ni oshkor bo'lgan bo'lishi mumkin;
- zaxira kod bilan kirish.

Foydalanuvchi kiritgan matn HTML-escape qilinadi; 10 daqiqada ko'pi bilan 20 ta xabar (ko'proq bo'lsa bitta
«juda ko'p hodisa» xabari). Yuborish Celery'da — Telegram ishlamasa ham sayt ishlayveradi.
Alohida chat kerak bo'lsa: `TELEGRAM_ALERT_CHAT_ID` (bo'sh bo'lsa audit chatiga).

### Registraturada vaqtinchalik parol

Registraturada ochilgan yoki parolini unutgan bemorga registrator bemor kartasidan «Parol berish» orqali
vaqtinchalik parol beradi (`users/temp_password.py`). Parol (`XXXX-XXXX-XXXX`, ~59 bit) faqat bir marta
ko'rsatiladi va qog'ozga chop etiladi; bazada faqat xeshi. U 72 soat amal qiladi va u bilan hech qanday
API'ga kirib bo'lmaydi: birinchi kirishda bemor o'z parolini o'rnatmaguncha token berilmaydi. Faqat bemor
hisoblari uchun (registrator shifokor yoki admin hisobini shu yo'l bilan egallay olmaydi); yangi parol
berilganda bemorning barcha sessiyalari yopiladi, har bir berilish auditga yoziladi.

### Kiosk kalitlari

Har bir kiosk ekrani o'z kaliti bilan ishlaydi (`appointments/kiosks.py`, Registratura → «Kiosk kalitlari»).
Kalit bir marta ko'rsatiladi, bazada SHA-256 xeshi; bekor qilish darhol kuchga kiradi, oxirgi faollik va
IP ko'rinadi. Eski umumiy `QUEUE_KIOSK_KEY` o'tish davri uchun ishlaydi — barcha kiosklar yangi kalitlarga
o'tgach `.env.docker` dan o'chiring.

## Uzluksiz integratsiya (CI)

Har bir push va pull request'da GitHub Actions quyidagilarni avtomatik bajaradi (`.github/workflows/ci.yml`):

- **Testlar** ikki bazada: SQLite (tez) va PostgreSQL 16 (audit jurnali triggeri bilan);
- **pip-audit** — `requirements.txt` dagi kutubxonalarning ma'lum zaifliklarini PyPI va OSV bazasidan tekshiradi;
- **bandit** — koddagi xavfli naqshlarni (o'rta va yuqori daraja) qidiradi.

Kod o'zgarmasa ham har dushanba qayta ishga tushadi: kutubxonada yangi zaiflik e'lon qilinsa, CI qizilga aylanadi. Dependabot esa har hafta yangilanish uchun pull request ochadi.

## Muallif

Voitov Alisher Abduvaxob o'g'li, magistrant (Axborot xavfsizligi, 70610202)
Andijon davlat universiteti, Dasturiy injiniring kafedrasi
