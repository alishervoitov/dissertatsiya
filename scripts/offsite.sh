#!/usr/bin/env bash
# ShifoSafe: shifrlangan zaxirani serverdan tashqari UCHINCHI joyga yuborish (STRIDE D1, D4).
# backup.sh tomonidan `source` qilinadi. Arxiv allaqachon age bilan shifrlangan - yopiq kalitsiz
# uni na Telegram, na bulut provayderi o'qiy oladi.
#
# Yo'llar (sozlanganlari hammasi ishlaydi):
#   1) Telegram (@ShifoSafeBot): .env.docker dagi TELEGRAM_BOT_TOKEN va TELEGRAM_BACKUP_CHAT_ID
#      (bo'sh bo'lsa TELEGRAM_AUDIT_CHAT_ID). 45 MB dan katta arxiv bo'laklarga bo'linadi.
#   2) Istalgan bulut (Google Drive, Backblaze B2, S3, Yandex Disk...) - rclone orqali:
#      .env.docker dagi BACKUP_RCLONE_REMOTE, masalan "gdrive:ShifoSafe-backups".
# Token hech qachon logga yozilmaydi.

TG_PART_SIZE=${TG_PART_SIZE:-45M}
TG_API=${TG_API:-https://api.telegram.org}

env_get() {   # env_get KEY [fayl] -> .env.docker dagi qiymat (qo'shtirnoqsiz)
    local f=${2:-$APP_DIR/.env.docker}
    [ -f "$f" ] || return 0
    grep -E "^$1=" "$f" | tail -n1 | cut -d= -f2- | tr -d '\r' | sed -e 's/^"//' -e 's/"$//' -e "s/^'//" -e "s/'$//"
}

_tg_send_doc() {   # _tg_send_doc token chat fayl izoh
    curl -sS --fail -m 600 -o /dev/null \
        -F "chat_id=$2" -F "document=@$3" -F "caption=$4" -F "disable_notification=true" \
        "$TG_API/bot$1/sendDocument" 2>&1 | sed -E 's#/bot[^/ ]+#/bot***#g'
    return "${PIPESTATUS[0]}"
}

tg_message() {   # ixtiyoriy matnli xabar (masalan, xato haqida)
    local token chat
    token=$(env_get TELEGRAM_BOT_TOKEN); chat=$(env_get TELEGRAM_BACKUP_CHAT_ID)
    [ -n "$chat" ] || chat=$(env_get TELEGRAM_AUDIT_CHAT_ID)
    [ -n "$token" ] && [ -n "$chat" ] || return 0
    curl -sS --fail -m 30 -o /dev/null --data-urlencode "chat_id=$chat" --data-urlencode "text=$1" \
        "$TG_API/bot$token/sendMessage" 2>/dev/null || true
}

offsite_telegram() {   # 0 - yuborildi, 2 - sozlanmagan, boshqa - xato
    local out=$1 token chat sum size name parts n i
    token=$(env_get TELEGRAM_BOT_TOKEN); chat=$(env_get TELEGRAM_BACKUP_CHAT_ID)
    [ -n "$chat" ] || chat=$(env_get TELEGRAM_AUDIT_CHAT_ID)
    [ -n "$token" ] && [ -n "$chat" ] || return 2
    command -v curl >/dev/null || { log "XATO: curl o'rnatilmagan"; return 1; }
    name=$(basename "$out")
    sum=$(cut -d' ' -f1 "$out.sha256")
    size=$(du -h "$out" | cut -f1)
    if [ "$(stat -c %s "$out")" -le 47185920 ]; then
        _tg_send_doc "$token" "$chat" "$out" "ShifoSafe zaxira · $name · $size
SHA-256: $sum" || return 1
    else
        parts=$(mktemp -d "${TMPDIR:-/tmp}/shifosafe-parts-XXXXXX")
        split -b "$TG_PART_SIZE" -d -a 2 "$out" "$parts/$name.part"
        n=$(find "$parts" -type f | wc -l); i=0
        for p in "$parts"/*; do
            i=$((i + 1))
            _tg_send_doc "$token" "$chat" "$p" "ShifoSafe zaxira · $name · bo'lak $i/$n
Birlashtirish: cat $name.part* > $name
SHA-256 (butun fayl): $sum" || { rm -rf "$parts"; return 1; }
        done
        rm -rf "$parts"
    fi
    return 0
}

offsite_rclone() {   # 0 - yuborildi, 2 - sozlanmagan, boshqa - xato
    local out=$1 remote
    remote=$(env_get BACKUP_RCLONE_REMOTE)
    [ -n "$remote" ] || return 2
    command -v rclone >/dev/null || { log "XATO: rclone o'rnatilmagan (BACKUP_RCLONE_REMOTE berilgan)"; return 1; }
    rclone copy "$out" "$remote" --quiet || return 1
    rclone copy "$out.sha256" "$remote" --quiet || return 1
    # Bulutda ham KEEP_DAYS dan eski nusxalar o'chiriladi
    rclone delete "$remote" --min-age "${KEEP_DAYS:-30}d" --include "shifosafe-*.tar.age*" --quiet || true
    return 0
}

offsite_upload() {   # mahalliy zaxira tayyor bo'lgach chaqiriladi; xato mahalliy nusxaga ta'sir qilmaydi
    local out=$1 rc any=0 failed=0
    for target in telegram rclone; do
        # if ichida: set -e va backup.sh dagi ERR trap ishlamaydi - xato shu yerda ushlanadi
        if "offsite_$target" "$out"; then rc=0; else rc=$?; fi
        case $rc in
            0) log "OFFSITE OK ($target) $(basename "$out")"; any=1 ;;
            2) ;;
            *) log "OFFSITE XATO ($target) $(basename "$out")"; failed=1 ;;
        esac
    done
    if [ "$failed" = 1 ]; then
        tg_message "⚠️ ShifoSafe: zaxirani uchinchi joyga yuborib bo'lmadi ($(basename "$out")). Serverdagi backup.log ni tekshiring."
        return 1
    fi
    [ "$any" = 1 ] || log "OFFSITE sozlanmagan: zaxira faqat serverda (va kompyuterga ko'chirilganda)"
    return 0
}
