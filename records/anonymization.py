"""
k-anonimlik asosida bemorlar ma'lumotlarini ikkilamchi foydalanish
(tadqiqot, statistika) uchun anonimlashtirish moduli.

Ishlash tamoyili:
1. To'g'ridan-to'g'ri identifikatorlar (F.I.Sh., username, milliy ID,
   telefon, email, manzil) butunlay olib tashlanadi.
2. Bilvosita identifikatorlar (yosh, jins, qon guruhi) umumlashtiriladi
   (masalan aniq yosh o'rniga 10 yillik oraliq).
3. Umumlashtirilgan atributlar bo'yicha guruhlash amalga oshiriladi.
4. Agar biror guruhda k tadan kam yozuv bo'lsa, o'sha guruh butunlay
   chiqarilgan (suppressed) datasetdan olib tashlanadi - bu k-anonimlik
   shartini kafolatlaydi: har bir chiqarilgan qatorni kamida (k-1) ta
   boshqa qatordan ajratib bo'lmaydi.
"""

from collections import defaultdict
from datetime import date


def get_age_bucket(date_of_birth, bucket_size=10):
    """Aniq tug'ilgan sana o'rniga N yillik yosh oralig'ini qaytaradi."""
    if not date_of_birth:
        return "Noma'lum"
    today = date.today()
    age = today.year - date_of_birth.year - (
        (today.month, today.day) < (date_of_birth.month, date_of_birth.day)
    )
    lower = (age // bucket_size) * bucket_size
    upper = lower + bucket_size - 1
    return f"{lower}-{upper}"


def build_rows(patients_qs):
    """
    Har bir bemor uchun to'g'ridan-to'g'ri identifikatorsiz,
    umumlashtirilgan qatorni tayyorlaydi.
    """
    rows = []
    for p in patients_qs.select_related("user").prefetch_related("records"):
        record_types = sorted(set(p.records.values_list("record_type", flat=True)))
        rows.append({
            "age_group": get_age_bucket(p.date_of_birth),
            "gender": p.get_gender_display() or "Noma'lum",
            "blood_type": p.blood_type or "Noma'lum",
            "records_count": p.records.count(),
            "record_types": record_types,  # nozik atribut (sensitive attribute)
        })
    return rows


def anonymize(rows, k=5, quasi_identifiers=("age_group", "gender", "blood_type")):
    """
    k-anonimlik: berilgan bilvosita identifikatorlar (quasi_identifiers)
    bo'yicha guruhlaydi. k tadan kam a'zoli guruhlar butunlay chiqarib
    tashlanadi (suppression).

    Qaytaradi: (chiqarilgan_qatorlar, chiqarib_tashlangan_soni, jami_guruhlar_soni)
    """
    groups = defaultdict(list)
    for row in rows:
        key = tuple(row[q] for q in quasi_identifiers)
        groups[key].append(row)

    released = []
    suppressed = 0
    group_summaries = []

    for key, members in groups.items():
        distinct_sensitive = len({tuple(m["record_types"]) for m in members})
        summary = {
            "guruh": dict(zip(quasi_identifiers, key)),
            "hajmi": len(members),
            "l_diversity": distinct_sensitive,  # nechta xil tibbiy toifa mavjud
            "chiqarildi": len(members) >= k,
        }
        group_summaries.append(summary)

        if len(members) >= k:
            released.extend(members)
        else:
            suppressed += len(members)

    return released, suppressed, group_summaries


def anonymization_report(patients_qs, k=5, quasi_identifiers=("age_group", "gender", "blood_type")):
    """To'liq hisobot: chiqarilgan dataset + statistik ko'rsatkichlar."""
    rows = build_rows(patients_qs)
    released, suppressed, group_summaries = anonymize(rows, k=k, quasi_identifiers=quasi_identifiers)

    total = len(rows)
    info_loss = round((suppressed / total) * 100, 1) if total else 0.0

    return {
        "k": k,
        "quasi_identifiers": list(quasi_identifiers),
        "jami_bemorlar": total,
        "chiqarilgan_yozuvlar": len(released),
        "chiqarib_tashlangan_yozuvlar": suppressed,
        "axborot_yoqotilishi_foizi": info_loss,  # information loss %
        "guruhlar": group_summaries,
        "dataset": released,
    }