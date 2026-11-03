#!/usr/bin/env bash
# ShifoSafe: zaxira nusxani TIKLAB TEKSHIRISH (asosiy bazaga tegmaydi).
#
# Zaxirani vaqtinchalik bazaga tiklaydi, asosiy jadvallardagi yozuvlarni sanaydi,
# audit jurnali xesh zanjirini tekshiradi va vaqtinchalik bazani o'chiradi.
# Oyiga bir marta bajarish tavsiya etiladi: "zaxira bor" emas, "zaxiradan tiklasa bo'ladi".
#
# Ishlatish:
#   bash scripts/restore_check.sh ~/backups/medkarta/shifosafe-YYYYMMDD-HHMM.tar.age KALIT_FAYLI
# KALIT_FAYLI - yopiq kalit (age-keygen yaratgan fayl). Tekshiruvdan keyin serverdan o'chiring.
set -Eeuo pipefail

FILE=${1:?"zaxira fayli ko'rsatilmagan"}
KEY=${2:?"yopiq kalit fayli ko'rsatilmagan"}
APP_DIR=${APP_DIR:-/opt/medkarta/dissertatsiya}
COMPOSE=(docker compose -f "$APP_DIR/docker-compose.prod.yml")
CHECK_DB="restore_check_$(date +%s)"

umask 077
TMP=$(mktemp -d)
cleanup() {
    "${COMPOSE[@]}" exec -T db sh -c "dropdb -U \"\$POSTGRES_USER\" --if-exists $CHECK_DB" >/dev/null 2>&1 || true
    rm -rf "$TMP"
}
trap cleanup EXIT
cd "$APP_DIR"

echo "1/5 Nazorat yig'indisi"
if [ -f "$FILE.sha256" ]; then
    ( cd "$(dirname "$FILE")" && sha256sum -c "$(basename "$FILE").sha256" )
else
    echo "    (.sha256 fayli yo'q - o'tkazib yuborildi)"
fi

echo "2/5 Shifrni ochish"
age -d -i "$KEY" -o "$TMP/backup.tar" "$FILE" 2>/dev/null \
    || { echo "    XATO: shifrni ochib bo'lmadi - kalit bu zaxiraga mos emas yoki fayl buzilgan"; exit 1; }
tar xf "$TMP/backup.tar" -C "$TMP" && rm -f "$TMP/backup.tar"
echo "    olingan: $(cat "$TMP/created.txt"), kod: $(cut -c1-10 "$TMP/commit.txt" 2>/dev/null || echo '?')"
tar tzf "$TMP/files.tar.gz" >/dev/null && echo "    fayllar arxivi: $(tar tzf "$TMP/files.tar.gz" | grep -vc '/$') ta fayl"

echo "3/5 Vaqtinchalik bazaga tiklash ($CHECK_DB)"
DB_USER=$("${COMPOSE[@]}" exec -T db printenv POSTGRES_USER | tr -d '\r')
"${COMPOSE[@]}" exec -T db createdb -U "$DB_USER" "$CHECK_DB"
"${COMPOSE[@]}" exec -T db pg_restore -U "$DB_USER" -d "$CHECK_DB" --no-owner --exit-on-error < "$TMP/db.dump"

echo "4/5 Yozuvlar soni"
"${COMPOSE[@]}" exec -T db psql -U "$DB_USER" -d "$CHECK_DB" -At -c "
    SELECT 'foydalanuvchilar: ' || count(*) FROM users_user
    UNION ALL SELECT 'bemorlar: ' || count(*) FROM records_patient
    UNION ALL SELECT 'tibbiy yozuvlar: ' || count(*) FROM records_medicalrecord
    UNION ALL SELECT 'tahlillar: ' || count(*) FROM labs_labresult
    UNION ALL SELECT 'audit yozuvlari: ' || count(*) FROM records_auditlog" | sed 's/^/    /'

echo "5/5 Audit zanjiri (tiklangan bazada, tiklangan kalit bilan)"
HMAC=$(grep '^AUDIT_HMAC_KEY=' "$TMP/env.docker" | cut -d= -f2- || true)
"${COMPOSE[@]}" exec -T -e DB_NAME="$CHECK_DB" -e AUDIT_HMAC_KEY="$HMAC" backend \
    python manage.py verify_audit_log 2>/dev/null | grep -v " INFO \| CRITICAL " | sed 's/^/    /'

echo "Tayyor: zaxira o'qiladi va tiklanadi. Vaqtinchalik baza o'chirildi."
