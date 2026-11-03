# Zaxira nusxa (backup)

Har kuni soat 03:15 da server `scripts/backup.sh` ni bajaradi:

- **Nima saqlanadi:** PostgreSQL bazasi, yuklangan fayllar (`media`, `protected_media`) va `.env.docker`. `.env.docker` kerak, chunki undagi kalitlarsiz bazadagi shifrlangan ma'lumot ochilmaydi.
- **Qanday shifrlanadi:** hammasi bitta arxivga yig'iladi va `age` bilan **ochiq kalitga** shifrlanadi. Yopiq kalit serverda yo'q, shuning uchun server buzilsa ham zaxiralarni o'qib bo'lmaydi.
- **Qayerda turadi:** `~/backups/medkarta/` papkasida 30 kun saqlanadi. Kompyuter ularni `scripts/pull_backups.ps1` bilan serverdan tashqariga ko'chiradi.

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
