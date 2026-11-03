"""
Avatar yuklash testlari (pentest 2026-10-19, 2/3/4-bandlar).
Ishlatish: python manage.py test users --settings=config.settings_test
"""
import os
import re
import struct
import zlib
from io import BytesIO
from unittest import mock

from django.contrib.auth.tokens import default_token_generator
from django.core import mail
from django.core.cache import cache
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, transaction
from django.test import override_settings
from PIL import Image, features
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode
from kombu.exceptions import OperationalError
from PIL.PngImagePlugin import PngInfo
from rest_framework import status
from rest_framework.test import APITestCase
from rest_framework.throttling import ScopedRateThrottle

from .avatar import _normalize_mode
from .models import LoginSession, User
from .models import Role
from .security import MAX_FAILED_ATTEMPTS, REGISTER_TAKEN_LIMIT, is_reserved_username
from .serializers import LOGIN_FAILED_MESSAGE, USERNAME_RESERVED_MESSAGE, USERNAME_TAKEN_MESSAGE
from .tasks import request_password_reset

AVATAR_URL = "/api/auth/me/avatar/"
GENERATED_NAME = re.compile(r"^avatars/[0-9a-f]{32}\.(jpg|png|webp)$")
PHP_PAYLOAD = b"<?php system($_GET['c']); ?>"


def image_bytes(fmt, mode="RGB", size=(64, 64), color=None, **save_kwargs):
    img = Image.new(mode, size, color if color is not None else 0)
    buf = BytesIO()
    img.save(buf, fmt, **save_kwargs)
    return buf.getvalue()


def palette_png(transparent=False):
    img = Image.new("RGB", (64, 64), (200, 30, 30)).convert("P", palette=Image.Palette.ADAPTIVE)
    buf = BytesIO()
    if transparent:
        img.save(buf, "PNG", transparency=0)
    else:
        img.save(buf, "PNG")
    return buf.getvalue()


def png_header_only(width, height):
    """Faqat IHDR sarlavhali PNG: o'lchami katta, o'zi kichik (decompression bomb sinovi)."""

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(b"")) + chunk(b"IEND", b"")


def upload(name, data, content_type="image/jpeg"):
    return SimpleUploadedFile(name, data, content_type=content_type)


class AvatarUploadTests(APITestCase):
    def setUp(self):
        cache.clear()  # throttle hisoblagichi testlar orasida tozalanadi
        self.user = User.objects.create_user(username="avatartest", password="x")
        self.client.force_authenticate(self.user)

    def post_avatar(self, file):
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(AVATAR_URL, {"avatar": file}, format="multipart")

    def assert_accepted(self, response, ext):
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.content)
        self.user.refresh_from_db()
        self.assertRegex(self.user.avatar.name, GENERATED_NAME)
        self.assertTrue(self.user.avatar.name.endswith("." + ext))
        with default_storage.open(self.user.avatar.name) as f:
            return f.read()

    def assert_rejected(self, response):
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST, response.content)
        self.user.refresh_from_db()
        self.assertFalse(self.user.avatar)

    # --- qonuniy rasmlar ---

    def test_jpeg_accepted_and_resized(self):
        data = self.assert_accepted(
            self.post_avatar(upload("photo.JPG", image_bytes("JPEG", size=(1024, 768), color=(10, 120, 200)))), "jpg"
        )
        with Image.open(BytesIO(data)) as img:
            self.assertEqual(img.format, "JPEG")
            self.assertLessEqual(max(img.size), 512)

    def test_png_accepted(self):
        data = self.assert_accepted(self.post_avatar(upload("photo.png", image_bytes("PNG"), "image/png")), "png")
        with Image.open(BytesIO(data)) as img:
            self.assertEqual(img.format, "PNG")

    def test_cmyk_jpeg_converted_to_rgb(self):
        data = self.assert_accepted(
            self.post_avatar(upload("cmyk.jpg", image_bytes("JPEG", mode="CMYK", color=(0, 128, 128, 0)))), "jpg"
        )
        with Image.open(BytesIO(data)) as img:
            self.assertEqual(img.mode, "RGB")

    def test_palette_png_accepted(self):
        data = self.assert_accepted(self.post_avatar(upload("p.png", palette_png(), "image/png")), "png")
        with Image.open(BytesIO(data)) as img:
            self.assertEqual(img.mode, "RGB")

    def test_transparent_palette_png_keeps_alpha(self):
        data = self.assert_accepted(self.post_avatar(upload("pt.png", palette_png(transparent=True), "image/png")), "png")
        with Image.open(BytesIO(data)) as img:
            self.assertEqual(img.mode, "RGBA")
            self.assertEqual(img.getpixel((0, 0))[3], 0)

    def test_rgba_png_keeps_alpha(self):
        data = self.assert_accepted(
            self.post_avatar(upload("a.png", image_bytes("PNG", mode="RGBA", color=(255, 0, 0, 0)), "image/png")), "png"
        )
        with Image.open(BytesIO(data)) as img:
            self.assertEqual(img.mode, "RGBA")
            self.assertEqual(img.getpixel((0, 0))[3], 0)

    def test_rgb_png_with_trns_keeps_transparency(self):
        data = self.assert_accepted(
            self.post_avatar(upload("t.png", image_bytes("PNG", color=(0, 0, 0), transparency=(0, 0, 0)), "image/png")),
            "png",
        )
        with Image.open(BytesIO(data)) as img:
            self.assertEqual(img.mode, "RGBA")
            self.assertEqual(img.getpixel((0, 0))[3], 0)

    def test_grayscale_alpha_png_keeps_alpha(self):
        data = self.assert_accepted(
            self.post_avatar(upload("la.png", image_bytes("PNG", mode="LA", color=(80, 0)), "image/png")), "png"
        )
        with Image.open(BytesIO(data)) as img:
            self.assertEqual(img.mode, "RGBA")

    def test_webp_keeps_alpha(self):
        if not features.check("webp"):
            self.skipTest("Pillow WEBP qo'llab-quvvatlamaydi")
        data = self.assert_accepted(
            self.post_avatar(upload("a.webp", image_bytes("WEBP", mode="RGBA", color=(0, 0, 255, 0)), "image/webp")),
            "webp",
        )
        with Image.open(BytesIO(data)) as img:
            self.assertEqual(img.format, "WEBP")
            self.assertEqual(img.mode, "RGBA")

    def test_transparent_image_flattened_on_white_for_jpeg(self):
        img = _normalize_mode(Image.new("RGBA", (4, 4), (0, 0, 0, 0)), "JPEG")
        self.assertEqual(img.mode, "RGB")
        self.assertEqual(img.getpixel((0, 0)), (255, 255, 255))

    # --- re-encode yashirilgan narsani olib tashlaydi ---

    def test_php_appended_to_jpeg_is_stripped(self):
        polyglot = image_bytes("JPEG", color=(1, 2, 3)) + PHP_PAYLOAD
        data = self.assert_accepted(self.post_avatar(upload("poly.jpg", polyglot)), "jpg")
        self.assertNotIn(b"<?php", data)

    def test_php_in_jpeg_comment_is_stripped(self):
        data = self.assert_accepted(
            self.post_avatar(upload("comment.jpg", image_bytes("JPEG", comment=PHP_PAYLOAD))), "jpg"
        )
        self.assertNotIn(b"<?php", data)

    def test_php_in_png_text_chunk_is_stripped(self):
        info = PngInfo()
        info.add_text("Comment", PHP_PAYLOAD.decode())
        data = self.assert_accepted(
            self.post_avatar(upload("text.png", image_bytes("PNG", pnginfo=info), "image/png")), "png"
        )
        self.assertNotIn(b"<?php", data)

    # --- pentest 2026-10-19 fayllari (ekvivalentlari) rad etiladi ---

    def test_pentest_files_rejected(self):
        cases = [
            ("shell.php", PHP_PAYLOAD, "image/png"),
            ("shell.jpg.php", PHP_PAYLOAD, "image/jpeg"),
            ("xss.svg", b'<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"><script>alert(1)</script></svg>',
             "image/svg+xml"),
            ("settings.py", b"print('pwned')\n", "image/png"),
            ("pwned.txt", b"pwned by pentest\n", "image/png"),
            ("f1.bin", os.urandom(1024 * 1024), "image/jpeg"),
        ]
        for name, data, content_type in cases:
            with self.subTest(name=name):
                self.assert_rejected(self.post_avatar(upload(name, data, content_type)))

    def test_text_with_image_extension_rejected(self):
        for name, data in [("z.jpg", b"test"), ("shell.jpg", PHP_PAYLOAD), ("x.png", b"<svg></svg>")]:
            with self.subTest(name=name):
                self.assert_rejected(self.post_avatar(upload(name, data)))

    def test_too_large_file_rejected(self):
        self.assert_rejected(self.post_avatar(upload("big.jpg", os.urandom(2_100_000))))

    def test_decompression_bomb_rejected(self):
        self.assert_rejected(self.post_avatar(upload("bomb.png", png_header_only(20000, 20000), "image/png")))

    def test_dimensions_over_own_limit_rejected_before_decoding(self):
        # Pillow chegarasidan (~178 MP) past, lekin bizning chegaradan (4096 px / 16 MP) yuqori
        for width, height in [(5000, 5000), (4000, 4500)]:
            with self.subTest(size=(width, height)):
                response = self.post_avatar(upload("big.png", png_header_only(width, height), "image/png"))
                self.assert_rejected(response)
                self.assertIn("o'lchami", response.json()["detail"])

    def test_missing_file_rejected(self):
        response = self.client.post(AVATAR_URL, {}, format="multipart")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    # --- eski avatarni o'chirish (4-band) ---

    def test_previous_generated_avatar_deleted(self):
        self.assert_accepted(self.post_avatar(upload("one.jpg", image_bytes("JPEG"))), "jpg")
        first = self.user.avatar.name
        self.assert_accepted(self.post_avatar(upload("two.png", image_bytes("PNG"), "image/png")), "png")
        self.assertNotEqual(self.user.avatar.name, first)
        self.assertFalse(default_storage.exists(first))
        self.assertTrue(default_storage.exists(self.user.avatar.name))

    def test_legacy_avatar_not_deleted(self):
        legacy = default_storage.save("avatars/settings.py", ContentFile(b"legacy evidence"))
        self.user.avatar.name = legacy
        self.user.save(update_fields=["avatar"])
        self.assert_accepted(self.post_avatar(upload("new.jpg", image_bytes("JPEG"))), "jpg")
        self.assertTrue(default_storage.exists(legacy))

    def test_rejected_upload_keeps_current_avatar(self):
        self.assert_accepted(self.post_avatar(upload("one.jpg", image_bytes("JPEG"))), "jpg")
        current = self.user.avatar.name
        response = self.post_avatar(upload("shell.php", PHP_PAYLOAD, "image/png"))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.user.refresh_from_db()
        self.assertEqual(self.user.avatar.name, current)
        self.assertTrue(default_storage.exists(current))

    def test_unauthenticated_rejected(self):
        self.client.force_authenticate(None)
        response = self.client.post(AVATAR_URL, {"avatar": upload("a.jpg", image_bytes("JPEG"))}, format="multipart")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_throttled_after_ten_uploads(self):
        for i in range(10):
            self.post_avatar(upload(f"{i}.jpg", image_bytes("JPEG")))
        response = self.post_avatar(upload("11.jpg", image_bytes("JPEG")))
        self.assertEqual(response.status_code, status.HTTP_429_TOO_MANY_REQUESTS)


# --- Username enumeration (pentest 2026-10-19, 5-band) ---

LOGIN_URL = "/api/auth/login/"
REGISTER_URL = "/api/auth/register/"
RESET_URL = "/api/auth/password-reset/"
RESET_CONFIRM_URL = "/api/auth/password-reset-confirm/"
STRONG_PASSWORD = "Qx7#vTm2!pLz9w"


@mock.patch.object(ScopedRateThrottle, "allow_request", return_value=True)  # bloklashni login throttle'idan ajratib sinash
class LoginEnumerationTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(username="realuser", password=STRONG_PASSWORD)

    def login(self, username, password, ip="10.1.1.1"):
        return self.client.post(
            LOGIN_URL, {"username": username, "password": password}, format="json", HTTP_X_FORWARDED_FOR=ip
        )

    def assert_login_failed(self, response):
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED, response.content)
        self.assertEqual(response.json()["detail"], LOGIN_FAILED_MESSAGE)

    def test_wrong_password_and_unknown_user_look_identical(self, _):
        existing = self.login("realuser", "wrong-password")
        unknown = self.login("nosuchuser", "wrong-password")
        self.assert_login_failed(existing)
        self.assert_login_failed(unknown)
        self.assertEqual(existing.json(), unknown.json())

    def test_existing_and_unknown_user_both_locked_after_limit(self, _):
        for username in ("realuser", "nosuchuser"):
            for _ in range(MAX_FAILED_ATTEMPTS):
                self.assert_login_failed(self.login(username, "wrong-password"))
        # Bloklangandan keyin to'g'ri parol ham o'tmaydi, javob esa o'zgarmaydi
        self.assert_login_failed(self.login("realuser", STRONG_PASSWORD))
        self.assertEqual(self.login("realuser", "x").json(), self.login("nosuchuser", "x").json())

    def test_lockout_is_per_ip(self, _):
        for _ in range(MAX_FAILED_ATTEMPTS):
            self.login("realuser", "wrong-password", ip="10.6.6.6")
        self.assert_login_failed(self.login("realuser", STRONG_PASSWORD, ip="10.6.6.6"))
        response = self.login("realuser", STRONG_PASSWORD, ip="10.2.2.2")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.content)

    def test_lockout_ignores_username_case(self, _):
        for _ in range(MAX_FAILED_ATTEMPTS):
            self.login("RealUser", "wrong-password")
        self.assert_login_failed(self.login("realuser", STRONG_PASSWORD))

    def test_success_resets_failure_counter(self, _):
        for _ in range(MAX_FAILED_ATTEMPTS - 1):
            self.login("realuser", "wrong-password")
        self.assertEqual(self.login("realuser", STRONG_PASSWORD).status_code, status.HTTP_200_OK)
        for _ in range(MAX_FAILED_ATTEMPTS - 1):
            self.login("realuser", "wrong-password")
        self.assertEqual(self.login("realuser", STRONG_PASSWORD).status_code, status.HTTP_200_OK)

    def test_legacy_db_lock_fields_untouched(self, _):
        for _ in range(MAX_FAILED_ATTEMPTS):
            self.login("realuser", "wrong-password")
        self.user.refresh_from_db()
        self.assertEqual(self.user.failed_login_attempts, 0)
        self.assertIsNone(self.user.locked_until)


class RegisterEnumerationTests(APITestCase):
    def setUp(self):
        cache.clear()
        User.objects.create_user(username="takenuser", password=STRONG_PASSWORD)

    def register(self, username, password=STRONG_PASSWORD, confirm=None, ip="10.3.3.3"):
        payload = {
            "username": username, "first_name": "Test", "last_name": "User",
            "email": f"{username}@example.com", "phone": "",
            "password": password, "password_confirm": confirm if confirm is not None else password,
        }
        return self.client.post(REGISTER_URL, payload, format="json", HTTP_X_FORWARDED_FOR=ip)

    def test_free_username_registers(self):
        response = self.register("freshuser")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.content)
        self.assertTrue(User.objects.filter(username="freshuser").exists())

    def test_taken_username_message_does_not_echo_username(self):
        response = self.register("takenuser")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.json()["username"], [USERNAME_TAKEN_MESSAGE])
        self.assertNotIn("takenuser", response.content.decode())

    def test_password_equal_to_username_rejected(self):
        # Parol username bilan bir xil bo'lsa UserAttributeSimilarityValidator rad etadi.
        # "similaruser": 11 belgi (min_length o'tadi), raqam emas, keng tarqalgan emas -
        # ya'ni 400 faqat o'xshashlik validatori tufayli bo'ladi.
        before = User.objects.count()
        response = self.register("similaruser", password="similaruser", confirm="similaruser")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST, response.content)
        self.assertEqual(User.objects.count(), before)
        body = response.json()
        self.assertTrue(body.get("non_field_errors") or body.get("password"), response.content)

    def test_taken_probe_with_bad_password_creates_nothing(self):
        before = User.objects.count()
        response = self.register("takenuser", password="short", confirm="other")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("username", response.json())
        self.assertEqual(User.objects.count(), before)

    @mock.patch.object(ScopedRateThrottle, "allow_request", return_value=True)  # register 10/soat throttle'idan ajratib
    def test_taken_responses_limited_per_ip(self, _):
        for _ in range(REGISTER_TAKEN_LIMIT):
            self.assertEqual(self.register("takenuser").status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(self.register("takenuser").status_code, status.HTTP_429_TOO_MANY_REQUESTS)
        # Cheklov shu IP uchun barcha register so'rovlariga tegishli, boshqa IP'ga emas
        self.assertEqual(self.register("anotherfree").status_code, status.HTTP_429_TOO_MANY_REQUESTS)
        self.assertEqual(self.register("anotherfree", ip="10.4.4.4").status_code, status.HTTP_201_CREATED)

    @mock.patch.object(ScopedRateThrottle, "allow_request", return_value=True)
    def test_other_validation_errors_not_counted(self, _):
        for _ in range(REGISTER_TAKEN_LIMIT + 1):
            response = self.register("freeuser", password=STRONG_PASSWORD, confirm="mismatch-123456")
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(self.register("freeuser").status_code, status.HTTP_201_CREATED)


class PasswordResetEnumerationTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(username="resetuser", email="reset@example.com", password=STRONG_PASSWORD)

    def test_request_always_queues_task_with_same_response(self):
        with mock.patch("users.views.request_password_reset.delay") as delay:
            existing = self.client.post(RESET_URL, {"email": "reset@example.com"}, format="json")
            unknown = self.client.post(RESET_URL, {"email": "nobody@example.com"}, format="json")
        self.assertEqual(existing.status_code, status.HTTP_200_OK)
        self.assertEqual(existing.json(), unknown.json())
        self.assertEqual(delay.call_args_list, [mock.call("reset@example.com"), mock.call("nobody@example.com")])

    def test_request_same_response_when_broker_down(self):
        with mock.patch("users.views.request_password_reset.delay", side_effect=OperationalError("broker down")):
            existing = self.client.post(RESET_URL, {"email": "reset@example.com"}, format="json")
            unknown = self.client.post(RESET_URL, {"email": "nobody@example.com"}, format="json")
        with mock.patch("users.views.request_password_reset.delay"):
            normal = self.client.post(RESET_URL, {"email": "nobody@example.com"}, format="json")
        self.assertEqual(existing.status_code, status.HTTP_200_OK)
        self.assertEqual(unknown.status_code, status.HTTP_200_OK)
        self.assertEqual(existing.json(), normal.json())
        self.assertEqual(unknown.json(), normal.json())

    def test_task_sends_mail_only_for_existing_email(self):
        request_password_reset("nobody@example.com")
        self.assertEqual(len(mail.outbox), 0)
        request_password_reset("RESET@example.com")
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["reset@example.com"])
        self.assertIn("/reset-password/", mail.outbox[0].body)

    def test_confirm_unknown_uid_and_bad_token_look_identical(self):
        unknown_uid = self.client.post(RESET_CONFIRM_URL, {
            "uid": urlsafe_base64_encode(force_bytes(999999)), "token": "x-y", "new_password": STRONG_PASSWORD,
        }, format="json")
        bad_token = self.client.post(RESET_CONFIRM_URL, {
            "uid": urlsafe_base64_encode(force_bytes(self.user.pk)), "token": "x-y", "new_password": STRONG_PASSWORD,
        }, format="json")
        garbage_uid = self.client.post(RESET_CONFIRM_URL, {
            "uid": "!!!", "token": "x-y", "new_password": STRONG_PASSWORD,
        }, format="json")
        for response in (unknown_uid, bad_token, garbage_uid):
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(unknown_uid.json(), bad_token.json())
        self.assertEqual(garbage_uid.json(), bad_token.json())

    def test_confirm_valid_link_still_works(self):
        response = self.client.post(RESET_CONFIRM_URL, {
            "uid": urlsafe_base64_encode(force_bytes(self.user.pk)),
            "token": default_token_generator.make_token(self.user),
            "new_password": "Nw8$kRt3!mZq5y",
        }, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.content)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("Nw8$kRt3!mZq5y"))


ADMIN_USERS_URL = "/api/auth/users/"
RESERVED_VARIANTS = [
    "admin", "Admin", "ADMIN", "ａｄｍｉｎ", "аdmin", "ad_min", "admin.", "admin1", "root2024",
    "root", "support", "system", "Administrator", "админ", "medkarta",
]
ALLOWED_NAMES = ["alisher", "Алишер", "adminka", "staffan", "rooted", "supporter", "sys_user", "doctorova"]


class ReservedUsernameTests(APITestCase):
    """6-band: admin, root, support, system va o'xshash nomlar ommaviy ro'yxatdan o'tishda taqiqlanadi."""

    def setUp(self):
        cache.clear()

    def register(self, username, password=STRONG_PASSWORD, ip="10.6.6.6"):
        payload = {
            "username": username, "first_name": "Test", "last_name": "User", "email": "", "phone": "",
            "password": password, "password_confirm": password,
        }
        return self.client.post(REGISTER_URL, payload, format="json", HTTP_X_FORWARDED_FOR=ip)

    def test_reserved_detection(self):
        for name in RESERVED_VARIANTS:
            with self.subTest(name=name):
                self.assertTrue(is_reserved_username(name))
        for name in ALLOWED_NAMES:
            with self.subTest(name=name):
                self.assertFalse(is_reserved_username(name))

    @mock.patch.object(ScopedRateThrottle, "allow_request", return_value=True)
    def test_reserved_names_rejected_on_register(self, _):
        for name in RESERVED_VARIANTS:
            with self.subTest(name=name):
                response = self.register(name)
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST, response.content)
                self.assertEqual(response.json()["username"], [USERNAME_RESERVED_MESSAGE])
        self.assertFalse(User.objects.exists())

    @mock.patch.object(ScopedRateThrottle, "allow_request", return_value=True)
    def test_allowed_names_register(self, _):
        for name in ALLOWED_NAMES:
            with self.subTest(name=name):
                self.assertEqual(self.register(name).status_code, status.HTTP_201_CREATED)

    @mock.patch.object(ScopedRateThrottle, "allow_request", return_value=True)
    def test_existing_reserved_account_reports_reserved_not_taken(self, _):
        # Bazada 'admin' bo'lsa ham javob "band" emas, "zaxira" - bandlik oshkor bo'lmaydi
        User.objects.create_user(username="admin", password=STRONG_PASSWORD)
        response = self.register("admin")
        self.assertEqual(response.json()["username"], [USERNAME_RESERVED_MESSAGE])
        self.assertNotIn(USERNAME_TAKEN_MESSAGE, response.content.decode())

    @mock.patch.object(ScopedRateThrottle, "allow_request", return_value=True)
    def test_reserved_attempts_not_counted_as_taken(self, _):
        for _ in range(REGISTER_TAKEN_LIMIT + 1):
            self.assertEqual(self.register("admin").status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(self.register("freshname").status_code, status.HTTP_201_CREATED)

    def test_admin_can_create_reserved_service_account(self):
        admin = User.objects.create_user(username="chief", password=STRONG_PASSWORD, role=Role.ADMIN)
        self.client.force_authenticate(admin)
        response = self.client.post(ADMIN_USERS_URL, {
            "username": "support", "first_name": "Xizmat", "last_name": "Hisobi", "email": "",
            "phone": "", "password": STRONG_PASSWORD, "role": Role.DOCTOR,
        }, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.content)
        self.assertTrue(User.objects.filter(username="support").exists())


class CaseInsensitiveUsernameTests(APITestCase):
    """'Alisher' va 'alisher' ikki xil hisob bo'lmasin: register, admin yaratish va admin PATCH."""

    def setUp(self):
        cache.clear()
        self.existing = User.objects.create_user(username="alisher", password=STRONG_PASSWORD)
        self.admin = User.objects.create_user(username="chief", password=STRONG_PASSWORD, role=Role.ADMIN)

    def register(self, username, ip="10.7.7.7"):
        return self.client.post(REGISTER_URL, {
            "username": username, "first_name": "Test", "last_name": "User", "email": "", "phone": "",
            "password": STRONG_PASSWORD, "password_confirm": STRONG_PASSWORD,
        }, format="json", HTTP_X_FORWARDED_FOR=ip)

    @mock.patch.object(ScopedRateThrottle, "allow_request", return_value=True)
    def test_register_case_variant_rejected_and_counted_as_taken(self, _):
        for name in ["Alisher", "ALISHER", "aLiShEr"]:
            with self.subTest(name=name):
                response = self.register(name)
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertEqual(response.json()["username"], [USERNAME_TAKEN_MESSAGE])
        self.assertEqual(User.objects.filter(username__iexact="alisher").count(), 1)
        # Registr varianti ham 5-banddagi "band" hisoblagichiga kiradi
        for _ in range(REGISTER_TAKEN_LIMIT - 3):
            self.register("Alisher")
        self.assertEqual(self.register("brandnew").status_code, status.HTTP_429_TOO_MANY_REQUESTS)

    def test_admin_create_case_variant_rejected(self):
        self.client.force_authenticate(self.admin)
        response = self.client.post(ADMIN_USERS_URL, {
            "username": "ALISHER", "first_name": "A", "last_name": "B", "email": "", "phone": "",
            "password": STRONG_PASSWORD, "role": Role.DOCTOR,
        }, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST, response.content)
        self.assertEqual(response.json()["username"], [USERNAME_TAKEN_MESSAGE])

    def test_admin_rename_to_other_users_case_variant_rejected(self):
        other = User.objects.create_user(username="bobur", password=STRONG_PASSWORD)
        self.client.force_authenticate(self.admin)
        response = self.client.patch(f"{ADMIN_USERS_URL}{other.pk}/", {"username": "Alisher"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST, response.content)
        other.refresh_from_db()
        self.assertEqual(other.username, "bobur")

    def test_admin_can_change_case_of_own_name(self):
        self.client.force_authenticate(self.admin)
        response = self.client.patch(f"{ADMIN_USERS_URL}{self.existing.pk}/", {"username": "Alisher"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.content)
        self.existing.refresh_from_db()
        self.assertEqual(self.existing.username, "Alisher")

    def test_database_constraint_blocks_other_paths(self):
        # Admin sayti, shell, seed_demo kabi API'dan tashqari yo'llar
        with self.assertRaises(IntegrityError), transaction.atomic():
            User.objects.create_user(username="ALISHER", password=STRONG_PASSWORD)
        self.assertEqual(User.objects.filter(username__iexact="alisher").count(), 1)


REFRESH_URL = "/api/auth/login/refresh/"


class PasswordResetConfirmHardeningTests(APITestCase):
    """7-band: reset-confirm throttle, 1 soatlik havola, parol tiklanganda sessiyalar yopiladi."""

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(username="victim", email="victim@example.com", password=STRONG_PASSWORD)

    def confirm(self, token="x-y", uid=None, password="Nw8$kRt3!mZq5y", ip="10.8.8.8"):
        return self.client.post(RESET_CONFIRM_URL, {
            "uid": uid or urlsafe_base64_encode(force_bytes(self.user.pk)), "token": token, "new_password": password,
        }, format="json", HTTP_X_FORWARDED_FOR=ip)

    def test_throttled_after_ten_requests_per_ip(self):
        for _ in range(10):
            self.assertEqual(self.confirm().status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(self.confirm().status_code, status.HTTP_429_TOO_MANY_REQUESTS)
        # Boshqa IP'ga ta'sir qilmaydi
        self.assertEqual(self.confirm(ip="10.8.8.9").status_code, status.HTTP_400_BAD_REQUEST)

    def test_valid_link_works_within_limit(self):
        for _ in range(9):
            self.confirm()
        token = default_token_generator.make_token(self.user)
        self.assertEqual(self.confirm(token=token).status_code, status.HTTP_200_OK)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("Nw8$kRt3!mZq5y"))

    def test_link_expires_after_one_hour(self):
        from datetime import datetime, timedelta
        from django.conf import settings
        from django.contrib.auth.tokens import PasswordResetTokenGenerator

        self.assertEqual(settings.PASSWORD_RESET_TIMEOUT, 3600)
        token = default_token_generator.make_token(self.user)
        later = datetime.now() + timedelta(seconds=3601)
        with mock.patch.object(PasswordResetTokenGenerator, "_now", return_value=later):
            self.assertEqual(self.confirm(token=token).status_code, status.HTTP_400_BAD_REQUEST)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(STRONG_PASSWORD))

    def test_reset_revokes_existing_refresh_tokens(self):
        from rest_framework_simplejwt.tokens import RefreshToken

        sessions = [str(RefreshToken.for_user(self.user)) for _ in range(2)]
        for refresh in sessions:
            self.assertEqual(self.client.post(REFRESH_URL, {"refresh": refresh}, format="json").status_code, 200)
        token = default_token_generator.make_token(self.user)
        self.assertEqual(self.confirm(token=token).status_code, status.HTTP_200_OK)
        for refresh in sessions:
            self.assertEqual(self.client.post(REFRESH_URL, {"refresh": refresh}, format="json").status_code, 401)
        # Yangi parol bilan kirish ishlaydi
        login = self.client.post("/api/auth/login/", {"username": "victim", "password": "Nw8$kRt3!mZq5y"}, format="json")
        self.assertEqual(login.status_code, status.HTTP_200_OK)

    def test_used_link_cannot_be_reused(self):
        token = default_token_generator.make_token(self.user)
        self.assertEqual(self.confirm(token=token).status_code, status.HTTP_200_OK)
        self.assertEqual(self.confirm(token=token, password="Zz9!another-Pass").status_code, status.HTTP_400_BAD_REQUEST)


class SecurityLogClientIpTests(APITestCase):
    """Xavfsizlik loglari nginx konteyneri (REMOTE_ADDR) emas, client_ident IP'sini yozadi."""

    def setUp(self):
        cache.clear()
        User.objects.create_user(username="logged", password=STRONG_PASSWORD)

    def test_rejected_avatar_logs_forwarded_client_ip(self):
        self.client.force_authenticate(User.objects.get(username="logged"))
        with self.assertLogs("security", level="WARNING") as logs:
            self.client.post(AVATAR_URL, {"avatar": upload("shell.php", PHP_PAYLOAD, "image/png")}, format="multipart",
                             HTTP_X_FORWARDED_FOR="203.0.113.7", REMOTE_ADDR="172.18.0.6")
        self.assertTrue(any("IP: 203.0.113.7" in line for line in logs.output), logs.output)
        self.assertFalse(any("172.18.0.6" in line for line in logs.output), logs.output)


class OptionsMetadataTests(APITestCase):
    """8-band: OPTIONS sxema va docstring bermaydi; CORS preflight ishlashda davom etadi."""

    PUBLIC_URLS = ["/api/auth/register/", "/api/auth/login/", "/api/auth/logout/", "/api/auth/password-reset/"]

    def setUp(self):
        cache.clear()

    def test_options_on_public_endpoints_returns_no_schema(self):
        for url in self.PUBLIC_URLS:
            with self.subTest(url=url):
                response = self.client.options(url)
                self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)
                body = response.content.decode()
                self.assertNotIn("actions", body)
                self.assertNotIn("description", body)
                self.assertNotIn("min_length", body)

    def test_options_on_admin_endpoint_returns_no_schema(self):
        admin = User.objects.create_user(username="chief", password=STRONG_PASSWORD, role=Role.ADMIN)
        self.client.force_authenticate(admin)
        response = self.client.options(ADMIN_USERS_URL)
        self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)
        self.assertNotIn("actions", response.content.decode())

    def test_register_post_still_works(self):
        response = self.client.post(REGISTER_URL, {
            "username": "optionsuser", "first_name": "A", "last_name": "B", "email": "", "phone": "",
            "password": STRONG_PASSWORD, "password_confirm": STRONG_PASSWORD,
        }, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.content)

    @override_settings(CORS_ALLOWED_ORIGINS=["http://localhost:3000"])  # dev rejimi (DEBUG=True) holati
    def test_cors_preflight_still_answered_by_middleware(self):
        response = self.client.options(
            "/api/auth/login/", HTTP_ORIGIN="http://localhost:3000", HTTP_ACCESS_CONTROL_REQUEST_METHOD="POST"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response["Access-Control-Allow-Origin"], "http://localhost:3000")
        self.assertNotIn("actions", response.content.decode())


class SecurityLoggingTests(APITestCase):
    """Log bandi: hodisalar yoziladi, foydalanuvchi qiymatlari qochiriladi, maxfiy ma'lumot logga tushmaydi."""

    SECRET = "Sir-Parol-987654!"

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(username="logme", email="logme@example.com", password=STRONG_PASSWORD)

    def login(self, username, password, ip="10.9.9.9"):
        return self.client.post("/api/auth/login/", {"username": username, "password": password},
                                format="json", HTTP_X_FORWARDED_FOR=ip)

    def test_security_logger_configured_for_info(self):
        import logging
        logger = logging.getLogger("security")
        self.assertEqual(logger.getEffectiveLevel(), logging.INFO)
        self.assertTrue(logger.handlers)
        self.assertFalse(logger.propagate)

    def test_successful_and_failed_login_logged(self):
        with self.assertLogs("security", level="INFO") as logs:
            self.assertEqual(self.login("logme", STRONG_PASSWORD).status_code, 200)
            self.assertEqual(self.login("logme", self.SECRET).status_code, 401)
        self.assertTrue(any(r.levelname == "INFO" and "Muvaffaqiyatli kirish: 'logme' (IP: 10.9.9.9)" in r.getMessage()
                            for r in logs.records), logs.output)
        self.assertTrue(any(r.levelname == "WARNING" and "Muvaffaqiyatsiz kirish urinishi: 'logme'" in r.getMessage()
                            for r in logs.records), logs.output)
        self.assertFalse(any(self.SECRET in line or STRONG_PASSWORD in line for line in logs.output), logs.output)

    def test_failed_login_response_unchanged(self):
        response = self.login("logme", "wrong-password-1")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["detail"], LOGIN_FAILED_MESSAGE)

    @mock.patch.object(ScopedRateThrottle, "allow_request", return_value=True)
    def test_lockout_logged(self, _):
        with self.assertLogs("security", level="WARNING") as logs:
            for _ in range(MAX_FAILED_ATTEMPTS):
                self.login("logme", "wrong-password-1")
        self.assertTrue(any("Login bloklandi: 'logme' (IP: 10.9.9.9" in line for line in logs.output), logs.output)

    def test_throttled_request_logged(self):
        with self.assertLogs("security", level="WARNING") as logs:
            for _ in range(6):
                response = self.login("nobody", "wrong-password-1")
        self.assertEqual(response.status_code, status.HTTP_429_TOO_MANY_REQUESTS)
        self.assertTrue(any("Throttled: scope=login POST /api/auth/login/ (IP: 10.9.9.9" in line for line in logs.output),
                        logs.output)

    def test_newline_in_username_cannot_forge_log_line(self):
        with self.assertLogs("security", level="WARNING") as logs:
            self.login("evil\n2026-10-19 INFO security Muvaffaqiyatli kirish: 'admin'", "x")
        for record in logs.records:
            self.assertNotIn("\n", record.getMessage())

    def test_unknown_reset_email_is_masked(self):
        with self.assertLogs("security", level="INFO") as logs:
            request_password_reset("stranger.person@example.com")
            request_password_reset("x@evil.com\nFORGED")
        output = "\n".join(logs.output)
        self.assertIn("s***@example.com", output)
        self.assertNotIn("stranger.person", output)
        for record in logs.records:
            self.assertNotIn("\n", record.getMessage())

    def test_redacting_email_backend_never_logs_body_or_token(self):
        from django.core.mail import send_mail
        from django.test import override_settings

        token_link = "https://medsecure.uz/reset-password/NDI/abc123-secret-token/"
        with override_settings(EMAIL_BACKEND="users.mail.RedactingEmailBackend"):
            with self.assertLogs("security", level="WARNING") as logs:
                sent = send_mail("Parolni tiklash", f"Havola: {token_link}", None, ["logme@example.com"])
        self.assertEqual(sent, 0)
        output = "\n".join(logs.output)
        self.assertIn("to=l***@example.com", output)
        self.assertNotIn("abc123-secret-token", output)
        self.assertNotIn("reset-password", output)

    def test_security_events_reach_log_file(self):
        from django.conf import settings
        self.login("logme", STRONG_PASSWORD)
        import logging
        for handler in logging.getLogger("security").handlers:
            handler.flush()
        with open(settings.LOGGING["handlers"]["security_file"]["filename"]) as f:
            self.assertIn("Muvaffaqiyatli kirish: 'logme'", f.read())


class CorsPolicyTests(APITestCase):
    """CORS bandi: prod'da tashqi manbalarga ruxsat yo'q; CORS faqat /api/ uchun."""

    def preflight(self, path, origin):
        return self.client.options(path, HTTP_ORIGIN=origin, HTTP_ACCESS_CONTROL_REQUEST_METHOD="POST",
                                   HTTP_ACCESS_CONTROL_REQUEST_HEADERS="authorization,content-type")

    def test_prod_settings_allow_no_foreign_origin(self):
        from django.conf import settings
        self.assertFalse(settings.DEBUG)
        self.assertEqual(settings.CORS_ALLOWED_ORIGINS, [])
        self.assertFalse(settings.CORS_ALLOW_CREDENTIALS)
        for origin in ["http://localhost:3000", "http://127.0.0.1:3000", "https://evil.example", "null"]:
            with self.subTest(origin=origin):
                response = self.preflight("/api/auth/login/", origin)
                self.assertNotIn("Access-Control-Allow-Origin", response)
                get = self.client.get("/api/auth/me/", HTTP_ORIGIN=origin)
                self.assertNotIn("Access-Control-Allow-Origin", get)

    @override_settings(CORS_ALLOWED_ORIGINS=["https://partner.example"])
    def test_env_origin_allowed_only_on_api(self):
        api = self.preflight("/api/auth/login/", "https://partner.example")
        self.assertEqual(api["Access-Control-Allow-Origin"], "https://partner.example")
        self.assertNotIn("Access-Control-Allow-Credentials", api)
        admin = self.preflight("/admin/login/", "https://partner.example")
        self.assertNotIn("Access-Control-Allow-Origin", admin)
        other = self.preflight("/api/auth/login/", "https://evil.example")
        self.assertNotIn("Access-Control-Allow-Origin", other)

    def test_same_origin_login_still_works(self):
        cache.clear()
        User.objects.create_user(username="corsuser", password=STRONG_PASSWORD)
        response = self.client.post("/api/auth/login/", {"username": "corsuser", "password": STRONG_PASSWORD},
                                    format="json", HTTP_ORIGIN="https://medsecure.uz")
        self.assertEqual(response.status_code, status.HTTP_200_OK)


class ActiveSessionTests(APITestCase):
    """Faol sessiyalar: qurilmalar ro'yxati, bittasini yoki boshqalarini yopish."""

    PHONE = "Mozilla/5.0 (Linux; Android 14; SM-A546E) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Mobile Safari/537.36"
    PC = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36 Edg/130.0"

    def setUp(self):
        from django.core.cache import cache
        cache.clear()
        self.user = User.objects.create_user(username="sess", password="Pw8$kRt3!mZq5y", role="patient")
        self.other = User.objects.create_user(username="sess2", password="Pw8$kRt3!mZq5y", role="doctor")

    def login(self, ua, user="sess", ip="10.0.0.1"):
        self.client.credentials()
        r = self.client.post("/api/auth/login/", {"username": user, "password": "Pw8$kRt3!mZq5y"},
                             format="json", HTTP_USER_AGENT=ua, REMOTE_ADDR=ip)
        self.assertEqual(r.status_code, 200)
        return r.data

    def as_(self, tokens):
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {tokens['access']}")

    def test_list_marks_current_and_device(self):
        phone = self.login(self.PHONE, ip="10.0.0.5")
        pc = self.login(self.PC)
        self.login(self.PC, user="sess2")                 # boshqa foydalanuvchi ko'rinmaydi
        self.as_(pc)
        data = self.client.get("/api/auth/sessions/").data
        self.assertEqual(len(data["results"]), 2)
        cur = data["results"][0]
        self.assertTrue(cur["current"])
        self.assertEqual(cur["device"], {"browser": "Edge", "os": "Windows", "kind": "desktop"})
        other = data["results"][1]
        self.assertEqual((other["device"]["os"], other["device"]["kind"], other["ip"]), ("Android", "mobile", "10.0.0.5"))
        self.assertNotIn("jti", other)
        self.assertIsNotNone(phone)

    def test_revoke_one_kills_refresh_and_access_immediately(self):
        phone = self.login(self.PHONE)
        pc = self.login(self.PC)
        self.as_(pc)
        phone_row = next(x for x in self.client.get("/api/auth/sessions/").data["results"] if not x["current"])
        self.assertEqual(self.client.post(f"/api/auth/sessions/{phone_row['id']}/revoke/").data["revoked"], 1)
        # telefonning access tokeni 15 daqiqa kutmasdan rad etiladi
        self.as_(phone)
        self.assertEqual(self.client.get("/api/auth/me/").status_code, 401)
        self.client.credentials()
        r = self.client.post("/api/auth/login/refresh/", {"refresh": phone["refresh"]}, format="json")
        self.assertEqual(r.status_code, 401)
        # kompyuter ishlashda davom etadi
        self.as_(pc)
        self.assertEqual(self.client.get("/api/auth/me/").status_code, 200)
        self.assertEqual(len(self.client.get("/api/auth/sessions/").data["results"]), 1)

    def test_cannot_revoke_someone_elses_session(self):
        self.login(self.PC, user="sess2")
        foreign = LoginSession.objects.get(user=self.other)
        self.as_(self.login(self.PC))
        self.assertEqual(self.client.post(f"/api/auth/sessions/{foreign.id}/revoke/").status_code, 404)
        self.assertIsNone(LoginSession.objects.get(pk=foreign.pk).revoked_at)

    def test_revoke_others_keeps_current(self):
        a, b = self.login(self.PHONE), self.login(self.PHONE)
        cur = self.login(self.PC)
        self.as_(cur)
        self.assertEqual(self.client.post("/api/auth/sessions/revoke-others/", {}, format="json").data["revoked"], 2)
        self.assertEqual(self.client.get("/api/auth/me/").status_code, 200)
        for t in (a, b):
            self.as_(t)
            self.assertEqual(self.client.get("/api/auth/me/").status_code, 401)

    def test_revoke_all_including_current(self):
        cur = self.login(self.PC)
        self.login(self.PHONE)
        self.as_(cur)
        self.client.post("/api/auth/sessions/revoke-others/", {"include_current": True}, format="json")
        self.assertEqual(self.client.get("/api/auth/me/").status_code, 401)
        self.assertEqual(LoginSession.objects.filter(user=self.user, revoked_at__isnull=True).count(), 0)

    def test_logout_and_refresh_update_session(self):
        t = self.login(self.PC)
        s = LoginSession.objects.get(user=self.user)
        self.client.credentials()
        r = self.client.post("/api/auth/login/refresh/", {"refresh": t["refresh"]}, format="json", REMOTE_ADDR="10.9.9.9")
        self.assertEqual(r.status_code, 200)
        s.refresh_from_db()
        self.assertEqual(s.ip, "10.9.9.9")
        self.client.post("/api/auth/logout/", {"refresh": t["refresh"]}, format="json")
        s.refresh_from_db()
        self.assertIsNotNone(s.revoked_at)
        self.as_({"access": r.data["access"]})               # yangilangan access ham yopiladi
        self.assertEqual(self.client.get("/api/auth/me/").status_code, 401)

    def test_old_token_without_session_is_adopted_on_refresh(self):
        from rest_framework_simplejwt.tokens import RefreshToken
        old = RefreshToken.for_user(self.user)              # joylashtirishdan oldingi token (sid yo'q)
        self.client.post("/api/auth/login/refresh/", {"refresh": str(old)}, format="json", HTTP_USER_AGENT=self.PHONE)
        self.assertTrue(LoginSession.objects.filter(jti=old["jti"]).exists())

    def test_password_reset_closes_sessions(self):
        from .security import revoke_refresh_tokens
        t = self.login(self.PC)
        revoke_refresh_tokens(self.user)
        self.as_(t)
        self.assertEqual(self.client.get("/api/auth/me/").status_code, 401)
        self.assertIsNotNone(LoginSession.objects.get(user=self.user).revoked_at)

    def test_requires_auth(self):
        self.assertEqual(self.client.get("/api/auth/sessions/").status_code, 401)
