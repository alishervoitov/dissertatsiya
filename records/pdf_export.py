"""
Bemorning kasallik tarixini PDF hujjat sifatida generatsiya qilish.
"""

from io import BytesIO

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak,
)

RECORD_TYPE_LABELS = {
    "visit": "Tashrif",
    "diagnosis": "Tashxis",
    "prescription": "Retsept",
    "lab_result": "Laboratoriya natijasi",
    "procedure": "Muolaja",
}


def build_patient_history_pdf(patient, records):
    """
    Bemor va uning tibbiy yozuvlari asosida PDF baytlar oqimini (BytesIO)
    qaytaradi - buni to'g'ridan-to'g'ri HTTP javobiga yozish mumkin.
    """
    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4,
        topMargin=20 * mm, bottomMargin=20 * mm,
        leftMargin=20 * mm, rightMargin=20 * mm,
    )

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "TitleUz", parent=styles["Title"], fontSize=16, spaceAfter=4,
    )
    meta_style = ParagraphStyle(
        "MetaUz", parent=styles["Normal"], fontSize=9, textColor=colors.HexColor("#55716A"),
        spaceAfter=16,
    )
    section_style = ParagraphStyle(
        "SectionUz", parent=styles["Heading2"], fontSize=12,
        textColor=colors.HexColor("#0E5C56"), spaceBefore=14, spaceAfter=6,
    )
    label_style = ParagraphStyle(
        "LabelUz", parent=styles["Normal"], fontSize=9, textColor=colors.HexColor("#55716A"),
    )
    body_style = ParagraphStyle(
        "BodyUz", parent=styles["Normal"], fontSize=10, leading=14,
    )

    elements = []

    full_name = f"{patient.user.first_name} {patient.user.last_name}".strip() or patient.user.username
    elements.append(Paragraph("Bemor kasallik tarixi", title_style))
    elements.append(Paragraph(
        f"MedKarta tizimi orqali generatsiya qilingan hujjat. "
        f"Ushbu hujjat maxfiy ma'lumotlarni o'z ichiga oladi.",
        meta_style,
    ))

    # Bemor profili jadvali
    profile_data = [
        ["F.I.Sh.", full_name],
        ["Tug'ilgan sana", str(patient.date_of_birth) if patient.date_of_birth else "—"],
        ["Jinsi", patient.get_gender_display() or "—"],
        ["Qon guruhi", patient.blood_type or "—"],
        ["Allergiyalar", patient.allergies or "—"],
    ]
    profile_table = Table(profile_data, colWidths=[45 * mm, 110 * mm])
    profile_table.setStyle(TableStyle([
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("TEXTCOLOR", (0, 0), (0, -1), colors.HexColor("#55716A")),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("LINEBELOW", (0, 0), (-1, -1), 0.5, colors.HexColor("#DCE6E2")),
    ]))
    elements.append(profile_table)
    elements.append(Spacer(1, 10 * mm))

    elements.append(Paragraph(f"Tibbiy yozuvlar ({len(records)} ta)", section_style))

    if not records:
        elements.append(Paragraph("Hozircha tibbiy yozuvlar mavjud emas.", body_style))
    else:
        for r in records:
            record_title = f"{r.visit_date.strftime('%Y-%m-%d')} — {r.title}"
            elements.append(Paragraph(record_title, section_style))
            elements.append(Paragraph(
                f"Turi: {RECORD_TYPE_LABELS.get(r.record_type, r.record_type)} · "
                f"Shifokor: {r.created_by.get_full_name() or r.created_by.username}",
                label_style,
            ))
            if r.diagnosis:
                elements.append(Paragraph(f"<b>Tashxis:</b> {r.diagnosis}", body_style))
            if r.treatment:
                elements.append(Paragraph(f"<b>Davolash:</b> {r.treatment}", body_style))
            if r.notes:
                elements.append(Paragraph(f"<b>Izoh:</b> {r.notes}", body_style))
            elements.append(Spacer(1, 4 * mm))

    doc.build(elements)
    buffer.seek(0)
    return buffer