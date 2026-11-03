# ShifoSafe: serverdagi shifrlangan zaxiralarni shu kompyuterga ko'chirish (serverdan tashqari nusxa).
# Fayllar shifrlangan, ochish uchun yopiq kalit kerak - uni shu kompyuterda alohida saqlang.
#
# Qo'lda:  powershell -ExecutionPolicy Bypass -File pull_backups.ps1
# Har kuni: docs/BACKUP.md dagi schtasks buyrug'i.
param(
    [string]$Server = "alisher@157.173.107.205",
    [string]$Remote = "~/backups/medkarta",
    [string]$Local  = "$env:USERPROFILE\Documents\ShifoSafe-backups",
    [int]$KeepDays  = 90
)
$ErrorActionPreference = "Stop"
New-Item -ItemType Directory -Force -Path $Local | Out-Null
$logFile = Join-Path $Local "pull.log"

try {
    # Serverdagi ro'yxat; mavjudlarini qayta yuklamaymiz
    $names = ssh -o BatchMode=yes $Server "ls -1 $Remote/shifosafe-*.tar.age* 2>/dev/null | xargs -n1 basename"
    if ($LASTEXITCODE -ne 0) { throw "serverga ulanib bo'lmadi" }
    $new = 0
    foreach ($n in $names) {
        $dst = Join-Path $Local $n
        if (-not (Test-Path $dst)) {
            scp -q -o BatchMode=yes "${Server}:$Remote/$n" "$dst"
            if ($LASTEXITCODE -ne 0) { throw "yuklab bo'lmadi: $n" }
            $new++
        }
    }
    # Nazorat yig'indisini tekshirish
    foreach ($s in Get-ChildItem $Local -Filter "*.tar.age.sha256") {
        $want = (Get-Content $s.FullName).Split(" ")[0]
        $file = $s.FullName -replace "\.sha256$", ""
        if (Test-Path $file) {
            $got = (Get-FileHash $file -Algorithm SHA256).Hash.ToLower()
            if ($got -ne $want) { throw "nazorat yig'indisi mos emas: $file" }
        }
    }
    Get-ChildItem $Local -Filter "shifosafe-*" | Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-$KeepDays) } | Remove-Item
    "$(Get-Date -Format s) OK yangi: $new" | Add-Content $logFile
    Write-Host "OK: $new ta yangi fayl -> $Local"
}
catch {
    "$(Get-Date -Format s) XATO: $_" | Add-Content $logFile
    Write-Host "XATO: $_" -ForegroundColor Red
    exit 1
}
