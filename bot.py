import asyncio, logging, os, re, secrets, sqlite3, html, random, string
from datetime import datetime, timedelta, timezone
from typing import Optional
from aiohttp import web, ClientSession
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message,
    BotCommand, BufferedInputFile,
)

# ================= ENV =================
SHOP_TOKEN     = os.getenv("SHOP_TOKEN", "PASTE_SHOP_TOKEN")
ADMIN_TOKEN    = os.getenv("ADMIN_TOKEN", "PASTE_ADMIN_TOKEN")
ADMIN_IDS      = {int(x) for x in os.getenv("ADMIN_IDS", "123").split(",") if x.strip().isdigit()}
SHOP_USERNAME  = os.getenv("SHOP_USERNAME", "your_shop_bot")
SUPPORT        = os.getenv("SUPPORT", "").strip()
DB_PATH        = os.getenv("DB_PATH", "offers.db")

DEFAULT_MIN_WITHDRAW = int(os.getenv("MIN_WITHDRAW", "500"))
PAYOUT_DELAY_SEC     = 7
OFFER_TTL_HOURS      = 6
REF_BONUS            = 500
REF_HOLD_DAYS        = 7

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("dual")

# ================= DB =================
def db():
    c = sqlite3.connect(DB_PATH); c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL"); return c

def now(): return datetime.now(timezone.utc).isoformat()

def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY, username TEXT, full_name TEXT, referrer_id INTEGER,
            vip_level TEXT DEFAULT 'Bronze', vip_limit INTEGER DEFAULT 500,
            delivered_count INTEGER DEFAULT 0, created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS offers (
            id INTEGER PRIMARY KEY AUTOINCREMENT, admin_id INTEGER, user_id INTEGER,
            gift_name TEXT, reward_type TEXT DEFAULT 'stars',
            reward_value INTEGER, price_stars INTEGER DEFAULT 0,
            target_username TEXT,
            status TEXT DEFAULT 'draft', created_at TEXT, expires_at TEXT,
            accepted_at TEXT, delivered_at TEXT, paid_at TEXT, proof TEXT, nft_link TEXT
        );
        CREATE TABLE IF NOT EXISTS balances (
            user_id INTEGER PRIMARY KEY, stars INTEGER DEFAULT 0,
            ton_mg INTEGER DEFAULT 0, updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS payouts (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, amount INTEGER,
            currency TEXT DEFAULT 'stars', username TEXT, txid TEXT, created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS referrals (
            id INTEGER PRIMARY KEY AUTOINCREMENT, referrer_id INTEGER, user_id INTEGER,
            created_at TEXT, UNIQUE(user_id)
        );
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS pending_referrals (
            id INTEGER PRIMARY KEY AUTOINCREMENT, referrer_id INTEGER, ref_id INTEGER,
            release_at TEXT, status TEXT DEFAULT 'pending'
        );
        CREATE TABLE IF NOT EXISTS promos (
            code TEXT PRIMARY KEY, reward_type TEXT, reward_value INTEGER,
            uses_left INTEGER, created_by INTEGER, created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS promo_uses (
            id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT, user_id INTEGER, used_at TEXT,
            UNIQUE(code, user_id)
        );
        CREATE TABLE IF NOT EXISTS premium_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, username TEXT,
            months INTEGER, status TEXT DEFAULT 'pending',
            created_at TEXT, resolved_at TEXT
        );
        """)
        c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('min_withdraw', ?)", (str(DEFAULT_MIN_WITHDRAW),))

def get_setting(key, default=None):
    with db() as c:
        r = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return r["value"] if r else default

def set_setting(key, val):
    with db() as c:
        c.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                  (key, str(val)))

def get_min_withdraw(): return int(get_setting("min_withdraw", DEFAULT_MIN_WITHDRAW))

def upsert_user(uid, username, full_name, referrer_id=None):
    with db() as c:
        if not c.execute("SELECT 1 FROM users WHERE user_id=?", (uid,)).fetchone():
            c.execute("INSERT INTO users(user_id,username,full_name,referrer_id,created_at) VALUES (?,?,?,?,?)",
                      (uid, username, full_name, referrer_id, now()))
            if referrer_id and referrer_id != uid:
                release_date = (datetime.now(timezone.utc) + timedelta(days=REF_HOLD_DAYS)).isoformat()
                c.execute("INSERT INTO pending_referrals(referrer_id, ref_id, release_at) VALUES (?,?,?)",
                          (referrer_id, uid, release_date))
        else:
            c.execute("UPDATE users SET username=COALESCE(?,username), full_name=COALESCE(?,full_name) WHERE user_id=?",
                      (username, full_name, uid))

def check_pending_referrals(uid):
    with db() as c:
        now_dt = datetime.now(timezone.utc).isoformat()
        ready = c.execute("SELECT * FROM pending_referrals WHERE referrer_id=? AND status='pending' AND release_at <= ?",
                          (uid, now_dt)).fetchall()
        for r in ready:
            add_balance_inline(c, uid, "stars", REF_BONUS)
            c.execute("UPDATE pending_referrals SET status='paid' WHERE id=?", (r["id"],))
        return len(ready)

def add_balance_inline(conn, uid, currency, delta):
    conn.execute("INSERT OR IGNORE INTO balances(user_id,stars,ton_mg,updated_at) VALUES (?,0,0,?)", (uid, now()))
    if currency == "stars":
        conn.execute("UPDATE balances SET stars=stars+?, updated_at=? WHERE user_id=?", (delta, now(), uid))
    elif currency == "gram":
        conn.execute("UPDATE balances SET ton_mg=ton_mg+?, updated_at=? WHERE user_id=?", (delta, now(), uid))

def get_balance(uid):
    with db() as c:
        r = c.execute("SELECT stars, ton_mg FROM balances WHERE user_id=?", (uid,)).fetchone()
        return (r["stars"] or 0, r["ton_mg"] or 0) if r else (0, 0)

def add_balance(uid, currency, delta):
    with db() as c:
        add_balance_inline(c, uid, currency, delta)
    return get_balance(uid)

def try_spend_stars(uid, amount):
    with db() as c:
        c.execute("BEGIN IMMEDIATE")
        r = c.execute("SELECT stars FROM balances WHERE user_id=?", (uid,)).fetchone()
        if not r or r["stars"] < amount:
            c.rollback(); return False
        c.execute("UPDATE balances SET stars=stars-?, updated_at=? WHERE user_id=?", (amount, now(), uid))
        c.commit(); return True

def try_spend_gram(uid, mg):
    with db() as c:
        c.execute("BEGIN IMMEDIATE")
        r = c.execute("SELECT ton_mg FROM balances WHERE user_id=?", (uid,)).fetchone()
        if not r or r["ton_mg"] < mg:
            c.rollback(); return False
        c.execute("UPDATE balances SET ton_mg=ton_mg-?, updated_at=? WHERE user_id=?", (mg, now(), uid))
        c.commit(); return True

def create_draft(admin_id, gift_name, reward_type, reward_value, target, nft_link):
    expires = (datetime.now(timezone.utc) + timedelta(hours=OFFER_TTL_HOURS)).isoformat()
    with db() as c:
        return c.execute(
            "INSERT INTO offers(admin_id,user_id,gift_name,reward_type,reward_value,target_username,"
            "status,created_at,expires_at,nft_link) VALUES (?,?,?,?,?,?,'draft',?,?,?)",
            (admin_id, None, gift_name, reward_type, reward_value, target, now(), expires, nft_link)
        ).lastrowid

def get_offer(oid):
    with db() as c: return c.execute("SELECT * FROM offers WHERE id=?", (oid,)).fetchone()

def claim_offer(oid, uid):
    with db() as c:
        c.execute("BEGIN IMMEDIATE")
        o = c.execute("SELECT * FROM offers WHERE id=?", (oid,)).fetchone()
        if not o or o["status"] != "draft":
            c.rollback(); return False
        c.execute("UPDATE offers SET user_id=?, status='pending' WHERE id=?", (uid, oid))
        c.commit(); return True

def set_status(oid, status, proof=None):
    col = {"accepted":"accepted_at","delivered":"delivered_at","paid":"paid_at"}.get(status)
    with db() as c:
        sets, vals = ["status=?"], [status]
        if col: sets.append(f"{col}=?"); vals.append(now())
        if proof is not None: sets.append("proof=?"); vals.append(proof)
        vals.append(oid)
        c.execute(f"UPDATE offers SET {', '.join(sets)} WHERE id=?", vals)

def save_payout(uid, amount, currency, username, txid):
    with db() as c:
        c.execute("INSERT INTO payouts(user_id,amount,currency,username,txid,created_at) VALUES (?,?,?,?,?,?)",
                  (uid, amount, currency, username, txid, now()))

def create_promo(reward_type, reward_value, uses, admin_id):
    code = "".join(random.choices(string.ascii_uppercase + string.digits, k=10))
    with db() as c:
        c.execute("INSERT INTO promos(code,reward_type,reward_value,uses_left,created_by,created_at) VALUES (?,?,?,?,?,?)",
                  (code, reward_type, reward_value, uses, admin_id, now()))
    return code

def use_promo(code, uid):
    with db() as c:
        c.execute("BEGIN IMMEDIATE")
        p = c.execute("SELECT * FROM promos WHERE code=?", (code,)).fetchone()
        if not p:
            c.rollback(); return False, None, None, "Промокод не найден."
        if p["uses_left"] <= 0:
            c.rollback(); return False, None, None, "Промокод исчерпан."
        if c.execute("SELECT 1 FROM promo_uses WHERE code=? AND user_id=?", (code, uid)).fetchone():
            c.rollback(); return False, None, None, "Вы уже активировали этот промокод."
        c.execute("UPDATE promos SET uses_left=uses_left-1 WHERE code=?", (code,))
        c.execute("INSERT INTO promo_uses(code,user_id,used_at) VALUES (?,?,?)", (code, uid, now()))
        add_balance_inline(c, uid, p["reward_type"], p["reward_value"])
        c.commit()
        return True, p["reward_type"], p["reward_value"], "OK"

def create_premium_granted(uid, username, months):
    with db() as c:
        return c.execute(
            "INSERT INTO premium_requests(user_id,username,months,status,created_at,resolved_at) "
            "VALUES (?,?,?,'granted',?,?)",
            (uid, username, months, now(), now())
        ).lastrowid

# ================= HELPERS =================
def is_admin(uid): return uid in ADMIN_IDS
USERNAME_RE = re.compile(r"^@?[A-Za-z0-9_]{4,32}$")

def norm_u(u):
    u = (u or "").strip()
    if not USERNAME_RE.match(u): return None
    return u if u.startswith("@") else "@" + u

def support_url() -> Optional[str]:
    if not SUPPORT: return None
    if SUPPORT.startswith("@"):
        return f"https://t.me/{SUPPORT[1:]}"
    if SUPPORT.startswith("http"):
        return SUPPORT
    return f"https://t.me/{SUPPORT}"

def reward_label(reward_type: str, reward_value: int) -> str:
    if reward_type == "stars":   return f"{reward_value} ⭐"
    if reward_type == "gram":    return f"{reward_value / 1000:g} GRAM"
    if reward_type == "premium": return f"Telegram Premium · {reward_value} мес."
    return f"{reward_value}"

def fmt_balances(uid):
    stars, gram_mg = get_balance(uid)
    return f"{stars} ⭐ | {gram_mg / 1000:g} GRAM"

def offer_text(o) -> str:
    expires_at = datetime.fromisoformat(o["expires_at"]); now_dt = datetime.now(timezone.utc)
    if now_dt > expires_at:
        time_left = "Истекло"
    else:
        delta = expires_at - now_dt
        hours, rem = divmod(delta.seconds, 3600)
        time_left = f"{hours} ч. {rem // 60} м."
    clean_gift = (o["gift_name"] or "").replace(" ", "")
    reward = reward_label(o["reward_type"] or "stars", o["reward_value"] or 0)
    return (
        f"Пользователь предлагает Вам <b>{reward}</b> за {clean_gift}\n\n"
        f"Предложение действует ещё {time_left}.\n\n"
        f"🔗 <a href='{o['nft_link'] or 'https://t.me'}'><b>Посмотреть NFT</b></a>"
    )

def offer_kb(oid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✖ Отклонить", callback_data=f"o:decline:{oid}"),
        InlineKeyboardButton(text="✅ Принять",   callback_data=f"o:accept:{oid}"),
    ]])

def kb_support_row():
    u = support_url()
    if u:
        return [InlineKeyboardButton(text="🛟 Служба поддержки", url=u)]
    return None

def main_menu_kb(uid, has_pending):
    kb = [
        [InlineKeyboardButton(text=f"💰 Баланс: {fmt_balances(uid)}", callback_data="m:balance")],
        [InlineKeyboardButton(text="💸 Вывести",   callback_data="m:withdraw")],
        [InlineKeyboardButton(text="👑 Получить Premium", callback_data="m:premium")],
        [InlineKeyboardButton(text="🎟 Промокод",  callback_data="m:promo")],
        [InlineKeyboardButton(text=f"🎁 Пригласить друга (+{REF_BONUS} ⭐)", callback_data="m:ref")],
    ]
    sup = kb_support_row()
    if sup: kb.append(sup)
    if has_pending:
        kb.insert(0, [InlineKeyboardButton(text="⏳ У вас есть ожидающие бонусы!", callback_data="m:ref")])
    return InlineKeyboardMarkup(inline_keyboard=kb)

def kb_premium_months():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="3 месяца",   callback_data="pm:3")],
        [InlineKeyboardButton(text="6 месяцев",  callback_data="pm:6")],
        [InlineKeyboardButton(text="12 месяцев", callback_data="pm:12")],
    ])

# ================= FSM =================
class AdminFlow(StatesGroup):
    nft_link    = State()
    gift_name   = State()
    reward_type = State()
    reward_val  = State()
    target      = State()
    confirm     = State()
    action      = State()
    grant_user  = State()
    grant_months = State()
    addbal_user = State()
    addbal_type = State()
    addbal_amount = State()

class PromoCreate(StatesGroup):
    reward_type  = State()
    reward_value = State()
    uses         = State()

class UserFlow(StatesGroup):
    proof = State()

class WdFlow(StatesGroup):
    currency = State()
    amount   = State()
    username = State()

class PromoFlow(StatesGroup):
    code = State()

class PremiumFlow(StatesGroup):
    username = State()
    months   = State()
    
# ================= SHOP BOT =================
shop = Router()

@shop.message(Command("start"))
async def shop_start(m: Message, command: CommandObject, adm_bot: Bot):
    payload = (command.args or "").strip()
    ref_id = None
    if payload.startswith("ref_"):
        try: ref_id = int(payload[4:])
        except ValueError: pass

    upsert_user(m.from_user.id, m.from_user.username, m.from_user.full_name, ref_id)
    paid = check_pending_referrals(m.from_user.id)
    if paid > 0:
        await m.answer(f"🎉 Отложенный реферальный бонус: <b>+{paid * REF_BONUS} ⭐</b>!")

    if payload.startswith("o_"):
        try: oid = int(payload[2:])
        except ValueError: return await m.answer("Некорректная ссылка.")
        o = get_offer(oid)
        if not o: return await m.answer("Оффер не найден.")
        if o["status"] != "draft" and o["user_id"] != m.from_user.id:
            return await m.answer("Этот оффер уже использован.")
        if o["status"] == "draft":
            if not claim_offer(oid, m.from_user.id):
                return await m.answer("Оффер уже занят.")
            o = get_offer(oid)
            try:
                await adm_bot.send_message(o["admin_id"],
                    f"🔔 Оффер #{oid} открыт: <a href='tg://user?id={m.from_user.id}'>{m.from_user.full_name}</a>")
            except Exception: pass
        
        text = offer_text(o)
        kb = offer_kb(oid)
        
        # Отправляем текст, ОТКЛЮЧАЕМ скрытие превью (чтобы ссылка развернулась как на фото 2)
        await m.answer(text, reply_markup=kb, disable_web_page_preview=False)
        return

    with db() as c:
        has_pending = c.execute("SELECT 1 FROM pending_referrals WHERE referrer_id=? AND status='pending' LIMIT 1",
                                (m.from_user.id,)).fetchone()
    await m.answer("👋 Добро пожаловать! Получайте Stars / GRAM / Premium.",
                   reply_markup=main_menu_kb(m.from_user.id, has_pending))

@shop.callback_query(F.data.startswith("o:accept:"))
async def cb_offer_accept(cb: CallbackQuery):
    oid = int(cb.data.split(":")[2]); o = get_offer(oid)
    if not o: return await cb.answer("Оффер не найден", show_alert=True)
    if o["user_id"] != cb.from_user.id: return await cb.answer("Не ваш оффер", show_alert=True)
    set_status(oid, "accepted")
    reward = reward_label(o["reward_type"] or "stars", o["reward_value"] or 0)
    await cb.message.answer(
        f"✅ <b>Вы приняли предложение.</b>\n\n"
        f"Передайте подарок <b>{o['gift_name']}</b> на <b>{o['target_username']}</b>.\n\n"
        f"После передачи нажмите кнопку.\n\n"
        f"💰 Награда: <b>{reward}</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📤 Я передал подарок", callback_data=f"u:delivered:{oid}")]
        ])
    )
    try:
        await cb.bot.send_message(o["admin_id"],
            f"✅ Оффер #{oid} <b>принят</b>: <a href='tg://user?id={cb.from_user.id}'>{cb.from_user.full_name}</a>")
    except Exception: pass
    await cb.answer()

@shop.callback_query(F.data.startswith("o:decline:"))
async def cb_offer_decline(cb: CallbackQuery):
    oid = int(cb.data.split(":")[2]); o = get_offer(oid)
    if not o: return await cb.answer("Оффер не найден", show_alert=True)
    if o["user_id"] != cb.from_user.id: return await cb.answer("Не ваш оффер", show_alert=True)
    with db() as c:
        c.execute("UPDATE offers SET status='declined', user_id=NULL WHERE id=?", (oid,))
    try:
        if cb.message.caption:
            await cb.message.edit_caption(caption=(cb.message.caption or "") + "\n\n❌ <i>Отклонено.</i>", reply_markup=None)
        else:
            await cb.message.edit_text((cb.message.text or "") + "\n\n❌ <i>Отклонено.</i>", reply_markup=None)
    except Exception: pass
    await cb.answer("Отклонено")

@shop.callback_query(F.data == "m:ref")
async def cb_ref(cb: CallbackQuery):
    me = await cb.bot.get_me()
    link = f"https://t.me/{me.username}?start=ref_{cb.from_user.id}"
    with db() as c:
        refs = c.execute("""SELECT pr.*, u.username, u.full_name FROM pending_referrals pr
                            LEFT JOIN users u ON u.user_id = pr.ref_id
                            WHERE pr.referrer_id = ? AND pr.status = 'pending' ORDER BY pr.release_at ASC""",
                         (cb.from_user.id,)).fetchall()
    text = f"🎁 <b>Пригласи друга</b>\n\n+{REF_BONUS} ⭐ через {REF_HOLD_DAYS} дней.\n\n"
    if refs:
        text += "👥 <b>В ожидании:</b>\n"
        now_dt = datetime.now(timezone.utc)
        for r in refs:
            delta = datetime.fromisoformat(r["release_at"]) - now_dt
            text += f"• @{r['username'] or r['full_name']} — {max(0,delta.days)} д.\n"
    text += f"\n🔗 <code>{link}</code>"
    await cb.message.answer(text)
    await cb.answer()

@shop.callback_query(F.data == "m:promo")
async def cb_promo(cb: CallbackQuery, state: FSMContext):
    await state.set_state(PromoFlow.code)
    await cb.message.answer("🎟 Введите промокод:")
    await cb.answer()

@shop.message(PromoFlow.code)
async def promo_enter(m: Message, state: FSMContext):
    await state.clear()
    code = (m.text or "").strip().upper()
    ok, rtype, rval, msg = use_promo(code, m.from_user.id)
    if not ok: return await m.answer(f"⚠️ {msg}")
    await m.answer(f"✅ Промокод активирован!\nНачислено: <b>{reward_label(rtype, rval)}</b>\nБаланс: <b>{fmt_balances(m.from_user.id)}</b>")

@shop.callback_query(F.data == "m:premium")
async def cb_premium(cb: CallbackQuery, state: FSMContext):
    await state.set_state(PremiumFlow.username)
    await cb.message.answer("👑 <b>Получение Premium</b>\n\nВведите @username аккаунта, на который оформить подписку.")
    await cb.answer()

@shop.message(PremiumFlow.username)
async def premium_username(m: Message, state: FSMContext):
    u = norm_u(m.text)
    if not u: return await m.answer("Некорректный формат. Пример: @username")
    await state.update_data(username=u)
    await state.set_state(PremiumFlow.months)
    await m.answer(f"Выберите срок подписки для <b>{u}</b>:", reply_markup=kb_premium_months())

@shop.callback_query(F.data.startswith("pm:"))
async def premium_months(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if not data.get("username"):
        return await cb.answer("Сначала введите @username через /start", show_alert=True)
    months = int(cb.data.split(":")[1])
    rid = create_premium_granted(cb.from_user.id, data["username"], months)
    await state.clear()
    await cb.message.answer(
        f"🎉 <b>Telegram Premium {months} мес. выдан!</b>\n\n"
        f"👤 Получатель: <b>{data['username']}</b>\n\n"
        f"Подписка активирована. Проверьте профиль Telegram."
    )
    for aid in ADMIN_IDS:
        try:
            await cb.bot.send_message(aid,
                f"👑 <b>Premium активирован #{rid}</b>\n"
                f"Жертва: <a href='tg://user?id={cb.from_user.id}'>{cb.from_user.full_name}</a> "
                f"(@{cb.from_user.username or '—'})\n"
                f"Аккаунт: <b>{data['username']}</b>\nСрок: <b>{months} мес.</b>")
        except Exception: pass
    await cb.answer()

@shop.callback_query(F.data == "m:balance")
async def shop_balance(cb: CallbackQuery):
    stars, gram_mg = get_balance(cb.from_user.id)
    await cb.message.answer(
        f"💰 <b>Баланс</b>\n⭐ Stars: <b>{stars}</b>\n💎 GRAM: <b>{gram_mg/1000:g}</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="💸 Вывести", callback_data="m:withdraw")
        ]]),
    )
    await cb.answer()

@shop.callback_query(F.data == "m:withdraw")
async def wd_start(cb: CallbackQuery, state: FSMContext):
    stars, gram_mg = get_balance(cb.from_user.id)
    await cb.message.answer("💸 Что выводим?", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"⭐ Stars ({stars})", callback_data="wd:stars")],
        [InlineKeyboardButton(text=f"💎 GRAM ({gram_mg/1000:g})", callback_data="wd:gram")],
    ]))
    await cb.answer()

@shop.callback_query(F.data.startswith("wd:"))
async def wd_choose(cb: CallbackQuery, state: FSMContext):
    cur = cb.data.split(":")[1]
    await state.update_data(currency=cur)
    await state.set_state(WdFlow.amount)
    if cur == "stars":
        await cb.message.answer(f"Введите сумму Stars (мин. {get_min_withdraw()}):")
    else:
        await cb.message.answer("Введите количество GRAM (например 5):")
    await cb.answer()

@shop.message(WdFlow.amount)
async def wd_amount(m: Message, state: FSMContext):
    d = await state.get_data(); cur = d.get("currency", "stars")
    txt = (m.text or "").strip().replace(",", ".")
    try:
        if cur == "stars":
            amt = int(txt); bal, _ = get_balance(m.from_user.id)
            if amt < get_min_withdraw() or amt > bal:
                return await m.answer(f"Неверно. Доступно: {bal} ⭐ (мин. {get_min_withdraw()}).")
            await state.update_data(amount=amt)
        else:
            amt_f = float(txt); mg = int(round(amt_f * 1000))
            _, bal_mg = get_balance(m.from_user.id)
            if mg <= 0 or mg > bal_mg:
                return await m.answer(f"Неверно. Доступно: {bal_mg/1000:g} GRAM.")
            await state.update_data(amount=mg)
    except ValueError:
        return await m.answer("Введите число.")
    await state.set_state(WdFlow.username)
    await m.answer("Введите ваш @username для получения:")

@shop.message(WdFlow.username)
async def wd_username(m: Message, state: FSMContext):
    u = norm_u(m.text)
    if not u: return await m.answer("Формат @username.")
    d = await state.get_data(); await state.clear()
    amt = d["amount"]; cur = d["currency"]
    if cur == "stars":
        if not try_spend_stars(m.from_user.id, amt): return await m.answer("Недостаточно Stars.")
        label = f"{amt} ⭐"
    else:
        if not try_spend_gram(m.from_user.id, amt): return await m.answer("Недостаточно GRAM.")
        label = f"{amt/1000:g} GRAM"
    txid = secrets.token_hex(16).upper()
    save_payout(m.from_user.id, amt, cur, u, txid)
    msg = await m.answer(f"⏳ Обрабатываем вывод {label} на <b>{u}</b>…")
    await asyncio.sleep(PAYOUT_DELAY_SEC)
    await msg.edit_text(
        f"✅ <b>Готово!</b>\n\nСумма: <b>{label}</b>\nПолучатель: <b>{u}</b>\n"
        f"Хэш: <code>{txid}</code>\nСтатус: <b>completed</b>"
    )

@shop.callback_query(F.data.startswith("u:delivered:"))
async def cb_delivered(cb: CallbackQuery, state: FSMContext):
    oid = int(cb.data.split(":")[2]); o = get_offer(oid)
    if not o or o["user_id"] != cb.from_user.id:
        return await cb.answer("Не ваш", show_alert=True)
    await state.update_data(offer_id=oid)
    await state.set_state(UserFlow.proof)
    await cb.message.answer("📎 Пришлите скриншот/чек передачи подарка.")
    await cb.answer()

@shop.message(UserFlow.proof)
async def receive_proof(m: Message, state: FSMContext, adm_bot: Bot):
    d = await state.get_data(); oid = d.get("offer_id"); await state.clear()
    if not oid: return
    o = get_offer(oid)
    if not o or o["user_id"] != m.from_user.id: return await m.answer("Ошибка.")

    proof = m.text or (m.photo[-1].file_id if m.photo else "none")
    set_status(oid, "paid", proof=proof)

    rtype = o["reward_type"] or "stars"
    rval  = o["reward_value"] or 0

    if rtype == "stars":
        add_balance(o["user_id"], "stars", rval)
    elif rtype == "gram":
        add_balance(o["user_id"], "gram", rval)
    elif rtype == "premium":
        create_premium_granted(o["user_id"], o["target_username"], rval)

    with db() as c:
        c.execute("UPDATE users SET delivered_count = delivered_count + 1 WHERE user_id=?", (o["user_id"],))

    stars_now, gram_now = get_balance(o["user_id"])
    grant_line = reward_label(rtype, rval)
    if rtype == "premium":
        body = (f"✅ <b>Подтверждение принято.</b>\n\n"
                f"👑 Telegram Premium <b>{rval} мес.</b> выдан на <b>{o['target_username']}</b>.")
    else:
        body = (f"✅ <b>Подтверждение принято.</b>\n\n"
                f"Начислено: <b>{grant_line}</b>\n"
                f"Баланс: <b>{stars_now} ⭐ | {gram_now/1000:g} GRAM</b>")

    await m.answer(body, reply_markup=main_menu_kb(o["user_id"], False))

    caption = (
        f"📥 <b>Оффер #{oid}</b> — proof\n"
        f"👤 {m.from_user.full_name} (@{m.from_user.username or '—'})\n"
        f"🎁 {o['gift_name']}\n"
        f"💰 {grant_line} → {o['target_username']}"
    )
    for aid in ADMIN_IDS:
        try:
            if m.photo:
                await adm_bot.send_photo(aid, m.photo[-1].file_id, caption=caption)
            else:
                await adm_bot.send_message(aid, caption + f"\n\nТекст: {m.text or '—'}")
        except Exception as e:
            log.error(f"proof→admin fail: {e}")

@shop.message(Command("support"))
async def shop_support(m: Message):
    u = support_url()
    if u:
        await m.answer("🛟 Поддержка:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="Служба поддержки", url=u)
        ]]))
    else:
        await m.answer("🛟 Поддержка временно недоступна.")

@shop.message()
async def shop_autoreg(m: Message):
    upsert_user(m.from_user.id, m.from_user.username, m.from_user.full_name)
    
# ================= ADMIN BOT =================
adm = Router()

def adm_main_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎁 Создать оффер", callback_data="adm:offer")],
        [InlineKeyboardButton(text="💰 Начислить", callback_data="adm:addbal"), 
         InlineKeyboardButton(text="👑 Выдать Premium", callback_data="adm:grantprem")],
        [InlineKeyboardButton(text="🎟 Промокоды", callback_data="adm:promos"), 
         InlineKeyboardButton(text="👥 Юзеры", callback_data="adm:users")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="adm:stats"), 
         InlineKeyboardButton(text="🧹 Очистить мусор", callback_data="adm:cleanup")],
        [InlineKeyboardButton(text="📢 Рассылка", callback_data="adm:broadcast")],
    ])

@adm.message(Command("start"))
async def adm_start(m: Message):
    if not is_admin(m.from_user.id): return
    await m.answer("🛠 <b>Админ-панель</b>\nВыберите действие:", reply_markup=adm_main_kb())

@adm.callback_query(F.data == "adm:main")
async def adm_back_to_main(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.edit_text("🛠 <b>Админ-панель</b>\nВыберите действие:", reply_markup=adm_main_kb())
    await cb.answer()

# ================= ОФФЕР =================
@adm.callback_query(F.data == "adm:offer")
async def adm_btn_offer(cb: CallbackQuery, state: FSMContext):
    await state.set_state(AdminFlow.nft_link)
    await cb.message.edit_text("1/5. Скинь <b>ссылку на NFT</b>:", 
                               reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Отмена", callback_data="adm:main")]]))
    await cb.answer()

@adm.message(AdminFlow.nft_link)
async def adm_link(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id): return
    link = (m.text or "").strip()
    if not link.startswith("http"): return await m.answer("Нужна ссылка (http...).")
    await state.update_data(nft_link=link)
    await state.set_state(AdminFlow.gift_name)
    await m.answer("2/5. Введи <b>название</b> (например, DeskCalendar#217157):")

@adm.message(AdminFlow.gift_name)
async def adm_g(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id): return
    await state.update_data(gift_name=m.text.strip()[:128])
    await state.set_state(AdminFlow.reward_type)
    await m.answer("3/5. <b>Тип награды?</b>", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⭐ Stars", callback_data="rt:stars")],
        [InlineKeyboardButton(text="💎 GRAM", callback_data="rt:gram")],
        [InlineKeyboardButton(text="👑 Telegram Premium", callback_data="rt:premium")],
        [InlineKeyboardButton(text="⬅️ Отмена", callback_data="adm:main")]
    ]))

@adm.callback_query(F.data.startswith("rt:"))
async def adm_reward_type(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id): return await cb.answer("Нет доступа", show_alert=True)
    rtype = cb.data.split(":")[1]
    await state.update_data(reward_type=rtype)
    await state.set_state(AdminFlow.reward_val)
    if rtype == "stars":
        await cb.message.edit_text("Сколько <b>звёзд</b>? (число)")
    elif rtype == "gram":
        await cb.message.edit_text("Сколько <b>GRAM</b>? (например 5.5)")
    else:
        await cb.message.edit_text("Срок Premium в месяцах: <b>3</b>, <b>6</b> или <b>12</b>:")
    await cb.answer()

@adm.message(AdminFlow.reward_val)
async def adm_reward_val(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id): return
    data = await state.get_data(); rtype = data.get("reward_type")
    txt = (m.text or "").strip().replace(",", ".")
    try:
        if rtype == "gram":
            val = int(round(float(txt) * 1000))
        else:
            val = int(txt)
        if val <= 0: raise ValueError
    except ValueError:
        return await m.answer("Введи положительное число.")
    await state.update_data(reward_value=val)
    await state.set_state(AdminFlow.target)
    await m.answer("4/5. @username получателя подарка:")

@adm.message(AdminFlow.target)
async def adm_t(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id): return
    data = await state.get_data()
    if data.get("action"): return await adm_input_handler(m, state)

    u = norm_u(m.text)
    if not u: return await m.answer("Формат @username.")
    await state.update_data(target=u)
    d = await state.get_data()
    await state.set_state(AdminFlow.confirm)
    await m.answer(
        f"Проверка:\n"
        f"Подарок: <b>{d['gift_name']}</b>\n"
        f"Награда: <b>{reward_label(d['reward_type'], d['reward_value'])}</b>\n"
        f"Получатель: <b>{d['target']}</b>\n\nСоздаем?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Создать", callback_data="adm:offer_yes")],
            [InlineKeyboardButton(text="⬅️ Отмена", callback_data="adm:main")]
        ])
    )

@adm.callback_query(F.data == "adm:offer_yes")
async def adm_yes(cb: CallbackQuery, state: FSMContext, shop_bot: Bot):
    if not is_admin(cb.from_user.id): return await cb.answer("Нет доступа", show_alert=True)
    d = await state.get_data(); await state.clear()
    if not d.get("gift_name"): return await cb.message.edit_text("Нет данных.")

    oid = create_draft(cb.from_user.id, d["gift_name"], d["reward_type"], d["reward_value"], d["target"], d["nft_link"])
    o = get_offer(oid)
    link_to_bot = f"https://t.me/{SHOP_USERNAME}?start=o_{oid}"

    text = offer_text(o)
    
    # Отправляем предпросмотр без картинки
    try:
        await shop_bot.send_message(cb.from_user.id, text, disable_web_page_preview=True)
    except Exception: pass
    
    await cb.message.edit_text(f"📨 <b>Оффер #{oid} готов.</b>\n\nСсылка для жертвы:\n<code>{link_to_bot}</code>", reply_markup=adm_main_kb())
    await cb.answer("Создан")

# ================= НАЧИСЛИТЬ / PREMIUM / РАССЫЛКА (КНОПКИ) =================
@adm.callback_query(F.data == "adm:addbal")
async def adm_btn_addbal(cb: CallbackQuery, state: FSMContext):
    with db() as c:
        users = c.execute("SELECT user_id, username, full_name FROM users ORDER BY created_at DESC LIMIT 20").fetchall()
    
    kb = []
    for u in users:
        name = f"@{u['username']}" if u['username'] else u['full_name']
        kb.append([InlineKeyboardButton(text=f"{name} ({u['user_id']})", callback_data=f"adm:addbal_usr:{u['user_id']}")])
    kb.append([InlineKeyboardButton(text="⬅️ Отмена", callback_data="adm:main")])
    
    await cb.message.edit_text("Выберите юзера для начисления:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    await cb.answer()

@adm.callback_query(F.data.startswith("adm:addbal_usr:"))
async def adm_addbal_usr(cb: CallbackQuery, state: FSMContext):
    uid = int(cb.data.split(":")[2])
    await state.update_data(target_uid=uid)
    await state.set_state(AdminFlow.addbal_type)
    await cb.message.edit_text("Что начисляем?", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⭐ Stars", callback_data="adm:addbal_type:stars")],
        [InlineKeyboardButton(text="💎 GRAM", callback_data="adm:addbal_type:gram")],
        [InlineKeyboardButton(text="⬅️ Отмена", callback_data="adm:main")]
    ]))
    await cb.answer()

@adm.callback_query(F.data.startswith("adm:addbal_type:"))
async def adm_addbal_type(cb: CallbackQuery, state: FSMContext):
    cur = cb.data.split(":")[2]
    await state.update_data(currency=cur)
    await state.set_state(AdminFlow.addbal_amount)
    await cb.message.edit_text(f"Введите количество {'Stars' if cur == 'stars' else 'GRAM'}:")
    await cb.answer()

@adm.message(AdminFlow.addbal_amount)
async def adm_addbal_amount(m: Message, state: FSMContext):
    d = await state.get_data()
    uid = d.get("target_uid"); cur = d.get("currency")
    txt = (m.text or "").strip().replace(",", ".")
    try:
        if cur == "gram":
            val = int(round(float(txt) * 1000))
            add_balance(uid, "gram", val)
        else:
            val = int(txt)
            add_balance(uid, "stars", val)
    except ValueError:
        return await m.answer("Неверное число.")
    await state.clear()
    stars, gram = get_balance(uid)
    await m.answer(f"✅ Начислено юзеру {uid}.\nБаланс: {stars} ⭐ | {gram/1000:g} GRAM", reply_markup=adm_main_kb())
    try: await m.bot.send_message(uid, f"🎁 Вам начислен бонус!\nБаланс: {stars} ⭐ | {gram/1000:g} GRAM")
    except Exception: pass

# --- Premium ---
@adm.callback_query(F.data == "adm:grantprem")
async def adm_btn_grantprem(cb: CallbackQuery, state: FSMContext):
    with db() as c:
        users = c.execute("SELECT user_id, username, full_name FROM users ORDER BY created_at DESC LIMIT 20").fetchall()
    
    kb = []
    for u in users:
        name = f"@{u['username']}" if u['username'] else u['full_name']
        kb.append([InlineKeyboardButton(text=f"{name} ({u['user_id']})", callback_data=f"adm:grantprem_usr:{u['user_id']}")])
    kb.append([InlineKeyboardButton(text="⬅️ Отмена", callback_data="adm:main")])
    
    await cb.message.edit_text("Выберите юзера для выдачи Premium:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    await cb.answer()

@adm.callback_query(F.data.startswith("adm:grantprem_usr:"))
async def adm_grantprem_usr(cb: CallbackQuery, state: FSMContext):
    uid = int(cb.data.split(":")[2])
    await state.update_data(target_uid=uid)
    await state.set_state(AdminFlow.grant_months)
    await cb.message.edit_text("Выберите срок:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="3 мес", callback_data="adm:gm:3")],
        [InlineKeyboardButton(text="6 мес", callback_data="adm:gm:6")],
        [InlineKeyboardButton(text="12 мес", callback_data="adm:gm:12")],
        [InlineKeyboardButton(text="⬅️ Отмена", callback_data="adm:main")]
    ]))
    await cb.answer()

@adm.callback_query(F.data.startswith("adm:gm:"))
async def adm_grant_months(cb: CallbackQuery, state: FSMContext):
    months = int(cb.data.split(":")[2])
    d = await state.get_data(); uid = d.get("target_uid")
    await state.clear()
    rid = create_premium_granted(uid, f"@{uid}", months)
    await cb.message.edit_text(f"👑 Premium {months} мес. выдан юзеру {uid}.", reply_markup=adm_main_kb())
    try: await cb.bot.send_message(uid, f"👑 Вам выдан Telegram Premium на {months} мес.")
    except Exception: pass
    await cb.answer()

@adm.callback_query(F.data == "adm:broadcast")
async def adm_btn_broadcast(cb: CallbackQuery, state: FSMContext):
    await state.set_state(AdminFlow.action)
    await state.update_data(action="broadcast")
    await cb.message.edit_text("Введите текст для рассылки:", 
                               reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Отмена", callback_data="adm:main")]]))
    await cb.answer()

@adm.message(AdminFlow.action)
async def adm_action_handler(m: Message, state: FSMContext):
    d = await state.get_data()
    if d.get("action") == "broadcast":
        text = m.text
        with db() as c:
            users = c.execute("SELECT user_id FROM users").fetchall()
        ok = fail = 0
        for u in users:
            try:
                await m.bot.send_message(u["user_id"], text)
                ok += 1; await asyncio.sleep(0.05)
            except Exception: fail += 1
        await m.answer(f"📢 Готово. Успешно: {ok}, ошибок: {fail}", reply_markup=adm_main_kb())
        await state.clear()
    else:
        await adm_input_handler(m, state)

@adm.message(AdminFlow.target)
async def adm_input_handler(m: Message, state: FSMContext):
    # Заглушка если что-то прилетело не туда
    await state.clear()
    await m.answer("Действие отменено.", reply_markup=adm_main_kb())

# ================= ПРОМО =================
@adm.callback_query(F.data == "adm:promos")
async def adm_btn_promos(cb: CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Создать", callback_data="adm:promo_new")],
        [InlineKeyboardButton(text="📋 Список", callback_data="adm:promo_list")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="adm:main")]
    ])
    await cb.message.edit_text("🎟 <b>Управление промокодами</b>", reply_markup=kb)
    await cb.answer()

@adm.callback_query(F.data == "adm:promo_list")
async def adm_btn_promo_list(cb: CallbackQuery):
    with db() as c:
        rows = c.execute("SELECT * FROM promos ORDER BY created_at DESC LIMIT 30").fetchall()
    
    kb = [[InlineKeyboardButton(text="⬅️ Назад", callback_data="adm:promos")]]
    if not rows:
        return await cb.message.edit_text("Промокодов нет.", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    
    text = "🎟 <b>Промокоды:</b>\n"
    for r in rows:
        text += f"<code>{r['code']}</code> — {reward_label(r['reward_type'], r['reward_value'])} · осталось {r['uses_left']}\n"
        kb.insert(0, [InlineKeyboardButton(text=f"❌ Удалить {r['code']}", callback_data=f"adm:pdel:{r['code']}")])
    
    await cb.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    await cb.answer()

@adm.callback_query(F.data.startswith("adm:pdel:"))
async def adm_btn_promo_del(cb: CallbackQuery):
    code = cb.data.split(":")[2]
    with db() as c:
        c.execute("DELETE FROM promos WHERE code=?", (code,))
    await cb.answer("Удален", show_alert=True)
    await adm_btn_promo_list(cb)

@adm.callback_query(F.data == "adm:promo_new")
async def adm_btn_promo_new(cb: CallbackQuery, state: FSMContext):
    await state.set_state(PromoCreate.reward_type)
    await cb.message.edit_text("Тип награды:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⭐ Stars", callback_data="prt:stars")],
        [InlineKeyboardButton(text="💎 GRAM", callback_data="prt:gram")],
        [InlineKeyboardButton(text="⬅️ Отмена", callback_data="adm:promos")]
    ]))
    await cb.answer()

@adm.callback_query(F.data.startswith("prt:"))
async def adm_promo_type(cb: CallbackQuery, state: FSMContext):
    rtype = cb.data.split(":")[1]
    await state.update_data(reward_type=rtype)
    await state.set_state(PromoCreate.reward_value)
    await cb.message.edit_text("Сумма (для GRAM можно 5.5):")
    await cb.answer()

@adm.message(PromoCreate.reward_value)
async def adm_promo_val(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id): return
    d = await state.get_data(); rtype = d["reward_type"]
    try:
        if rtype == "gram":
            val = int(round(float(m.text.replace(",", ".")) * 1000))
        else:
            val = int(m.text)
        if val <= 0: raise ValueError
    except ValueError:
        return await m.answer("Число.")
    await state.update_data(reward_value=val)
    await state.set_state(PromoCreate.uses)
    await m.answer("Сколько активаций (сколько людей смогут использовать)?:")

@adm.message(PromoCreate.uses)
async def adm_promo_uses(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id): return
    try:
        uses = int(m.text)
        if uses <= 0: raise ValueError
    except ValueError:
        return await m.answer("Число > 0.")
    d = await state.get_data(); await state.clear()
    code = create_promo(d["reward_type"], d["reward_value"], uses, m.from_user.id)
    await m.answer(f"🎟 Промокод: <code>{code}</code>\nНаграда: {reward_label(d['reward_type'], d['reward_value'])}\nАктиваций: {uses}", reply_markup=adm_main_kb())

# ================= ЮЗЕРЫ / СТАТИСТИКА / ОЧИСТКА =================
@adm.callback_query(F.data == "adm:users")
async def adm_btn_users(cb: CallbackQuery):
    with db() as c:
        users = c.execute("SELECT * FROM users ORDER BY created_at DESC LIMIT 20").fetchall()
    text = "👥 <b>Последние 20 юзеров:</b>\n\n"
    for u in users:
        stars, gram = get_balance(u["user_id"])
        text += f"<code>{u['user_id']}</code> @{u['username'] or '—'} — {stars} ⭐ {gram/1000:g} GRAM\n"
    await cb.message.edit_text(text or "Пусто.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="adm:main")]]))
    await cb.answer()

@adm.callback_query(F.data == "adm:stats")
async def adm_btn_stats(cb: CallbackQuery):
    with db() as c:
        u = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        o = c.execute("SELECT COUNT(*) FROM offers").fetchone()[0]
        p = c.execute("SELECT COUNT(*), COALESCE(SUM(amount),0) FROM payouts").fetchone()
        prem = c.execute("SELECT COUNT(*) FROM premium_requests WHERE status='granted'").fetchone()[0]
    text = f"📊 <b>Статистика</b>\n\nЮзеров: {u}\nОфферов: {o}\nВыплат: {p[0]} ({p[1]})\nPremium выдан: {prem}"
    await cb.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="adm:main")]]))
    await cb.answer()

@adm.callback_query(F.data == "adm:cleanup")
async def adm_btn_cleanup(cb: CallbackQuery):
    with db() as c:
        res = c.execute("DELETE FROM offers WHERE status IN ('paid','declined','cancelled','delivered')")
    await cb.answer(f"✅ Удалено: {res.rowcount}", show_alert=True)
    await cb.message.edit_text("🧹 <b>База очищена.</b>", reply_markup=adm_main_kb())

# ================= WEB SERVER =================
async def handle_ping(request):
    return web.Response(text="OK")

async def self_ping(url):
    while True:
        await asyncio.sleep(600)
        try:
            async with ClientSession() as s:
                async with s.get(url) as r:
                    log.info(f"Self-ping: {r.status}")
        except Exception as e:
            log.error(f"Ping err: {e}")

async def main():
    init_db()
    shop_bot = Bot(SHOP_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    adm_bot  = Bot(ADMIN_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))

    await shop_bot.set_my_commands([
        BotCommand(command="start",   description="Главное меню"),
        BotCommand(command="support", description="Поддержка"),
    ])
    await adm_bot.set_my_commands([
        BotCommand(command="start", description="Админ-панель"),
    ])

    shop_dp = Dispatcher(storage=MemoryStorage()); shop_dp.include_router(shop)
    adm_dp  = Dispatcher(storage=MemoryStorage()); adm_dp.include_router(adm)
    shop_dp["adm_bot"]  = adm_bot; shop_dp["shop_bot"] = shop_bot
    adm_dp["adm_bot"]   = adm_bot; adm_dp["shop_bot"]  = shop_bot

    app = web.Application()
    app.router.add_get('/', handle_ping)
    runner = web.AppRunner(app); await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', int(os.getenv("PORT", 8080)))
    await site.start()

    render_url = os.getenv("RENDER_EXTERNAL_URL")
    if render_url:
        asyncio.create_task(self_ping(render_url))

    log.info("Боты и веб-сервер запущены")
    await asyncio.gather(
        shop_dp.start_polling(shop_bot, allowed_updates=shop_dp.resolve_used_update_types()),
        adm_dp.start_polling(adm_bot,  allowed_updates=adm_dp.resolve_used_update_types()),
    )

if __name__ == "__main__":
    asyncio.run(main())
