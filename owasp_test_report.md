# MedKarta: OWASP Top 10 avtomatik test hisoboti

Sana: 2026-10-17 13:02 | Maqsad: http://127.0.0.1

**Jami: 41 | O'tdi: 39 | O'tmadi: 1 | Ma'lumot/o'tkazib yuborilgan: 1**

| № | OWASP | Test | Kutilgan | Haqiqiy | Natija |
|---|---|---|---|---|---|
| T01 | A01 Buzilgan kirish nazorati | IDOR: bemor boshqa bemor profilini ko'radi | 403/404 | 403 | PASS |
| T02 | A01 Buzilgan kirish nazorati | IDOR: bemor boshqa bemor PDF tarixini yuklaydi | 403/404 | 403 | PASS |
| T03 | A01 Buzilgan kirish nazorati | IDOR: bemor boshqa bemor tibbiy yozuvini oladi | 403/404 | 403 | PASS |
| T04 | A01 Buzilgan kirish nazorati | IDOR: bemor boshqa bemor yozuvini o'zgartiradi | 403/404/405 | 403 | PASS |
| T05 | A01 Buzilgan kirish nazorati | IDOR: bemor boshqa bemor yozuvini o'chiradi | 403/404/405 | 403 | PASS |
| T06 | A01 Buzilgan kirish nazorati | IDOR: bemor boshqa bemor retseptini oladi | 403/404 | 403 | PASS |
| T07 | A01 Buzilgan kirish nazorati | Ro'yxat izolyatsiyasi: bemor ro'yxatida boshqa bemor yozuvlari yo'q | kesishma yo'q | kesishma=0 | PASS |
| T08 | A01 Buzilgan kirish nazorati | Bemor barcha bemorlar ro'yxatini ko'radi | 403 yoki faqat o'zi | 403, ro'yxat=0 | PASS |
| T09 | A01 Buzilgan kirish nazorati | Rolni oshirish: bemor /auth/users/ ga kiradi | 403 | 403 | PASS |
| T10 | A01 Buzilgan kirish nazorati | Rolni oshirish: bemor /records/audit-logs/ ga kiradi | 403 | 403 | PASS |
| T11 | A01 Buzilgan kirish nazorati | Rolni oshirish: bemor /records/export-anonymized/ ga kiradi | 403 | 403 | PASS |
| T12 | A01 Buzilgan kirish nazorati | Rolni oshirish: shifokor (admin emas) /auth/users/ ga kiradi | 403 | 403 | PASS |
| T13 | A01 Buzilgan kirish nazorati | Rolni oshirish: shifokor (admin emas) /records/audit-logs/ ga kiradi | 403 | 403 | PASS |
| T14 | A01 Buzilgan kirish nazorati | Rolni oshirish: bemor admin foydalanuvchi yaratadi | 403 | 403 | PASS |
| T15 | A01 Buzilgan kirish nazorati | Shifokor boshqa shifokorning bemoriga kiradi (siyosatga bog'liq) | ma'lumot | 200 | INFO |
| T16.1 | A01 Buzilgan kirish nazorati | Tokensiz kirish: /auth/me/ | 401 | 401 | PASS |
| T16.2 | A01 Buzilgan kirish nazorati | Tokensiz kirish: /records/patients/ | 401 | 401 | PASS |
| T16.3 | A01 Buzilgan kirish nazorati | Tokensiz kirish: /records/medical-records/ | 401 | 401 | PASS |
| T16.4 | A01 Buzilgan kirish nazorati | Tokensiz kirish: /records/prescriptions/ | 401 | 401 | PASS |
| T16.5 | A01 Buzilgan kirish nazorati | Tokensiz kirish: /records/audit-logs/ | 401 | 401 | PASS |
| T16.6 | A01 Buzilgan kirish nazorati | Tokensiz kirish: /records/patients/3/history-pdf/ | 401 | 401 | PASS |
| T20 | A05 Inyeksiya | SQL inyeksiya: login maydoni (4 ta yuklama) | 401/400, 500 va DB xatosi yo'q | [401, 401, 401, 401] | PASS |
| T21 | A05 Inyeksiya | SQL inyeksiya: so'rov parametri (search=' OR 1=1--) | 200/400, 500 yo'q | 200 | PASS |
| T22 | A05 Inyeksiya | SQL inyeksiya: so'rov parametri (ordering=id;DROP TABLE x) | 200/400, 500 yo'q | 200 | PASS |
| T23 | A05 Inyeksiya | SQL inyeksiya: so'rov parametri (search=%27%20UNION%20SELECT%20) | 200/400, 500 yo'q | 200 | PASS |
| T24 | A05 Inyeksiya | SQL inyeksiya: URL ichidagi pk | 404 | 404 | PASS |
| T25 | A05 Inyeksiya | Saqlangan XSS: yuklama JSON sifatida qaytadi, brauzer ijro etmaydi | application/json + nosniff | 201, application/json, nosniff=True | PASS |
| T26 | A05 Inyeksiya | XSS: ro'yxatdan o'tishda <script> username | 400 (rad etiladi) | 400 | PASS |
| T30 | A07 Autentifikatsiya xatolari | JWT: imzosi buzilgan token | 401 | 401 | PASS |
| T31 | A07 Autentifikatsiya xatolari | JWT: alg=none hujumi | 401 | 401 | PASS |
| T32 | A07 Autentifikatsiya xatolari | JWT: payload (user_id) o'zgartirilgan, eski imzo | 401 | 401 | PASS |
| T33 | A07 Autentifikatsiya xatolari | JWT: refresh token access o'rniga ishlatiladi | 401 | 401 | PASS |
| T34 | A07 Autentifikatsiya xatolari | JWT: noto'g'ri formatdagi token | 401 | 401 | PASS |
| T35 | A07 Autentifikatsiya xatolari | Logoutdan keyin refresh token qora ro'yxatda | 401 | 401 | PASS |
| T36 | A07 Autentifikatsiya xatolari | Parol tiklash: mavjud va mavjud bo'lmagan email javobi bir xil | bir xil javob | 200/200, bir xil=True | PASS |
| T37 | A07 Autentifikatsiya xatolari | Brute-force: 7 ta noto'g'ri parol ketma-ket | 429 limit yoki blok | [401, 401, 429, 429, 429, 429, 429] | PASS |
| T38 | A07 Autentifikatsiya xatolari | Akkaunt bloklanishi: limitdan keyin to'g'ri parol ham rad etiladi | 401/403/423 (blok) | 200 | FAIL |
| T40 | A02 Noto'g'ri sozlama | Xato sahifasida debug/stack trace yo'q | 404, ma'lumot sizmaydi | 404 | PASS |
| T41 | A02 Noto'g'ri sozlama | CORS: begona origin ruxsat etilmaydi | ACAO yo'q yoki * emas | yo'q | PASS |
| T42 | A02 Noto'g'ri sozlama | API javobida xavfsizlik sarlavhalari bor | hammasi bor | hammasi bor | PASS |
| T43 | A02 Noto'g'ri sozlama | Server sarlavhasi versiyani oshkor qilmaydi | versiyasiz | nginx | PASS |
