# Telegram bot

To'ldirish/yechish so'rovlari, P2P bozor va guruh himoyasi (bitta fayl: `bot.py`).

## Ishga tushirish
1. `pip install -r requirements.txt`
2. `.env.example` ni `.env` deb nusxalang va to'ldiring (BOT_TOKEN, ADMIN_ID).
3. Ishga tushiring: `python bot.py` (`.env` fayl avtomatik o'qiladi).
4. Admin botga avval `/start` yuborishi shart.

## Muhim
- `.env`, `mob.db`, `persist.pkl` fayllarini GitHub'ga yuklamang (`.gitignore` da bor).
- Hostingda `mob.db` va `persist.pkl` doimiy diskda (volume) turishi kerak.
- Guruh himoyasi uchun botni guruhga admin qilib, "xabarlarni o'chirish" huquqini bering. `/guard on|off`.
