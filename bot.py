import asyncio, logging, os, re, secrets, sqlite3
from datetime import datetime, timedelta, timezone
from aiohttp import web, ClientSession
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, BotCommand
)

SHOP_TOKEN = os.getenv("SHOP_TOKEN", "PASTE_SHOP_TOKEN")
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "PASTE_ADMIN_TOKEN")
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "123").split(",") if x.strip().isdigit()}
SHOP_USERNAME = os.getenv("SHOP_USERNAME", "your_shop_bot")
SUPPORT = os.getenv("SUPPORT", "@your_support")
DB_PATH = os.getenv("DB_PATH", "offers.db")

DEFAULT_MIN_WITHDRAW = int(os.getenv("MIN_WITHDRAW", "500"))
PAYOUT_DELAY_SEC = 7
OFFER_TTL_HOURS = 6
REF_BONUS = 500
REF_HOLD_DAYS = 7

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
            vip_level TEXT DEFAULT 'Bronze', vip_limit INTEGER DEFAULT 500, delivered_count INTEGER DEFAULT 0, created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS offers (
            id INTEGER PRIMARY KEY AUTOINCREMENT, admin_id INTEGER, user_id INTEGER,
            gift_name TEXT, price_stars REAL, currency TEXT DEFAULT 'stars', 
            target_username TEXT, status TEXT DEFAULT 'draft', created_at TEXT, expires_at TEXT,
            accepted_at TEXT, delivered_at TEXT, paid_at TEXT, proof TEXT, nft_link TEXT
        );
        CREATE TABLE IF NOT EXISTS balances (
            user_id INTEGER PRIMARY KEY, stars INTEGER DEFAULT 0, gram REAL DEFAULT 0, premium INTEGER DEFAULT 0, updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS payouts (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, amount REAL, currency TEXT, 
            username TEXT, txid TEXT, created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS referrals (id INTEGER PRIMARY KEY AUTOINCREMENT, referrer_id INTEGER, user_id INTEGER, created_at TEXT, UNIQUE(user_id));
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS pending_referrals (
            id INTEGER PRIMARY KEY AUTOINCREMENT, referrer_id INTEGER, ref_id INTEGER, release_at TEXT, status TEXT DEFAULT 'pending'
        );
        """)
        c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('min_withdraw', ?)", (str(DEFAULT_MIN_WITHDRAW),))

def get_min_withdraw():
    with db() as c:
        r = c.execute("SELECT value FROM settings WHERE key='min_withdraw'").fetchone()
        return int(r["value"]) if r else DEFAULT_MIN_WITHDRAW

def set_min_withdraw(val):
    with db() as c:
        c.execute("UPDATE settings SET value=? WHERE key='min_withdraw'", (str(val),))

def upsert_user(uid, username, full_name, referrer_id=None):
    with db() as c:
        if not c.execute("SELECT 1 FROM users WHERE user_id=?", (uid,)).fetchone():
            c.execute("INSERT INTO users(user_id,username,full_name,referrer_id,created_at) VALUES (?,?,?,?,?)", (uid, username, full_name, referrer_id, now()))
            if referrer_id and referrer_id != uid:
                release_date = (datetime.now(timezone.utc) + timedelta(days=REF_HOLD_DAYS)).isoformat()
                c.execute("INSERT INTO pending_referrals(referrer_id, ref_id, release_at) VALUES (?,?,?)", (referrer_id, uid, release_date))
        else:
            c.execute("UPDATE users SET username=COALESCE(?,username), full_name=COALESCE(?,full_name) WHERE user_id=?", (username, full_name, uid))

def check_pending_referrals(uid):
    with db() as c:
        now_dt = datetime.now(timezone.utc).isoformat()
        ready = c.execute("SELECT * FROM pending_referrals WHERE referrer_id=? AND status='pending' AND release_at <= ?", (uid, now_dt)).fetchall()
        for r in ready:
            add_balance(uid, 'stars', REF_BONUS)
            c.execute("UPDATE pending_referrals SET status='paid' WHERE id=?", (r["id"],))
        return len(ready)

def add_balance_inline(conn, uid, currency, delta):
    if currency == 'stars':
        conn.execute("INSERT INTO balances(user_id,stars,updated_at) VALUES (?,?,?) ON CONFLICT(user_id) DO UPDATE SET stars=stars+excluded.stars, updated_at=excluded.updated_at", (uid, delta, now()))
    elif currency == 'gram':
        conn.execute("INSERT INTO balances(user_id,gram,updated_at) VALUES (?,?,?) ON CONFLICT(user_id) DO UPDATE SET gram=gram+excluded.gram, updated_at=excluded.updated_at", (uid, delta, now()))
    elif currency == 'premium':
        conn.execute("INSERT INTO balances(user_id,premium,updated_at) VALUES (?,?,?) ON CONFLICT(user_id) DO UPDATE SET premium=premium+excluded.premium, updated_at=excluded.updated_at", (uid, delta, now()))

def get_balance(uid, currency='stars'):
    with db() as c:
        r = c.execute("SELECT * FROM balances WHERE user_id=?", (uid,)).fetchone()
        if not r: return 0
        return r[currency] if r[currency] else 0

def add_balance(uid, currency, delta):
    with db() as c:
        add_balance_inline(c, uid, currency, delta)
        return get_balance(uid, currency)

def try_spend(uid, currency, amount):
    with db() as c:
        c.execute("BEGIN IMMEDIATE")
        r = c.execute("SELECT * FROM balances WHERE user_id=?", (uid,)).fetchone()
        if not r or (r[currency] or 0) < amount: c.rollback(); return False
        col = 'stars' if currency=='stars' else ('gram' if currency=='gram' else 'premium')
        c.execute(f"UPDATE balances SET {col}={col}-?, updated_at=? WHERE user_id=?", (amount, now(), uid))
        c.commit(); return True

def create_draft(admin_id, gift_name, price, currency, target, nft_link):
    expires = (datetime.now(timezone.utc) + timedelta(hours=OFFER_TTL_HOURS)).isoformat()
    with db() as c:
        return c.execute("INSERT INTO offers(admin_id,user_id,gift_name,price_stars,currency,target_username,status,created_at,expires_at,nft_link) VALUES (?,?,?,?,?,?,'draft',?,?,?)",
                         (admin_id, None, gift_name, price, currency, target, now(), expires, nft_link)).lastrowid

def get_offer(oid):
    with db() as c: return c.execute("SELECT * FROM offers WHERE id=?", (oid,)).fetchone()

def claim_offer(oid, uid):
    with db() as c:
        c.execute("BEGIN IMMEDIATE")
        o = c.execute("SELECT * FROM offers WHERE id=?", (oid,)).fetchone()
        if not o or o["status"] != "draft": c.rollback(); return False
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
        c.execute("INSERT INTO payouts(user_id,amount,currency,username,txid,created_at) VALUES (?,?,?,?,?,?)", (uid, amount, currency, username, txid, now()))

# ================= FSM & HELPERS =================
class AdminFlow(StatesGroup):
    nft_link = State(); gift_name = State(); price = State(); currency = State(); target = State(); confirm = State()
    broadcast = State(); setmin = State(); addbal_id = State(); addbal_amount = State(); addbal_currency = State()

class UserFlow(StatesGroup):
    proof = State()
class WdFlow(StatesGroup):
    currency = State(); amount = State(); username = State()

def is_admin(uid): return uid in ADMIN_IDS
USERNAME_RE = re.compile(r"^@?[A-Za-z0-9_]{4,32}$")
def norm_u(u):
    u = (u or "").strip()
    if not USERNAME_RE.match(u): return None
    return u if u.startswith("@") else "@" + u

def offer_text(o):
    expires_at = datetime.fromisoformat(o["expires_at"])
    now_dt = datetime.now(timezone.utc)
    if now_dt > expires_at: time_left = "Истекло"
    else:
        delta = expires_at - now_dt
        hours, rem = divmod(delta.seconds, 3600)
        time_left = f"{hours} ч. {rem // 60} м."
    
    curr = o['currency']
    price_str = f"{o['price_stars']:g} ⭐" if curr == 'stars' else (f"{o['price_stars']:g} GRAM" if curr == 'gram' else f"{o['price_stars']:g} Premium")
    
    return (
        f"🎁 <b>Пользователь предлагает Вам {price_str} за {o['gift_name']}</b>\n\n"
        f"Предложение действует ещё <b>{time_left}</b>"
    )

def kb_menu(uid):
    bal_stars = get_balance(uid, 'stars')
    bal_gram = get_balance(uid, 'gram')
    bal_prem = get_balance(uid, 'premium')
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"💰 Баланс: {bal_stars:g} ⭐ | {bal_gram:g} GRAM | {bal_prem:g} Prem", callback_data="m:balance")],
        [InlineKeyboardButton(text="💸 Вывести", callback_data="m:withdraw")],
        [InlineKeyboardButton(text=f"🎁 Пригласить друга (+{REF_BONUS} ⭐)", callback_data="m:ref")],
        [InlineKeyboardButton(text="🛟 Поддержка", url=f"https://t.me/{SUPPORT.replace('@','')}")]
    ])

def kb_admin_panel():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎁 Создать оффер", callback_data="a:create_offer")],
        [InlineKeyboardButton(text="👥 Юзеры", callback_data="a:users"), InlineKeyboardButton(text="📊 Статистика", callback_data="a:stats")],
        [InlineKeyboardButton(text="💰 Начислить баланс", callback_data="a:addbal"), InlineKeyboardButton(text="⚙️ Лимит вывода", callback_data="a:setmin")],
        [InlineKeyboardButton(text="📢 Рассылка", callback_data="a:broadcast"), InlineKeyboardButton(text="🧹 Очистить мусор", callback_data="a:cleanup")],
    ])

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

    paid_count = check_pending_referrals(m.from_user.id)
    if paid_count > 0:
        await m.answer(f"🎉 Вам начислен отложенный реферальный бонус: <b>+{paid_count * REF_BONUS} ⭐</b>!")

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
            try: await adm_bot.send_message(o["admin_id"], f"🔔 Оффер #{oid} открыт: @{m.from_user.username or '—'}")
            except Exception: pass
        
        set_status(oid, "accepted")
        await m.answer(
            f"✅ Оффер принят.\n\nПередайте подарок <b>{o['gift_name']}</b> на "
            f"<b>{o['target_username']}</b> через Telegram → Подарки → Отправить.\n\nПосле передачи нажмите кнопку.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📤 Я передал подарок", callback_data=f"u:delivered:{oid}")]]))
        return
    elif ref_id:
        await m.answer(f"🎉 Вы зашли по приглашению! Ваш друг получит +{REF_BONUS} ⭐ через {REF_HOLD_DAYS} дней.")
        
    with db() as c:
        has_pending = c.execute("SELECT 1 FROM pending_referrals WHERE referrer_id=? AND status='pending' LIMIT 1", (m.from_user.id,)).fetchone()
    
    kb_buttons = [
        [InlineKeyboardButton(text=f"💰 Баланс: {get_balance(m.from_user.id, 'stars')} ⭐", callback_data="m:balance")],
        [InlineKeyboardButton(text="💸 Вывести", callback_data="m:withdraw")],
        [InlineKeyboardButton(text=f"🎁 Пригласить друга (+{REF_BONUS} ⭐)", callback_data="m:ref")],
        [InlineKeyboardButton(text="🛟 Поддержка", url=f"https://t.me/{SUPPORT.replace('@','')}")]
    ]
    if has_pending:
        kb_buttons.insert(0, [InlineKeyboardButton(text="⏳ У вас есть ожидающие бонусы!", callback_data="m:ref")])
        
    await m.answer("👋 Добро пожаловать! Здесь вы можете получать предложения и выводить средства.", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_buttons))

@shop.callback_query(F.data == "m:ref")
async def cb_ref(cb: CallbackQuery):
    me = await cb.bot.get_me()
    link = f"https://t.me/{me.username}?start=ref_{cb.from_user.id}"
    
    with db() as c:
        refs = c.execute("""
            SELECT pr.*, u.username, u.full_name 
            FROM pending_referrals pr
            LEFT JOIN users u ON u.user_id = pr.ref_id
            WHERE pr.referrer_id = ? AND pr.status = 'pending'
            ORDER BY pr.release_at ASC
        """, (cb.from_user.id,)).fetchall()
    
    text = f"🎁 <b>Пригласи друга</b>\n\n"
    text += f"За каждого друга, который зайдет по вашей ссылке, вы получите <b>+{REF_BONUS} ⭐</b>.\n"
    text += f"<i>Бонус начисляется через {REF_HOLD_DAYS} дней после приглашения.</i>\n\n"
    
    if refs:
        text += "👥 <b>Ваши друзья в ожидании:</b>\n"
        now_dt = datetime.now(timezone.utc)
        for r in refs:
            release_at = datetime.fromisoformat(r["release_at"])
            delta = release_at - now_dt
            days_left = max(0, delta.days)
            hours_left = max(0, delta.seconds // 3600)
            name = f"@{r['username']}" if r['username'] else r['full_name']
            text += f"• {name} — осталось {days_left} д. {hours_left} ч.\n"
    else:
        text += "У вас пока нет приглашенных друзей в ожидании.\n\n"
        
    text += f"\n🔗 Ваша ссылка:\n<code>{link}</code>"
    
    await cb.message.answer(text)
    await cb.answer()

@shop.callback_query(F.data.startswith("u:delivered:"))
async def cb_delivered(cb: CallbackQuery, state: FSMContext):
    oid = int(cb.data.split(":")[2]); o = get_offer(oid)
    if not o or o["user_id"] != cb.from_user.id: return await cb.answer("Не ваш", show_alert=True)
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
    curr = o['currency']
    newbal = add_balance(o["user_id"], curr, o["price_stars"])
    
    await m.answer(
        f"✅ Подтверждение принято.\n<b>{o['price_stars']:g} {curr.upper()}</b> зачислены на баланс.\n"
        f"Баланс: <b>{newbal:g} {curr.upper()}</b>", reply_markup=kb_menu(m.from_user.id))
    
    try:
        await adm_bot.send_message(o["admin_id"], f"📥 <b>Оффер #{oid}</b>: proof от <a href='tg://user?id={m.from_user.id}'>{m.from_user.full_name}</a> (@{m.from_user.username or '—'})")
        if m.photo: await adm_bot.send_photo(o["admin_id"], m.photo[-1].file_id, caption=f"Скриншот к офферу #{oid}")
    except Exception as e: log.error(f"Не удалось переслать скрин: {e}")

@shop.callback_query(F.data == "m:balance")
async def shop_balance(cb: CallbackQuery):
    bal_s = get_balance(cb.from_user.id, 'stars')
    bal_g = get_balance(cb.from_user.id, 'gram')
    bal_p = get_balance(cb.from_user.id, 'premium')
    mw = get_min_withdraw()
    await cb.message.answer(
        f"💰 <b>Ваш баланс:</b>\n"
        f"⭐ Stars: {bal_s:g} (мин. {mw})\n"
        f"💎 GRAM: {bal_g:g}\n"
        f"🌟 Premium: {bal_p:g} мес.\n", 
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💸 Вывести", callback_data="m:withdraw")]]))
    await cb.answer()

@shop.callback_query(F.data == "m:withdraw")
async def wd_start(cb: CallbackQuery, state: FSMContext):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⭐ Stars", callback_data="wd:curr:stars"), InlineKeyboardButton(text="💎 GRAM", callback_data="wd:curr:gram")],
        [InlineKeyboardButton(text="🌟 Premium", callback_data="wd:curr:premium")]
    ])
    await cb.message.answer("Что хотите вывести?", reply_markup=kb)
    await cb.answer()

@shop.callback_query(F.data.startswith("wd:curr:"))
async def wd_currency(cb: CallbackQuery, state: FSMContext):
    curr = cb.data.split(":")[2]
    await state.update_data(currency=curr)
    bal = get_balance(cb.from_user.id, curr)
    mw = get_min_withdraw()
    
    if curr == 'stars' and bal < mw: 
        return await cb.message.answer(f"⚠️ Минимум для вывода Stars — <b>{mw} ⭐</b>. У вас: <b>{bal:g}</b>.")
    if bal <= 0:
        return await cb.message.answer("У вас нет средств на этом балансе.")
        
    await state.set_state(WdFlow.amount)
    await cb.message.answer(f"💸 <b>Вывод {curr.upper()}</b>\n\nДоступно: <b>{bal:g}</b>\nВведите сумму:")
    await cb.answer()

@shop.message(WdFlow.amount)
async def wd_amount(m: Message, state: FSMContext):
    d = await state.get_data()
    curr = d.get('currency')
    if not m.text.replace('.', '', 1).isdigit(): return await m.answer("Введите число:")
    
    amt = float(m.text.strip()) if curr in ['gram', 'stars'] else int(m.text.strip())
    bal = get_balance(m.from_user.id, curr)
    mw = get_min_withdraw()
    
    if curr == 'stars' and amt < mw: return await m.answer(f"Минимум {mw} ⭐.")
    if amt > bal: return await m.answer(f"У вас только {bal:g} {curr.upper()}.")
    
    await state.update_data(amount=amt)
    await state.set_state(WdFlow.username)
    await m.answer(f"Куда отправить <b>{amt:g} {curr.upper()}</b>? Введите ваш @username:")

@shop.message(WdFlow.username)
async def wd_username(m: Message, state: FSMContext):
    u = norm_u(m.text)
    if not u: return await m.answer("Формат @username.")
    d = await state.get_data(); await state.clear()
    amt = d.get("amount")
    curr = d.get("currency")
    
    if not try_spend(m.from_user.id, curr, amt): return await m.answer("Недостаточно средств.")
    txid = secrets.token_hex(16).upper()
    save_payout(m.from_user.id, amt, curr, u, txid)
    msg = await m.answer(f"⏳ Обрабатываем вывод <b>{amt:g} {curr.upper()}</b> на <b>{u}</b>…")
    await asyncio.sleep(PAYOUT_DELAY_SEC)
    await msg.edit_text(f"✅ <b>Средства отправлены!</b>\n\nСумма: <b>{amt:g} {curr.upper()}</b>\nПолучатель: <b>{u}</b>\nХэш: <code>{txid}</code>\nСтатус: <b>completed</b>")

@shop.message(Command("support"))
async def shop_support(m: Message):
    await m.answer(f"🛟 Поддержка: {SUPPORT}")

@shop.message()
async def shop_autoreg(m: Message):
    upsert_user(m.from_user.id, m.from_user.username, m.from_user.full_name)

# ================= ADMIN BOT =================
adm = Router()

@adm.message(Command("start"))
async def adm_start(m: Message):
    if not is_admin(m.from_user.id): return
    async def adm_create_offer(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id): return
    await cb.message.edit_text("1/5. Скинь <b>ссылку на NFT</b>:")
    await state.set_state(AdminFlow.nft_link)
    await cb.answer()

@adm.message(AdminFlow.nft_link)
async def adm_link(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id): return
    await state.update_data(nft_link=m.text.strip())
    await state.set_state(AdminFlow.gift_name)
    await m.answer("2/5. Введи <b>точное название</b> (например, Fine Pen #28,596):")

@adm.message(AdminFlow.gift_name)
async def adm_g(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id): return
    await state.update_data(gift_name=m.text.strip()[:128])
    await state.set_state(AdminFlow.price)
    await m.answer("3/5. Сколько предлагаем?")

@adm.message(AdminFlow.price)
async def adm_p(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id): return
    if not m.text.replace('.', '', 1).isdigit(): return await m.answer("Введите число.")
    await state.update_data(price=float(m.text.strip()) if '.' in m.text else int(m.text.strip()))
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⭐ Stars", callback_data="a:curr:stars"), InlineKeyboardButton(text="💎 GRAM", callback_data="a:curr:gram")],
        [InlineKeyboardButton(text="🌟 Premium", callback_data="a:curr:premium")]
    ])
    await state.set_state(AdminFlow.currency)
    await m.answer("4/5. В какой валюте?", reply_markup=kb)

@adm.callback_query(AdminFlow.currency, F.data.startswith("a:curr:"))
async def adm_currency(cb: CallbackQuery, state: FSMContext):
    curr = cb.data.split(":")[2]
    await state.update_data(currency=curr)
    await state.set_state(AdminFlow.target)
    await cb.message.edit_text("5/5. @username получателя подарка:")
    await cb.answer()

@adm.message(AdminFlow.target)
async def adm_t(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id): return
    u = norm_u(m.text)
    if not u: return await m.answer("Формат @username.")
    await state.update_data(target=u)
    d = await state.get_data()
    await state.set_state(AdminFlow.confirm)
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Отправить", callback_data="a:confirm_yes"), InlineKeyboardButton(text="❌ Отмена", callback_data="a:confirm_no")]
    ])
    await m.answer(f"Проверка:\nПодарок: <b>{d['gift_name']}</b>\nЦена: <b>{d['price']:g} {d['currency'].upper()}</b>\nПолучатель: <b>{d['target']}</b>\n\nВсе верно?", reply_markup=kb)

@adm.callback_query(F.data == "a:confirm_yes")
async def adm_confirm_yes(cb: CallbackQuery, state: FSMContext, shop_bot: Bot):
    if not is_admin(cb.from_user.id): return
    d = await state.get_data(); await state.clear()
    if not d.get("gift_name"): return await cb.message.answer("Нет данных.")
    
    oid = create_draft(cb.from_user.id, d["gift_name"], d["price"], d["currency"], d["target"], d["nft_link"])
    o = get_offer(oid)
    link_to_bot = f"https://t.me/{SHOP_USERNAME}?start=o_{oid}"
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Передать NFT", url=link_to_bot)],
        [InlineKeyboardButton(text="✅ Подтвердить передачу", url=link_to_bot)]
    ])
    
    # Отправляем от лица Витрины
    await shop_bot.send_message(
        chat_id=cb.from_user.id,
        text=offer_text(o) + f"\n\n<a href='{o['nft_link']}'>Посмотреть NFT</a>",
        reply_markup=kb,
        disable_web_page_preview=False
    )
    await cb.message.edit_text(f"📨 <b>Оффер #{oid} готов!</b>\nКарточка выше. Перешли её жертве.")

@adm.callback_query(F.data == "a:confirm_no")
async def adm_confirm_no(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.edit_text("Отменено.", reply_markup=kb_admin_panel())

# --- Кнопки управления ---
@adm.callback_query(F.data == "a:stats")
async def cb_stats(cb: CallbackQuery):
    if not is_admin(cb.from_user.id): return await cb.answer("Нет доступа", show_alert=True)
    with db() as c:
        u = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        o = c.execute("SELECT COUNT(*) FROM offers").fetchone()[0]
        p = c.execute("SELECT COUNT(*), COALESCE(SUM(amount),0) FROM payouts").fetchone()
        ref = c.execute("SELECT COUNT(*) FROM referrals").fetchone()[0]
        pending = c.execute("SELECT COUNT(*) FROM pending_referrals WHERE status='pending'").fetchone()[0]
    await cb.message.answer(f"📊 <b>Статистика</b>\nЮзеров: {u}\nОфферов: {o}\nВыплат: {p[0]} (сумма: {p[1]:g})\nРефералов: {ref}\nОжидают: {pending}")
    await cb.answer()

@adm.callback_query(F.data == "a:cleanup")
async def cb_cleanup(cb: CallbackQuery):
    if not is_admin(cb.from_user.id): return await cb.answer("Нет доступа", show_alert=True)
    with db() as c:
        res = c.execute("DELETE FROM offers WHERE status IN ('paid', 'declined', 'cancelled', 'delivered')")
        await cb.message.answer(f"✅ База очищена. Удалено отработанных офферов: {res.rowcount}")
    await cb.answer()

@adm.callback_query(F.data == "a:setmin")
async def cb_setmin_start(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id): return
    await state.set_state(AdminFlow.setmin)
    await cb.message.answer(f"Текущий лимит вывода: {get_min_withdraw()} ⭐\nВведите новое значение:")
    await cb.answer()

@adm.message(AdminFlow.setmin)
async def cb_setmin_end(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id): return
    if not m.text.isdigit(): return await m.answer("Введите число.")
    set_min_withdraw(int(m.text))
    await state.clear()
    await m.answer(f"✅ Лимит обновлен: {m.text} ⭐", reply_markup=kb_admin_panel())

@adm.callback_query(F.data == "a:broadcast")
async def cb_broadcast_start(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id): return
    await state.set_state(AdminFlow.broadcast)
    await cb.message.answer("Введите текст для рассылки:")
    await cb.answer()

@adm.message(AdminFlow.broadcast)
async def cb_broadcast_end(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id): return
    await state.clear()
    with db() as c:
        users = c.execute("SELECT user_id FROM users").fetchall()
    
    ok, fail = 0, 0
    msg = await m.answer("⏳ Рассылка началась...")
    for u in users:
        try:
            await m.bot.send_message(u["user_id"], m.text)
            ok += 1
            await asyncio.sleep(0.05)
        except Exception:
            fail += 1
    await msg.edit_text(f"📢 Рассылка завершена.\nУспешно: {ok}\nОшибок: {fail}", reply_markup=kb_admin_panel())

@adm.callback_query(F.data == "a:addbal")
async def cb_addbal_start(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id): return
    await state.set_state(AdminFlow.addbal_id)
    await cb.message.answer("Введите <b>ID пользователя</b>, которому нужно начислить:")
    await cb.answer()

@adm.message(AdminFlow.addbal_id)
async def cb_addbal_id(m: Message, state: FSMContext):
    if not m.text.isdigit(): return await m.answer("ID должен быть числом.")
    await state.update_data(uid=int(m.text))
    await state.set_state(AdminFlow.addbal_currency)
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⭐ Stars", callback_data="a:addcurr:stars"), InlineKeyboardButton(text="💎 GRAM", callback_data="a:addcurr:gram")],
        [InlineKeyboardButton(text="🌟 Premium", callback_data="a:addcurr:premium")]
    ])
    await m.answer("Выберите валюту:", reply_markup=kb)

@adm.callback_query(AdminFlow.addbal_currency, F.data.startswith("a:addcurr:"))
async def cb_addbal_currency(cb: CallbackQuery, state: FSMContext):
    curr = cb.data.split(":")[2]
    await state.update_data(currency=curr)
    await state.set_state(AdminFlow.addbal_amount)
    await cb.message.edit_text(f"Введите сумму для начисления ({curr.upper()}):")
    await cb.answer()

@adm.message(AdminFlow.addbal_amount)
async def cb_addbal_amount(m: Message, state: FSMContext):
    if not m.text.replace('.', '', 1).isdigit(): return await m.answer("Введите число.")
    d = await state.get_data(); await state.clear()
    amt = float(m.text) if d['currency'] in ['gram', 'stars'] else int(m.text)
    
    newbal = add_balance(d['uid'], d['currency'], amt)
    await m.answer(f"✅ Пользователю {d['uid']} начислено {amt:g} {d['currency'].upper()}.\nБаланс: {newbal:g}", reply_markup=kb_admin_panel())
    
    try:
        await m.bot.send_message(d['uid'], f"🎁 Вам начислен бонус: <b>{amt:g} {d['currency'].upper()}</b>\nВаш баланс: <b>{newbal:g}</b>")
    except: pass

@adm.callback_query(F.data == "a:users")
async def cb_users(cb: CallbackQuery):
    if not is_admin(cb.from_user.id): return await cb.answer("Нет доступа", show_alert=True)
    with db() as c:
        users = c.execute("SELECT * FROM users ORDER BY created_at DESC LIMIT 15").fetchall()
    
    text = "👥 <b>Последние 15 юзеров:</b>\n\n"
    for u in users:
        s = get_balance(u["user_id"], 'stars')
        g = get_balance(u["user_id"], 'gram')
        p = get_balance(u["user_id"], 'premium')
        text += f"ID: <code>{u['user_id']}</code> | @{u['username'] or '—'}\n└ {s:g}⭐ | {g:g}GRAM | {p}Prem\n\n"
    await cb.message.answer(text)
    await cb.answer()

# ================= WEB SERVER & PINGER =================
async def handle_ping(request):
    return web.Response(text="OK")

async def self_ping(url):
    while True:
        await asyncio.sleep(600)
        try:
            async with ClientSession() as session:
                async with session.get(url) as resp:
                    log.info(f"Self-ping status: {resp.status}")
        except Exception as e:
            log.error(f"Ping error: {e}")

async def main():
    init_db()
    shop_bot = Bot(SHOP_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    adm_bot = Bot(ADMIN_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))

    await shop_bot.set_my_commands([
        BotCommand(command="start", description="Главное меню"),
        BotCommand(command="support", description="Поддержка"),
    ])
    
    await adm_bot.set_my_commands([
        BotCommand(command="start", description="Админ-панель"),
    ])

    shop_dp = Dispatcher(storage=MemoryStorage())
    shop_dp.include_router(shop)
    adm_dp = Dispatcher(storage=MemoryStorage())
    adm_dp.include_router(adm)

    shop_dp["adm_bot"] = adm_bot
    adm_dp["adm_bot"] = adm_bot
    shop_dp["shop_bot"] = shop_bot
    adm_dp["shop_bot"] = shop_bot

    app = web.Application()
    app.router.add_get('/', handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', int(os.getenv("PORT", 8080)))
    await site.start()
    
    render_url = os.getenv("RENDER_EXTERNAL_URL")
    if render_url:
        asyncio.create_task(self_ping(render_url))

    log.info("Боты и веб-сервер запущены")
    await asyncio.gather(
        shop_dp.start_polling(shop_bot, allowed_updates=shop_dp.resolve_used_update_types()),
        adm_dp.start_polling(adm_bot, allowed_updates=adm_dp.resolve_used_update_types()),
    )

if __name__ == "__main__":
    asyncio.run(main())
