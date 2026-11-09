#!/usr/bin/env bash
# ShifoSafe: shifrlangan zaxira nusxa (har 6 soatda).
#
# Nima saqlanadi: PostgreSQL bazasi (pg_dump), yuklangan fayllar (media, protected_media)
# va .env.docker (FIELD_ENCRYPTION_KEY, AUDIT_HMAC_KEY - ularsiz bazadagi shifrlangan
# ma'lumotni ochib bo'lmaydi). Hammasi bitta arxivga yig'iladi va `age` bilan OCHIQ kalitga
# shifrlanadi. Yopiq kalit serverda saqlanmaydi: server buzilsa ham zaxirani ochib bo'lmaydi.
#
# Nusxalar uch joyda: serverda (30 kun), administrator kompyuterida (pull_backups.ps1) va uchinchi joyda -
# Telegram yoki bulut (scripts/offsite.sh).
#
# Sozlash: docs/BACKUP.md. Cron (har 6 soatda, RPO <= 6 soat):
#   15 */6 * * * bash /opt/medkarta/dissertatsiya/scripts/backup.sh >/dev/null 2>&1
set -Eeuo pipefail

APP_DIR=${APP_DIR:-/opt/medkarta/dissertatsiya}
BACKUP_DIR=${BACKUP_DIR:-$HOME/backups/medkarta}
RECIPIENT_FILE=${RECIPIENT_FILE:-$BACKUP_DIR/recipient.txt}
KEEP_DAYS=${KEEP_DAYS:-30}
LOG="$BACKUP_DIR/backup.log"
COMPOSE=(docker compose -f "$APP_DIR/docker-compose.prod.yml")

umask 077
mkdir -p "$BACKUP_DIR"
log() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*" | tee -a "$LOG"; }
trap 'log "XATO: ${BASH_COMMAND} (qator $LINENO)"; exit 1' ERR

command -v age >/dev/null || { log "XATO: age o'rnatilmagan (sudo apt install age)"; exit 1; }
[ -s "$RECIPIENT_FILE" ] || { log "XATO: ochiq kalit yo'q: $RECIPIENT_FILE"; exit 1; }

cd "$APP_DIR"
STAMP=$(date +%Y%m%d-%H%M)
TMP=$(mktemp -d "$BACKUP_DIR/.tmp-XXXXXX")
trap 'rm -rf "$TMP"' EXIT

DB_USER=$("${COMPOSE[@]}" exec -T db printenv POSTGRES_USER | tr -d '\r')
DB_NAME=$("${COMPOSE[@]}" exec -T db printenv POSTGRES_DB | tr -d '\r')

# 1. Baza (custom format: tiklashda jadval tanlash va tekshirish mumkin)
"${COMPOSE[@]}" exec -T db pg_dump -U "$DB_USER" -d "$DB_NAME" -Fc > "$TMP/db.dump"
# Arxiv o'qiladimi - shu zahoti tekshiriladi (buzilgan dump "muvaffaqiyatli" bo'lib qolmasin)
TABLES=$("${COMPOSE[@]}" exec -T db pg_restore --list < "$TMP/db.dump" | grep -c " TABLE DATA ")
[ "$TABLES" -gt 10 ] || { log "XATO: dumpda jadvallar juda kam ($TABLES)"; exit 1; }

# 2. Yuklangan fayllar (tahlil blankalari, biriktirmalar, avatarlar)
"${COMPOSE[@]}" exec -T backend tar czf - -C /app media protected_media > "$TMP/files.tar.gz"

# 3. Kalitlar va sozlamalar, qaysi kod versiyasi bilan olingani
cp .env.docker "$TMP/env.docker"
git rev-parse HEAD > "$TMP/commit.txt" 2>/dev/null || true
date -Is > "$TMP/created.txt"

# 4. Bitta arxiv -> age bilan shifrlash -> nazorat yig'indisi
OUT="$BACKUP_DIR/shifosafe-$STAMP.tar.age"
tar cf - -C "$TMP" . | age -R "$RECIPIENT_FILE" -o "$OUT.part"
mv "$OUT.part" "$OUT"
( cd "$BACKUP_DIR" && sha256sum "$(basename "$OUT")" > "$(basename "$OUT").sha256" )

# 5. Eski nusxalarni o'chirish
find "$BACKUP_DIR" -maxdepth 1 -name 'shifosafe-*.tar.age*' -mtime +"$KEEP_DAYS" -delete

log "OK $(basename "$OUT") $(du -h "$OUT" | cut -f1) (jadvallar: $TABLES)"

# 6. Uchinchi joy: Telegram va/yoki bulut (scripts/offsite.sh). Xato mahalliy nusxani bekor qilmaydi,
# lekin skript 1 bilan tugaydi va administratorga Telegram orqali xabar ketadi.
# shellcheck source=offsite.sh
source "$(dirname "$0")/offsite.sh"
offsite_upload "$OUT" || exit 1
