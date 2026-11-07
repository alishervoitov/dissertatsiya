# Zaxira nusxa (backup)

Har kuni soat 03:15 da server `scripts/backup.sh` ni bajaradi:

- **Nima saqlanadi:** PostgreSQL bazasi, yuklangan fayllar (`media`, `protected_media`) va `.env.docker`. `.env.docker` kerak, chunki undagi kalitlarsiz bazadagi shifrlangan ma'lumot ochilmaydi.
- **Qanday shifrlanadi:** hammasi bitta arxivga yig'iladi va `age` bilan **ochiq kalitga** shifrlanadi. Yopiq kalit serverda yo'q, shuning uchun server buzilsa ham zaxiralarni o'qib bo'lmaydi.
- **Qayerda turadi (3 joy):**
  1. serverda — `~/backups/medkarta/`, 30 kun;
  2. administrator kompyuterida — `scripts/pull_backups.ps1` ko'chiradi;
  3. uchinchi joyda — Telegram va/yoki bulut (`scripts/offsite.sh`, pastda). Server ham, kompyuter ham bir vaqtda yo'qolsa (yong'in, o'g'irlik, disk buzilishi) tiklash shu nusxadan bo'ladi.

## Bir martalik sozlash (server)

```
sudo apt install age
mkdir -p ~/backups/medkarta && chmod 700 ~/backups
age-keygen -o ~/shifosafe-backup-key.txt
age-keygen -y ~/shifosafe-backup-key.txt > ~/backups/medkarta/recipient.txt
```
`~/shifosafe-backup-key.txt` bu yopiq kalit. Uni kompyuterga ko'chiring (`scp`), keyin serverda `shred -u` bilan o'chiring.

Sinov va cron:
```
bash /opt/medkarta/dissertatsiya/scripts/backup.sh
(crontab -l 2>/dev/null; echo "15 3 * * * bash /opt/medkarta/dissertatsiya/scripts/backup.sh >/dev/null 2>&1") | crontab -
```
Jurnal: `~/backups/medkarta/backup.log`.

## Uchinchi joy (offsite)

Arxiv yuborilishidan oldin `age` bilan shifrlangan, shuning uchun uchinchi joy egasi (Telegram, bulut provayderi)
uni o'qiy olmaydi. Yuborish xatosi serverdagi nusxani bekor qilmaydi, lekin `backup.log` ga yoziladi va
Telegram'ga «⚠️ … yuborib bo'lmadi» xabari keladi.

**Telegram (qo'shimcha sozlamasiz).** `.env.docker` da `TELEGRAM_BOT_TOKEN` va `TELEGRAM_AUDIT_CHAT_ID` bo'lsa,
har kungi zaxira shu chatga fayl bo'lib (ovozsiz) keladi; izohda hajmi va SHA-256. Alohida chat kerak bo'lsa —
`TELEGRAM_BACKUP_CHAT_ID`. 45 MB dan katta arxiv bo'laklarga bo'linadi; tiklashda:
```
cat shifosafe-....tar.age.part* > shifosafe-....tar.age
```
Shaxsiy chat tavsiya etiladi: bot u yerdagi 48 soatdan eski xabarlarni o'chira olmaydi, ya'ni server egallansa
ham eski zaxiralar saqlanib qoladi.

**Bulut (ixtiyoriy, rclone).** Google Drive, Backblaze B2, S3, Yandex Disk va boshqalar:
```
sudo apt install rclone
rclone config            # masalan "gdrive" nomli remote
```
`.env.docker` ga `BACKUP_RCLONE_REMOTE=gdrive:ShifoSafe-backups`. Bulutda ham 30 kundan eski nusxalar o'chiriladi.

Sinash: `bash /opt/medkarta/dissertatsiya/scripts/backup.sh` — jurnalda `OFFSITE OK (telegram)` chiqishi kerak.

**Uchinchi joydan tiklash:** faylni Telegram'dan (yoki bulutdan) yuklab oling va pastdagi `restore_check.sh` bilan tekshiring.

## Tiklashni tekshirish (oyiga bir marta)

```
bash scripts/restore_check.sh ~/backups/medkarta/shifosafe-YYYYMMDD-HHMM.tar.age ~/kalit.txt
shred -u ~/kalit.txt
```
Skript asosiy bazaga tegmaydi:
1. nazorat yig'indisini tekshiradi va shifrni ochadi;
2. zaxirani vaqtinchalik bazaga tiklaydi;
3. yozuvlarni sanaydi va audit xesh zanjirini tekshiradi;
4. vaqtinchalik bazani o'chiradi.

## To'liq tiklash (favqulodda)

```
age -d -i kalit.txt shifosafe-....tar.age | tar xf - -C /tmp/restore
docker compose -f docker-compose.prod.yml exec -T db pg_restore -U medkarta -d dissertatsiya --clean --if-exists < /tmp/restore/db.dump
```
Fayllar `files.tar.gz` ichida, kalitlar esa `env.docker` ichida.
