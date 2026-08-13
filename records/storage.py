"""
Tibbiy fayl biriktirmalari (rentgen, tahlil natijalari) uchun alohida,
ommaviy /media/ orqali ochiq bo'lmagan xotira. Bu fayllar faqat
ruxsat tekshiruvidan o'tgan so'rovlar orqaligina berib turiladi
(records/views.py dagi AttachmentDownloadView ga qarang).
"""

from django.conf import settings
from django.core.files.storage import FileSystemStorage

protected_storage = FileSystemStorage(
    location=str(settings.BASE_DIR / "protected_media"),
)