"""Telegram bot: to'ldirish/yechish so'rovlari, P2P bozor va guruh himoyasi (birlashtirilgan versiya)."""
import html
import logging
import os
import re
import sqlite3
import time

from telegram import (Update, InlineKeyboardButton, InlineKeyboardMarkup,
                      ReplyKeyboardMarkup)
from telegram.error import TelegramError
from telegram.ext import (Application, CommandHandler, MessageHandler,
                          CallbackQueryHandler, ContextTypes, PicklePersistence, filters)

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
log = logging.getLogger("bot")

# ───────────────────────── SOZLAMALAR ─────────────────────────


def _int_env(name, default=0):
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return default


BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = _int_env("ADMIN_ID")
CARD = os.getenv("CARD", "8600 0000 0000 0000")  # karta qo'shilmagan bo'lsa shu ko'rinadi
DB_PATH = os.getenv("DB_PATH", "mob.db")
PERSIST_PATH = os.getenv("PERSIST_PATH", "persist.pkl")
MIN_DEP, MIN_WD = 10_000, 20_000
MAX_AMOUNT = _int_env("MAX_AMOUNT", 100_000_000)
MAX_P2P_ADS = 3          # bir foydalanuvchining faol/kutayotgan e'lonlari soni
P2P_TTL_HOURS = 48       # e'lon necha soatdan keyin avtomatik yopiladi
# Guard faqat shu guruhlarda ishlaydi (bo'sh bo'lsa — hamma guruhda). Masalan: ALLOWED_CHATS=-1001234567890
ALLOWED_CHATS = {int(x) for x in re.findall(r"-?\d+", os.getenv("ALLOWED_CHATS", ""))}

B_DEP, B_WD = "💳 Hisob to'ldirish", "💸 Pul yechish"
B_HIST, B_CONTACT = "📋 So'rovlarim", "📞 Admin bilan bog'lanish"
B_ADDCARD, B_PANEL = "🃏 Karta qo'shish", "⚙️ Admin panel"
B_P2P, B_CANCEL = "🔄 P2P", "❌ Bekor qilish"

SIDE = {"buy": "🟢 Sotib olaman", "sell": "🔴 Sotaman"}
ICON = {"buy": "🟢", "sell": "🔴"}
P2P_WARN = ("⚠️ Bot pulni ushlab turmaydi. Kelishuv va to'lov tomonlarning o'z javobgarligida. "
            "Avval kichik summa bilan sinab ko'ring.")

# ───────────────────────── BAZA ─────────────────────────

db = sqlite3.connect(DB_PATH, check_same_thread=False)


def init_db():
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("""CREATE TABLE IF NOT EXISTS orders(
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, kind TEXT,
        x_id TEXT, amount INTEGER, extra TEXT, status TEXT DEFAULT 'pending')""")
    db.execute("""CREATE TABLE IF NOT EXISTS cards(
        id INTEGER PRIMARY KEY AUTOINCREMENT, number TEXT, owner TEXT)""")
    db.execute("CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, name TEXT)")
    db.execute("""CREATE TABLE IF NOT EXISTS p2p(
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, uname TEXT, side TEXT,
        amount INTEGER, note TEXT, status TEXT DEFAULT 'pending')""")
    try:  # eski bazalar uchun migratsiya
        db.execute("ALTER TABLE p2p ADD COLUMN created_at INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    db.execute("""CREATE TABLE IF NOT EXISTS p2p_contacts(
        ad_id INTEGER, user_id INTEGER, PRIMARY KEY(ad_id, user_id))""")
    db.execute("CREATE TABLE IF NOT EXISTS guard(chat_id INTEGER PRIMARY KEY, on_off INTEGER DEFAULT 1)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_orders_user ON orders(user_id, status)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_p2p_status ON p2p(status, user_id)")
    db.commit()


# ───────────────────────── YORDAMCHILAR ─────────────────────────


def fmt(n):
    return f"{n:,}".replace(",", " ")


def parse_int(text):
    """'10 000', '10,000', '10.000' -> 10000. Faqat 0-9 raqamlar qabul qilinadi."""
    s = re.sub(r"[\s,._']", "", text or "")
    return int(s) if re.fullmatch(r"[0-9]{1,15}", s) else None


async def safe_send(bot, chat_id, text, **kw):
    """Xabar yuboradi; foydalanuvchi botni bloklagan bo'lsa ham bot yiqilmaydi."""
    try:
        await bot.send_message(chat_id, text, **kw)
        return True
    except TelegramError as e:
        log.warning("Xabar yuborilmadi (%s): %s", chat_id, e)
        return False


async def mark_message(q, note):
    """Admin xabariga holat qo'shadi va tugmalarni olib tashlaydi."""
    try:
        if q.message.text is not None:
            await q.edit_message_text(q.message.text + note)
        else:
            await q.edit_message_caption((q.message.caption or "") + note)
    except TelegramError as e:
        log.warning("Xabarni tahrirlab bo'lmadi: %s", e)


def menu(uid):
    if uid == ADMIN_ID:
        rows = [[B_DEP, B_WD], [B_P2P, B_PANEL], [B_ADDCARD]]
    else:
        rows = [[B_DEP, B_WD], [B_P2P, B_CONTACT], [B_HIST]]
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def cancel_kb():
    return ReplyKeyboardMarkup([[B_CANCEL]], resize_keyboard=True)


def cards_text():
    rows = db.execute("SELECT number, owner FROM cards").fetchall()
    if not rows:
        return CARD
    return "\n".join(f"{n} ({o})" if o else n for n, o in rows)


def mention(uid, uname):
    return f"@{uname}" if uname else f'<a href="tg://user?id={uid}">foydalanuvchi</a>'


# ───────────────────────── ASOSIY BUYRUQLAR ─────────────────────────


async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    ctx.user_data.clear()
    db.execute("INSERT OR IGNORE INTO users(id,name) VALUES(?,?)", (u.id, u.full_name))
    db.commit()
    await update.message.reply_html(
        f"✨ <b>Xush kelibsiz, {html.escape(u.first_name)}!</b>\n"
        "━━━━━━━━━━━━━━\n"
        "💳 Hisobingizni tez to'ldiring\n"
        "💸 Pulingizni oson yeching\n"
        "━━━━━━━━━━━━━━\n"
        "Kerakli bo'limni tanlang 👇",
        reply_markup=menu(u.id))


async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "ℹ️ Yordam\n\n"
        "/start — bosh menyu\n"
        "/cancel — joriy amalni bekor qilish\n"
        "/help — shu yordam\n\n"
        f"Minimal to'ldirish: {fmt(MIN_DEP)} so'm\nMinimal yechish: {fmt(MIN_WD)} so'm",
        reply_markup=menu(update.effective_user.id))


async def cancel_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    await update.message.reply_text("❎ Bekor qilindi.", reply_markup=menu(update.effective_user.id))


async def send_cards(target):
    rows = db.execute("SELECT id, number, owner FROM cards").fetchall()
    if not rows:
        return await target.reply_text("🃏 Kartalar yo'q. «Karta qo'shish» tugmasini bosing.")
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(f"🗑 {n[-4:]} {o}".strip(), callback_data=f"dc_{i}")]
                               for i, n, o in rows])
    await target.reply_text("🃏 Kartalar (o'chirish uchun bosing):\n"
                            + "\n".join(f"• {n} {o}" for i, n, o in rows), reply_markup=kb)


async def panel(update: Update):
    users = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    pend = db.execute("SELECT COUNT(*) FROM orders WHERE status='pending'").fetchone()[0]
    done = db.execute("SELECT COUNT(*) FROM orders WHERE status='done'").fetchone()[0]
    p2p_pend = db.execute("SELECT COUNT(*) FROM p2p WHERE status='pending'").fetchone()[0]
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🃏 Kartalar", callback_data="pn_cards")]])
    await update.message.reply_html(
        "⚙️ <b>Admin panel</b>\n━━━━━━━━━━━━━━\n"
        f"👤 Foydalanuvchilar: {users}\n⏳ Kutayotgan: {pend}\n✅ Bajarilgan: {done}\n"
        f"🔄 Tekshiruvdagi P2P: {p2p_pend}",
        reply_markup=kb)


async def history(update: Update):
    rows = db.execute("SELECT id,kind,amount,status FROM orders WHERE user_id=? "
                      "ORDER BY id DESC LIMIT 5", (update.effective_user.id,)).fetchall()
    if not rows:
        return await update.message.reply_text("📋 Sizda hali so'rovlar yo'q.")
    icons = {"pending": "⏳", "done": "✅", "rejected": "❌"}
    names = {"dep": "To'ldirish", "wd": "Yechish"}
    await update.message.reply_text("📋 Oxirgi so'rovlaringiz:\n" + "\n".join(
        f"{icons.get(s, '•')} #{i} {names.get(k, k)} — {fmt(a or 0)} so'm" for i, k, a, s in rows))


# ───────────────────────── ASOSIY MATN ISHLOVCHISI ─────────────────────────


async def handle(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    m = update.message
    d = ctx.user_data
    uid = update.effective_user.id
    t = (m.text or "").strip()
    step = d.get("step")

    if t == B_CANCEL:
        d.clear()
        return await m.reply_text("❎ Bekor qilindi.", reply_markup=menu(uid))
    if t in (B_DEP, B_WD):
        d.clear(); d.update(kind="dep" if t == B_DEP else "wd", step="id")
        return await m.reply_html("🆔 <b>1x ID</b> raqamingizni yuboring:", reply_markup=cancel_kb())
    if t == B_P2P:
        d.clear()
        return await p2p_menu(m)
    if t == B_HIST:
        return await history(update)
    if t == B_CONTACT:
        d.clear(); d["step"] = "contact"
        return await m.reply_text("✍️ Xabaringizni yozing, adminga yuboraman:", reply_markup=cancel_kb())
    if uid == ADMIN_ID and t == B_ADDCARD:
        d.clear(); d["step"] = "addcard"
        return await m.reply_html("🃏 Karta raqami va egasini yozing:\n"
                                  "<code>8600123412341234 Ism Familiya</code>", reply_markup=cancel_kb())
    if uid == ADMIN_ID and t == B_PANEL:
        return await panel(update)

    if step == "contact" and t:
        d.clear()
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("✍️ Javob berish", callback_data=f"rp_{uid}")]])
        ok = await safe_send(ctx.bot, ADMIN_ID,
                             f"📩 Murojaat\nUser: {uid} ({update.effective_user.full_name})\n\n{t[:3000]}",
                             reply_markup=kb)
        return await m.reply_text("✅ Xabaringiz adminga yuborildi." if ok
                                  else "⚠️ Hozir yuborib bo'lmadi, keyinroq urinib ko'ring.",
                                  reply_markup=menu(uid))

    if step == "reply" and uid == ADMIN_ID and t:
        target = d.get("target"); d.clear()
        ok = await safe_send(ctx.bot, target, f"👨‍💼 Admin javobi:\n\n{t}")
        return await m.reply_text("✅ Javob yuborildi." if ok
                                  else "⚠️ Yuborib bo'lmadi (foydalanuvchi botni bloklagan bo'lishi mumkin).",
                                  reply_markup=menu(uid))

    if step == "addcard" and uid == ADMIN_ID and t:
        mt = re.match(r"^\s*((?:[0-9]\s?){16})\s*(.*)$", t)
        if not mt:
            return await m.reply_text("❌ Format noto'g'ri. Misol: 8600123412341234 Ism Familiya")
        digits = re.sub(r"\s", "", mt.group(1))
        number = " ".join(digits[i:i + 4] for i in range(0, 16, 4))
        db.execute("INSERT INTO cards(number, owner) VALUES(?,?)", (number, mt.group(2).strip()[:60]))
        db.commit(); d.clear()
        await m.reply_text("✅ Karta qo'shildi.", reply_markup=menu(uid))
        return await send_cards(m)

    if await p2p_step(update, ctx):
        return

    if step == "id":
        if not re.fullmatch(r"[0-9]{5,15}", t):
            return await m.reply_text("❌ 1x ID faqat raqamlardan iborat bo'lishi kerak (5–15 ta raqam).")
        d["x_id"] = t; d["step"] = "amount"
        return await m.reply_text("💵 Summani kiriting (so'm):")

    if step == "amount":
        amt = parse_int(t)
        kind = d.get("kind")
        if amt is None or kind not in ("dep", "wd"):
            return await m.reply_text("❌ Summani faqat raqam bilan kiriting. Masalan: 50000")
        lo = MIN_DEP if kind == "dep" else MIN_WD
        if amt < lo:
            return await m.reply_text(f"❌ Minimal summa: {fmt(lo)} so'm.")
        if amt > MAX_AMOUNT:
            return await m.reply_text(f"❌ Maksimal summa: {fmt(MAX_AMOUNT)} so'm.")
        d["amount"] = amt
        if kind == "dep":
            d["step"] = "photo"
            return await m.reply_text(
                f"💳 {fmt(amt)} so'mni shu kartaga o'tkazing:\n{cards_text()}\n\n"
                "To'lov chekini (rasm yoki PDF) yuboring 📸")
        d["step"] = "code"
        return await m.reply_text("🔑 1x'dan olgan yechish kodini yuboring:")

    if step == "code" and t:
        if len(t) > 64:
            return await m.reply_text("❌ Kod juda uzun. Qaytadan yuboring.")
        d["code"] = t; d["step"] = "card"
        return await m.reply_text("💳 Pul tushadigan karta raqamingiz (16 xonali):")

    if step == "card" and t:
        digits = re.sub(r"[\s-]", "", t)
        if not re.fullmatch(r"[0-9]{16}", digits):
            return await m.reply_text("❌ Karta raqami 16 ta raqamdan iborat bo'lishi kerak.")
        number = " ".join(digits[i:i + 4] for i in range(0, 16, 4))
        return await submit(update, ctx, extra=f"Kod: {d.get('code')}\nKarta: {number}")

    if step == "photo":
        if m.photo:
            return await submit(update, ctx, extra="", file_id=m.photo[-1].file_id, is_doc=False)
        doc = m.document
        if doc and doc.mime_type and (doc.mime_type.startswith("image/") or doc.mime_type == "application/pdf"):
            return await submit(update, ctx, extra="", file_id=doc.file_id, is_doc=True)
        return await m.reply_text("📸 Iltimos, to'lov chekini rasm yoki PDF ko'rinishida yuboring.")

    await m.reply_text("Iltimos, menyudan bo'lim tanlang yoki so'ralgan ma'lumotni yuboring.",
                       reply_markup=menu(uid))


async def submit(update, ctx, extra, file_id=None, is_doc=False):
    d = ctx.user_data
    uid = update.effective_user.id
    if not all(k in d for k in ("kind", "x_id", "amount")):
        d.clear()
        return await update.message.reply_text("⚠️ Ma'lumotlar yo'qolgan, iltimos qaytadan boshlang.",
                                               reply_markup=menu(uid))
    cur = db.execute("INSERT INTO orders(user_id,kind,x_id,amount,extra) VALUES(?,?,?,?,?)",
                     (uid, d["kind"], d["x_id"], d["amount"], extra))
    db.commit()
    oid = cur.lastrowid
    title = "💳 TO'LDIRISH" if d["kind"] == "dep" else "💸 YECHISH"
    text = f"{title} #{oid}\nUser: {uid}\n1x ID: {d['x_id']}\nSumma: {fmt(d['amount'])}\n{extra}"
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Bajarildi", callback_data=f"ok_{oid}"),
                                InlineKeyboardButton("❌ Rad", callback_data=f"no_{oid}")]])
    try:
        if file_id and is_doc:
            await ctx.bot.send_document(ADMIN_ID, file_id, caption=text, reply_markup=kb)
        elif file_id:
            await ctx.bot.send_photo(ADMIN_ID, file_id, caption=text, reply_markup=kb)
        else:
            await ctx.bot.send_message(ADMIN_ID, text, reply_markup=kb)
    except TelegramError as e:
        log.error("Adminga yuborib bo'lmadi: %s", e)
        db.execute("DELETE FROM orders WHERE id=?", (oid,))
        db.commit()
        d.clear()
        return await update.message.reply_text("⚠️ So'rovni hozir yuborib bo'lmadi. Keyinroq urinib ko'ring.",
                                               reply_markup=menu(uid))
    d.clear()
    await update.message.reply_text("✅ So'rovingiz adminga yuborildi. Kuting.", reply_markup=menu(uid))


# ───────────────────────── ADMIN CALLBACK ─────────────────────────


async def admin_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.from_user.id != ADMIN_ID:
        return await q.answer("Ruxsat yo'q", show_alert=True)
    await q.answer()
    act, val = q.data.split("_", 1)
    if not val.isdecimal() and act != "pn":
        return

    if act == "rp":
        ctx.user_data.clear()
        ctx.user_data.update(step="reply", target=int(val))
        return await q.message.reply_text("✍️ Javobingizni yozing:", reply_markup=cancel_kb())
    if act == "dc":
        db.execute("DELETE FROM cards WHERE id=?", (int(val),))
        db.commit()
        return await q.message.reply_text("🗑 Karta o'chirildi.")
    if act == "pn":
        return await send_cards(q.message)
    if act in ("pa", "pr"):
        return await p2p_admin_action(q, ctx, act, int(val))

    # ok / no — to'ldirish va yechish so'rovlari
    oid = int(val)
    status = "done" if act == "ok" else "rejected"
    cur = db.execute("UPDATE orders SET status=? WHERE id=? AND status='pending'", (status, oid))
    db.commit()
    if cur.rowcount == 0:  # allaqachon ko'rib chiqilgan (ikki marta bosish)
        return
    row = db.execute("SELECT user_id FROM orders WHERE id=?", (oid,)).fetchone()
    if row:
        await safe_send(ctx.bot, row[0], f"✅ #{oid} bajarildi." if act == "ok" else f"❌ #{oid} rad etildi.")
    await mark_message(q, f"\n\nHolat: {status}")


# ───────────────────────── P2P BOZOR ─────────────────────────


async def p2p_menu(target):
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ E'lon berish", callback_data="p2_new")],
        [InlineKeyboardButton("📃 E'lonlar", callback_data="p2_list"),
         InlineKeyboardButton("🗂 Mening e'lonlarim", callback_data="p2_my")]])
    await target.reply_html("🔄 <b>P2P bozor</b>\n━━━━━━━━━━━━━━\n"
                            "Foydalanuvchilar shu yerda bir-biri bilan kelishadi.\n\n" + P2P_WARN,
                            reply_markup=kb)


def _ads_count(uid):
    return db.execute("SELECT COUNT(*) FROM p2p WHERE user_id=? AND status IN ('pending','active')",
                      (uid,)).fetchone()[0]


async def p2p_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    u = q.from_user
    d = ctx.user_data
    p = q.data.split("_")
    act = p[1] if len(p) > 1 else ""

    if act == "new":
        if _ads_count(u.id) >= MAX_P2P_ADS:
            return await q.message.reply_text(
                f"❌ Sizda {MAX_P2P_ADS} tadan ortiq faol e'lon bo'lishi mumkin emas. Avval eskisini yoping.")
        kb = InlineKeyboardMarkup([[InlineKeyboardButton(SIDE["buy"], callback_data="p2_side_buy"),
                                    InlineKeyboardButton(SIDE["sell"], callback_data="p2_side_sell")]])
        return await q.message.reply_text("Nima qilmoqchisiz?", reply_markup=kb)

    if act == "side" and len(p) > 2 and p[2] in SIDE:
        d.clear(); d.update(step="p2amount", side=p[2])
        warn = ""
        if not u.username:
            warn = ("\n\n⚠️ Sizda Telegram username yo'q, shuning uchun boshqalar siz bilan bog'lana "
                    "olmasligi mumkin. Sozlamalardan username o'rnating.")
        return await q.message.reply_text("💵 Summani kiriting (so'm):" + warn, reply_markup=cancel_kb())

    if act == "list":
        rows = db.execute("SELECT id, side, amount, note FROM p2p WHERE status='active' AND user_id != ? "
                          "ORDER BY id DESC LIMIT 10", (u.id,)).fetchall()
        if not rows:
            return await q.message.reply_text("📃 Hozircha e'lonlar yo'q.")
        kb = InlineKeyboardMarkup([[InlineKeyboardButton(
            f"{ICON.get(s, '•')} {fmt(a)} · {(n or '')[:18]}", callback_data=f"p2_c_{i}")]
            for i, s, a, n in rows])
        return await q.message.reply_text("📃 Faol e'lonlar. Bog'lanish uchun tanlang:", reply_markup=kb)

    if act == "my":
        rows = db.execute("SELECT id, side, amount, status FROM p2p WHERE user_id=? "
                          "AND status IN ('pending','active') ORDER BY id DESC LIMIT 10", (u.id,)).fetchall()
        if not rows:
            return await q.message.reply_text("🗂 Sizda faol e'lon yo'q.")
        kb = InlineKeyboardMarkup([[InlineKeyboardButton(
            f"🗑 {ICON.get(s, '•')} #{i} · {fmt(a)} so'm {'⏳' if st == 'pending' else '✅'}",
            callback_data=f"p2_x_{i}")] for i, s, a, st in rows])
        return await q.message.reply_text("🗂 Yopish uchun e'lonni bosing:", reply_markup=kb)

    if act == "x" and len(p) > 2 and p[2].isdecimal():
        cur = db.execute("UPDATE p2p SET status='closed' WHERE id=? AND user_id=? "
                         "AND status IN ('pending','active')", (int(p[2]), u.id))
        db.commit()
        return await q.message.reply_text("🗑 E'lon yopildi." if cur.rowcount else "E'lon allaqachon yopilgan.")

    if act == "c" and len(p) > 2 and p[2].isdecimal():
        ad_id = int(p[2])
        row = db.execute("SELECT user_id, uname, side, amount, note FROM p2p "
                         "WHERE id=? AND status='active'", (ad_id,)).fetchone()
        if not row:
            return await q.message.reply_text("❌ E'lon topilmadi yoki yopilgan.")
        owner, ouname, side, amount, note = row
        if owner == u.id:
            return await q.message.reply_text("Bu sizning e'loningiz.")
        try:  # bir foydalanuvchi bir e'longa faqat bir marta bog'lana oladi
            db.execute("INSERT INTO p2p_contacts(ad_id, user_id) VALUES(?,?)", (ad_id, u.id))
            db.commit()
        except sqlite3.IntegrityError:
            return await q.message.reply_text("Siz bu e'lon egasi bilan allaqachon bog'langansiz.")
        me = mention(u.id, u.username)
        await safe_send(ctx.bot, owner, f"🤝 E'loningiz #{ad_id} ga qiziqish bildirildi:\n{me}\n\n" + P2P_WARN,
                        parse_mode="HTML")
        await safe_send(ctx.bot, ADMIN_ID, f"🔄 P2P #{ad_id}: {mention(owner, ouname)} ↔ {me}\n"
                                           f"Summa: {fmt(amount)}", parse_mode="HTML")
        return await q.message.reply_html(
            f"{SIDE.get(side, side)}: {fmt(amount)} so'm\n📝 {html.escape(note or '')}\n\n"
            f"E'lon egasi: {mention(owner, ouname)}\n\n{P2P_WARN}")


async def p2p_step(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """E'lon berish bosqichlari. Qayta ishlangan bo'lsa True qaytaradi."""
    m = update.message
    d = ctx.user_data
    uid = update.effective_user.id
    t = (m.text or "").strip()
    st = d.get("step")

    if st == "p2amount":
        amt = parse_int(t)
        if amt is None:
            await m.reply_text("❌ Summani faqat raqam bilan kiriting. Masalan: 50000")
        elif amt < MIN_DEP:
            await m.reply_text(f"❌ Minimal summa: {fmt(MIN_DEP)} so'm.")
        elif amt > MAX_AMOUNT:
            await m.reply_text(f"❌ Maksimal summa: {fmt(MAX_AMOUNT)} so'm.")
        else:
            d["amount"] = amt; d["step"] = "p2note"
            await m.reply_text("📝 Izoh yozing (kurs, to'lov usuli, vaqt):")
        return True

    if st == "p2note" and t:
        if "side" not in d or "amount" not in d:
            d.clear()
            await m.reply_text("⚠️ Ma'lumotlar yo'qolgan, qaytadan boshlang.", reply_markup=menu(uid))
            return True
        if _ads_count(uid) >= MAX_P2P_ADS:
            d.clear()
            await m.reply_text(f"❌ Faol e'lonlar soni {MAX_P2P_ADS} tadan oshmasligi kerak.",
                               reply_markup=menu(uid))
            return True
        note = t[:200]
        cur = db.execute("INSERT INTO p2p(user_id,uname,side,amount,note,created_at) VALUES(?,?,?,?,?,?)",
                         (uid, update.effective_user.username, d["side"], d["amount"], note, int(time.time())))
        db.commit()
        oid = cur.lastrowid
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Chop etish", callback_data=f"pa_{oid}"),
                                    InlineKeyboardButton("❌ Rad", callback_data=f"pr_{oid}")]])
        ok = await safe_send(ctx.bot, ADMIN_ID,
                             f"🔄 Yangi P2P e'lon #{oid}\nUser: {uid}\n{SIDE[d['side']]}\n"
                             f"Summa: {fmt(d['amount'])}\nIzoh: {note}", reply_markup=kb)
        if not ok:
            db.execute("DELETE FROM p2p WHERE id=?", (oid,))
            db.commit()
        d.clear()
        await m.reply_text("✅ E'lon adminga tekshiruvga yuborildi." if ok
                           else "⚠️ E'lonni hozir yuborib bo'lmadi. Keyinroq urinib ko'ring.",
                           reply_markup=menu(uid))
        return True
    return False


async def p2p_admin_action(q, ctx, act, oid):
    """Admin e'lonni chop etadi (pa) yoki rad etadi (pr)."""
    st = "active" if act == "pa" else "rejected"
    cur = db.execute("UPDATE p2p SET status=?, created_at=? WHERE id=? AND status='pending'",
                     (st, int(time.time()), oid))
    db.commit()
    if cur.rowcount == 0:
        return
    row = db.execute("SELECT user_id FROM p2p WHERE id=?", (oid,)).fetchone()
    if row:
        await safe_send(ctx.bot, row[0], f"✅ P2P e'loningiz #{oid} chop etildi." if act == "pa"
                        else f"❌ P2P e'loningiz #{oid} rad etildi.")
    await mark_message(q, f"\n\nHolat: {st}")


async def expire_p2p(ctx: ContextTypes.DEFAULT_TYPE):
    cutoff = int(time.time()) - P2P_TTL_HOURS * 3600
    cur = db.execute("UPDATE p2p SET status='closed' WHERE status='active' AND created_at>0 AND created_at<?",
                     (cutoff,))
    db.commit()
    if cur.rowcount:
        log.info("%s ta eski P2P e'lon yopildi", cur.rowcount)


# ───────────────────────── GURUH HIMOYASI ─────────────────────────

_admin_cache = {}   # (chat_id, user_id) -> (is_admin, vaqt)
_warned = {}        # (chat_id, user_id) -> oxirgi ogohlantirish vaqti


async def is_chat_admin(bot, chat_id, user_id):
    key, now = (chat_id, user_id), time.time()
    hit = _admin_cache.get(key)
    if hit and now - hit[1] < 300:
        return hit[0]
    member = await bot.get_chat_member(chat_id, user_id)
    res = member.status in ("administrator", "creator")
    _admin_cache[key] = (res, now)
    return res


async def _delete_job(ctx: ContextTypes.DEFAULT_TYPE):
    try:
        await ctx.bot.delete_message(*ctx.job.data)
    except TelegramError:
        pass


async def moderate(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Guruhda fayl, ovozli xabar va havolalarni o'chiradi (guruh adminlariga tegmaydi)."""
    m, u = update.effective_message, update.effective_user
    if not m or not u or m.sender_chat or u.id == ADMIN_ID:
        return
    if ALLOWED_CHATS and m.chat_id not in ALLOWED_CHATS:
        return
    row = db.execute("SELECT on_off FROM guard WHERE chat_id=?", (m.chat_id,)).fetchone()
    if row and row[0] == 0:
        return  # bu guruhda himoya o'chirilgan
    try:
        if await is_chat_admin(ctx.bot, m.chat_id, u.id):
            return
        await m.delete()
    except TelegramError:
        return  # bot admin emas yoki o'chirish huquqi yo'q

    key, now = (m.chat_id, u.id), time.time()
    if now - _warned.get(key, 0) < 60:  # ogohlantirish spamining oldini olish
        return
    _warned[key] = now
    try:
        note = await ctx.bot.send_message(
            m.chat_id, f"🚫 {u.mention_html()}, guruhda fayl, ovozli xabar va havola yuborish taqiqlangan.",
            parse_mode="HTML")
        if ctx.job_queue:
            ctx.job_queue.run_once(_delete_job, 8, data=(note.chat_id, note.message_id))
    except TelegramError:
        pass


async def guard_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/guard — himoyani yoqadi/o'chiradi. /guard on yoki /guard off ham ishlaydi."""
    m, u = update.effective_message, update.effective_user
    if ALLOWED_CHATS and m.chat_id not in ALLOWED_CHATS:
        return
    try:
        allowed = u.id == ADMIN_ID or await is_chat_admin(ctx.bot, m.chat_id, u.id)
    except TelegramError:
        return
    if not allowed:
        return
    row = db.execute("SELECT on_off FROM guard WHERE chat_id=?", (m.chat_id,)).fetchone()
    cur = 1 if row is None else row[0]
    if ctx.args and ctx.args[0].lower() in ("on", "off"):
        new = 1 if ctx.args[0].lower() == "on" else 0
    else:
        new = 0 if cur else 1
    db.execute("INSERT OR REPLACE INTO guard(chat_id, on_off) VALUES(?,?)", (m.chat_id, new))
    db.commit()
    await m.reply_text("🛡 Himoya yoqildi ✅" if new else "🛡 Himoya o'chirildi ❌")


# ───────────────────────── XATO USHLAGICH VA ISHGA TUSHIRISH ─────────────────────────


async def on_error(update, ctx: ContextTypes.DEFAULT_TYPE):
    log.error("Kutilmagan xato", exc_info=ctx.error)
    if isinstance(update, Update) and update.effective_chat and update.effective_chat.type == "private":
        await safe_send(ctx.bot, update.effective_chat.id,
                        "⚠️ Xatolik yuz berdi. Iltimos, /start ni bosib qaytadan urinib ko'ring.")


def main():
    if not BOT_TOKEN:
        raise SystemExit("❌ BOT_TOKEN topilmadi. .env faylga yoki muhit o'zgaruvchilariga qo'shing.")
    if not ADMIN_ID:
        raise SystemExit("❌ ADMIN_ID topilmadi yoki noto'g'ri (raqam bo'lishi kerak).")

    init_db()
    app = (Application.builder().token(BOT_TOKEN)
           .persistence(PicklePersistence(filepath=PERSIST_PATH)).build())

    private = filters.ChatType.PRIVATE
    app.add_handler(CommandHandler("start", start, filters=private))
    app.add_handler(CommandHandler("help", help_cmd, filters=private))
    app.add_handler(CommandHandler("cancel", cancel_cmd, filters=private))
    app.add_handler(CommandHandler("guard", guard_cmd, filters=filters.ChatType.GROUPS))

    app.add_handler(CallbackQueryHandler(admin_cb, pattern=r"^(ok|no|rp|dc|pn|pa|pr)_"))
    app.add_handler(CallbackQueryHandler(p2p_cb, pattern=r"^p2_"))

    blocked = (filters.Document.ALL | filters.VOICE | filters.VIDEO_NOTE | filters.AUDIO
               | filters.Entity("url") | filters.Entity("text_link")
               | filters.CaptionEntity("url") | filters.CaptionEntity("text_link"))
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & blocked, moderate))

    app.add_handler(MessageHandler(
        private & ((filters.TEXT & ~filters.COMMAND) | filters.PHOTO | filters.Document.ALL), handle))

    app.add_error_handler(on_error)
    if app.job_queue:
        app.job_queue.run_repeating(expire_p2p, interval=3600, first=60)
    else:
        log.warning("JobQueue yo'q: pip install \"python-telegram-bot[job-queue]\"")

    log.info("Bot ishga tushdi")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
