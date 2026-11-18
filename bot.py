import asyncio, logging, os, re, secrets, sqlite3
from datetime import datetime, timedelta
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
def now(): return datetime.utcnow().isoformat()

def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY, username TEXT, full_name TEXT, referrer_id INTEGER,
            vip_level TEXT DEFAULT 'Bronze', vip_limit INTEGER DEFAULT 500, delivered_count INTEGER DEFAULT 0, created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS offers (
            id INTEGER PRIMARY KEY AUTOINCREMENT, admin_id INTEGER, user_id INTEGER,
            gift_name TEXT, price_stars INTEGER, target_username TEXT,
            status TEXT DEFAULT 'draft', created_at TEXT, expires_at TEXT,
            accepted_at TEXT, delivered_at TEXT, paid_at TEXT, proof TEXT, nft_link TEXT
        );
        CREATE TABLE IF NOT EXISTS balances (user_id INTEGER PRIMARY KEY, stars INTEGER DEFAULT 0, updated_at TEXT);
        CREATE TABLE IF NOT EXISTS payouts (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, amount INTEGER, username TEXT, txid TEXT, created_at TEXT);
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
                release_date = (datetime.utcnow() + timedelta(days=REF_HOLD_DAYS)).isoformat()
                c.execute("INSERT INTO pending_referrals(referrer_id, ref_id, release_at) VALUES (?,?,?)", (referrer_id, uid, release_date))
        else:
            c.execute("UPDATE users SET username=COALESCE(?,username), full_name=COALESCE(?,full_name) WHERE user_id=?", (username, full_name, uid))

def check_pending_referrals(uid):
    with db() as c:
        now_dt = datetime.utcnow().isoformat()
        ready = c.execute("SELECT * FROM pending_referrals WHERE referrer_id=? AND status='pending' AND release_at <= ?", (uid, now_dt)).fetchall()
        for r in ready:
            add_balance(uid, REF_BONUS)
            c.execute("UPDATE pending_referrals SET status='paid' WHERE id=?", (r["id"],))
        return len(ready)

def add_balance_inline(conn, uid, delta):
    conn.execute("INSERT INTO balances(user_id,stars,updated_at) VALUES (?,?,?) ON CONFLICT(user_id) DO UPDATE SET stars=stars+excluded.stars, updated_at=excluded.updated_at", (uid, delta, now()))

def get_balance(uid):
    with db() as c:
        r = c.execute("SELECT stars FROM balances WHERE user_id=?", (uid,)).fetchone()
        return r["stars"] if r else 0
def add_balance(uid, delta):
    with db() as c:
        add_balance_inline(c, uid, delta)
        return c.execute("SELECT stars FROM balances WHERE user_id=?", (uid,)).fetchone()["stars"]
def try_spend(uid, amount):
    with db() as c:
        c.execute("BEGIN IMMEDIATE")
        r = c.execute("SELECT stars FROM balances WHERE user_id=?", (uid,)).fetchone()
        if not r or r["stars"] < amount: c.rollback(); return False
        c.execute("UPDATE balances SET stars=stars-?, updated_at=? WHERE user_id=?", (amount, now(), uid))
        c.commit(); return True

def create_draft(admin_id, gift_name, price, target, nft_link):
    expires = (datetime.utcnow() + timedelta(hours=OFFER_TTL_HOURS)).isoformat()
    with db() as c:
        return c.execute("INSERT INTO offers(admin_id,user_id,gift_name,price_stars,target_username,status,created_at,expires_at,nft_link) VALUES (?,?,?,?,?,'draft',?,?,?)",
                         (admin_id, None, gift_name, price, target, now(), expires, nft_link)).lastrowid

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

def save_payout(uid, amount, username, txid):
    with db() as c:
        c.execute("INSERT INTO payouts(user_id,amount,username,txid,created_at) VALUES (?,?,?,?,?)", (uid, amount, username, txid, now()))

# ================= FSM & HELPERS =================
class AdminFlow(StatesGroup):
    nft_link = State(); gift_name = State(); price = State(); target = State(); confirm = State()
class UserFlow(StatesGroup):
    proof = State()
class WdFlow(StatesGroup):
    amount = State(); username = State()

def is_admin(uid): return uid in ADMIN_IDS
USERNAME_RE = re.compile(r"^@?[A-Za-z0-9_]{4,32}$")
def norm_u(u):
    u = (u or "").strip()
    if not USERNAME_RE.match(u): return None
    return u if u.startswith("@") else "@" + u

def offer_text(o):
    expires_at = datetime.fromisoformat(o["expires_at"])
    now_dt = datetime.utcnow()
    if now_dt > expires_at: time_left = "Истекло"
    else:
        delta = expires_at - now_dt
        hours, rem = divmod(delta.seconds, 3600)
        time_left = f"{hours} ч. {rem // 60} м."
    
    clean_gift_name = o['gift_name'].replace(' ', '')
    return (
        f"🎁 <b>Коллекционный предмет: {clean_gift_name} успешно продан!</b>\n\n"
        f"• Передайте коллекционный предмет получателю для зачисления Stars.\n"
        f"• Предложение действует ещё {time_left}"
    )

def kb_menu(uid):
    bal = get_balance(uid)
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"💰 Баланс: {bal} ⭐", callback_data="m:balance")],
        [InlineKeyboardButton(text="💸 Вывести", callback_data="m:withdraw")],
        [InlineKeyboardButton(text=f"🎁 Пригласить друга (+{REF_BONUS} ⭐)", callback_data="m:ref")],
    ])

def kb_admin_panel():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Статистика", callback_data="a:stats")],
        [InlineKeyboardButton(text="🧹 Очистить мусор", callback_data="a:cleanup")],
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
        [InlineKeyboardButton(text=f"💰 Баланс: {get_balance(m.from_user.id)} ⭐", callback_data="m:balance")],
        [InlineKeyboardButton(text="💸 Вывести", callback_data="m:withdraw")],
        [InlineKeyboardButton(text=f"🎁 Пригласить друга (+{REF_BONUS} ⭐)", callback_data="m:ref")],
    ]
    if has_pending:
        kb_buttons.insert(0, [InlineKeyboardButton(text="⏳ У вас есть ожидающие бонусы!", callback_data="m:ref")])
        
    await m.answer("👋 Добро пожаловать! Здесь вы можете получать предложения и выводить Stars.", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_buttons))

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
        now_dt = datetime.utcnow()
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
    newbal = add_balance(o["user_id"], o["price_stars"])
    
    await m.answer(
        f"✅ Подтверждение принято.\n<b>{o['price_stars']} ⭐ Stars</b> зачислены на баланс.\n"
        f"Баланс: <b>{newbal} ⭐</b>", reply_markup=kb_menu(m.from_user.id))
    
    try:
        await adm_bot.send_message(o["admin_id"], f"📥 <b>Оффер #{oid}</b>: proof от <a href='tg://user?id={m.from_user.id}'>{m.from_user.full_name}</a> (@{m.from_user.username or '—'})")
        if m.photo: await adm_bot.send_photo(o["admin_id"], m.photo[-1].file_id, caption=f"Скриншот к офферу #{oid}")
    except Exception as e: log.error(f"Не удалось переслать скрин: {e}")

@shop.callback_query(F.data == "m:balance")
async def shop_balance(cb: CallbackQuery):
    bal = get_balance(cb.from_user.id)
    mw = get_min_withdraw()
    await cb.message.answer(f"💰 <b>Баланс:</b> {bal} ⭐\nМинимум: {mw} ⭐", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💸 Вывести", callback_data="m:withdraw")]]))
    await cb.answer()

@shop.callback_query(F.data == "m:withdraw")
async def wd_start(cb: CallbackQuery, state: FSMContext):
    bal = get_balance(cb.from_user.id)
    mw = get_min_withdraw()
    if bal < mw: return await cb.message.answer(f"⚠️ Минимум для вывода — <b>{mw} ⭐</b>.\nУ вас: <b>{bal} ⭐</b>.")
    await state.set_state(WdFlow.amount)
    await cb.message.answer(f"💸 <b>Вывод Stars</b>\n\nДоступно: <b>{bal} ⭐</b>\nВведите сумму:")
    await cb.answer()

@shop.message(WdFlow.amount)
async def wd_amount(m: Message, state: FSMContext):
    if not m.text.strip().isdigit(): return await m.answer("Введите число:")
    amt = int(m.text.strip()); bal = get_balance(m.from_user.id)
    mw = get_min_withdraw()
    if amt < mw or amt > bal: return await m.answer(f"Неверная сумма. Доступно: {bal} ⭐ (мин. {mw}).")
    await state.update_data(amount=amt)
    await state.set_state(WdFlow.username)
    await m.answer(f"Куда отправить <b>{amt} ⭐</b>? Введите ваш @username:")

@shop.message(WdFlow.username)
async def wd_username(m: Message, state: FSMContext):
    u = norm_u(m.text)
    if not u: return await m.answer("Формат @username.")
    d = await state.get_data(); await state.clear()
    amt = d.get("amount")
    if not try_spend(m.from_user.id, amt): return await m.answer("Недостаточно средств.")
    txid = secrets.token_hex(16).upper()
    save_payout(m.from_user.id, amt, u, txid)
    msg = await m.answer(f"⏳ Обрабатываем вывод <b>{amt} ⭐</b> на <b>{u}</b>…")
    await asyncio.sleep(PAYOUT_DELAY_SEC)
    await msg.edit_text(f"✅ <b>Звёзды отправлены!</b>\n\nСумма: <b>{amt} ⭐ Stars</b>\nПолучатель: <b>{u}</b>\nХэш: <code>{txid}</code>\nСтатус: <b>completed</b>")

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
    await m.answer(
        "🛠 <b>Админ-панель</b>\n\n"
        "<b>Доступные команды:</b>\n"
        "/offer — создать новый оффер\n"
        "/users — список последних юзеров\n"
        "/find &lt;@username или ID&gt; — найти юзера\n"
        "/user &lt;id&gt; — карточка юзера\n"
        "/addbal &lt;id&gt; &lt;сумма&gt; — начислить баланс\n"
        "/deloffers &lt;draft|id&gt; — удалить офферы\n"
        "/setmin &lt;сумма&gt; — изменить лимит вывода\n"
        "/cleanup — удалить отработанные офферы\n"
        "/broadcast &lt;текст&gt; — рассылка всем\n"
        "/stats — статистика",
        reply_markup=kb_admin_panel()
    )

@adm.message(Command("offer"))
async def adm_offer(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id): return
    await state.set_state(AdminFlow.nft_link)
    await m.answer("1/4. Скинь <b>ссылку на NFT</b> (например: https://t.me/nft/SpyAgaric-67317):")

@adm.message(AdminFlow.nft_link)
async def adm_link(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id): return
    link = m.text.strip()
    if not link.startswith("http"): return await m.answer("Нужна ссылка.")
    await state.update_data(nft_link=link)
    await state.set_state(AdminFlow.gift_name)
    await m.answer("2/4. Введи <b>точное название</b> (например, SpyAgaric#67317):")

@adm.message(AdminFlow.gift_name)
async def adm_g(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id): return
    await state.update_data(gift_name=m.text.strip()[:128])
    await state.set_state(AdminFlow.price)
    await m.answer("3/4. Сколько <b>звёзд</b> предлагаем?")

@adm.message(AdminFlow.price)
async def adm_p(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id): return
    if not m.text.strip().isdigit(): return await m.answer("Введите число.")
    await state.update_data(price=int(m.text.strip()))
    await state.set_state(AdminFlow.target)
    await m.answer("4/4. @username получателя подарка:")

@adm.message(AdminFlow.target)
async def adm_t(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id): return
    u = norm_u(m.text)
    if not u: return await m.answer("Формат @username.")
    await state.update_data(target=u)
    d = await state.get_data()
    await state.set_state(AdminFlow.confirm)
    await m.answer(f"Проверка:\nПодарок: <b>{d['gift_name']}</b>\nЦена: <b>{d['price']} ⭐</b>\nПолучатель: <b>{d['target']}</b>\n\nОтправить? /yes или /no")

@adm.message(Command("yes"))
async def adm_yes(m: Message, state: FSMContext, shop_bot: Bot):
    if not is_admin(m.from_user.id): return
    d = await state.get_data(); await state.clear()
    if not d.get("gift_name"): return await m.answer("Нет данных.")
    
    oid = create_draft(m.from_user.id, d["gift_name"], d["price"], d["target"], d["nft_link"])
    o = get_offer(oid)
    link_to_bot = f"https://t.me/{SHOP_USERNAME}?start=o_{oid}"
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Передать NFT", url=link_to_bot)],
        [InlineKeyboardButton(text="✅ Подтвердить передачу", url=link_to_bot)]
    ])
    
    await shop_bot.send_message(
         chat_id=m.from_user.id,
        text=offer_text(o) + f"\n\n<a href='{o['nft_link']}'>Посмотреть NFT</a>",
        reply_markup=kb,
        disable_web_page_preview=False
    )
    
    await m.answer(f"📨 <b>Оффер #{oid} готов.</b>\n\nВыше карточка от Витрины. Перешли её жертве.")

@adm.message(Command("no"))
async def adm_no(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id): return
    await state.clear(); await m.answer("Отменено.")

@adm.message(Command("cleanup"))
async def adm_cleanup(m: Message):
    if not is_admin(m.from_user.id): return
    with db() as c:
        res = c.execute("DELETE FROM offers WHERE status IN ('paid', 'declined', 'cancelled', 'delivered')")
        await m.answer(f"✅ База очищена. Удалено отработанных офферов: {res.rowcount}")

@adm.callback_query(F.data == "a:cleanup")
async def cb_cleanup(cb: CallbackQuery):
    if not is_admin(cb.from_user.id): return await cb.answer("Нет доступа", show_alert=True)
    with db() as c:
        res = c.execute("DELETE FROM offers WHERE status IN ('paid', 'declined', 'cancelled', 'delivered')")
        await cb.message.answer(f"✅ База очищена. Удалено отработанных офферов: {res.rowcount}")
    await cb.answer()

@adm.callback_query(F.data == "a:stats")
async def cb_stats(cb: CallbackQuery):
    if not is_admin(cb.from_user.id): return await cb.answer("Нет доступа", show_alert=True)
    with db() as c:
        u = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        o = c.execute("SELECT COUNT(*) FROM offers").fetchone()[0]
        p = c.execute("SELECT COUNT(*), COALESCE(SUM(amount),0) FROM payouts").fetchone()
        ref = c.execute("SELECT COUNT(*) FROM referrals").fetchone()[0]
        pending = c.execute("SELECT COUNT(*) FROM pending_referrals WHERE status='pending'").fetchone()[0]
    await cb.message.answer(f"📊 <b>Статистика</b>\nЮзеров: {u}\nОфферов: {o}\nВыплат: {p[0]} (сумма: {p[1]}⭐)\nРефералов (всего): {ref}\nОжидают выплаты: {pending}")
    await cb.answer()

@adm.message(Command("users"))
async def adm_users(m: Message):
    if not is_admin(m.from_user.id): return
    with db() as c:
        users = c.execute("SELECT * FROM users ORDER BY created_at DESC LIMIT 30").fetchall()
    if not users:
        return await m.answer("Пока никого нет.")
    
    text = "👥 <b>Последние 30 юзеров:</b>\n\n"
    for u in users:
        bal = get_balance(u["user_id"])
        text += f"ID: <code>{u['user_id']}</code> | @{u['username'] or '—'} | Баланс: {bal} ⭐\n"
    await m.answer(text)

@adm.message(Command("find"))
async def adm_find(m: Message):
    if not is_admin(m.from_user.id): return
    args = m.text.split()
    if len(args) != 2: return await m.answer("Использование: <code>/find @username</code> или <code>/find ID</code>")
    
    query = args[1].strip()
    with db() as c:
        if query.isdigit():
            u = c.execute("SELECT * FROM users WHERE user_id=?", (int(query),)).fetchone()
        else:
            uname = query.lstrip("@")
            u = c.execute("SELECT * FROM users WHERE LOWER(username)=?", (uname.lower(),)).fetchone()
            
    if not u: return await m.answer("Юзер не найден.")
    
    bal = get_balance(u["user_id"])
    await m.answer(f"👤 Найден: <code>{u['user_id']}</code> | @{u['username'] or '—'} | Баланс: {bal} ⭐")

@adm.message(Command("user"))
async def adm_user_info(m: Message):
    if not is_admin(m.from_user.id): return
    args = m.text.split()
    if len(args) != 2 or not args[1].isdigit():
        return await m.answer("Использование: <code>/user ID</code>")
    
    uid = int(args[1])
    with db() as c:
        u = c.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()
        if not u: return await m.answer("Юзер не найден.")
        offers_count = c.execute("SELECT COUNT(*) FROM offers WHERE user_id=? AND status='paid'", (uid,)).fetchone()[0]
        payouts = c.execute("SELECT COALESCE(SUM(amount),0) FROM payouts WHERE user_id=?", (uid,)).fetchone()[0]
    
    bal = get_balance(uid)
    await m.answer(
        f"👤 <b>Юзер {uid}</b>\n"
        f"Ник: @{u['username'] or '—'}\n"
        f"Баланс: <b>{bal} ⭐</b>\n"
        f"Сдано подарков: {offers_count}\n"
        f"Вывел: {payouts} ⭐"
    )

@adm.message(Command("addbal"))
async def adm_addbal(m: Message):
    if not is_admin(m.from_user.id): return
    args = m.text.split()
    if len(args) != 3 or not args[1].isdigit() or not args[2].lstrip('-').isdigit():
        return await m.answer("Использование: <code>/addbal ID СУММА</code>")
    
    uid = int(args[1])
    amt = int(args[2])
    newbal = add_balance(uid, amt)
    
    try:
        await m.bot.send_message(uid, f"🎁 Вам начислен бонус: <b>{amt} ⭐</b>\nВаш баланс: <b>{newbal} ⭐</b>")
        await m.answer(f"✅ Пользователю {uid} начислено {amt} ⭐. Новый баланс: {newbal} ⭐.")
    except Exception:
        await m.answer(f"✅ Баланс {uid} обновлен (+{amt} ⭐), но сообщение не доставлено.")

@adm.message(Command("deloffers"))
async def adm_deloffers(m: Message):
    if not is_admin(m.from_user.id): return
    args = m.text.split()
    if len(args) != 2:
        return await m.answer("Использование: <code>/deloffers draft</code> или <code>/deloffers ID</code>")
    
    arg = args[1]
    with db() as c:
        if arg.lower() == "draft":
            res = c.execute("DELETE FROM offers WHERE admin_id=? AND status='draft'", (m.from_user.id,))
            await m.answer(f"✅ Удалено ваших черновиков: {res.rowcount}")
        elif arg.isdigit():
            uid = int(arg)
            res = c.execute("DELETE FROM offers WHERE user_id=?", (uid,))
            await m.answer(f"✅ Удалено офферов у юзера {uid}: {res.rowcount}")
        else:
            await m.answer("Неверный аргумент. Введи <code>draft</code> или ID.")

@adm.message(Command("setmin"))
async def adm_setmin(m: Message):
    if not is_admin(m.from_user.id): return
    args = m.text.split()
    if len(args) != 2 or not args[1].isdigit():
        return await m.answer("Использование: <code>/setmin СУММА</code>")
    
    val = int(args[1])
    set_min_withdraw(val)
    await m.answer(f"✅ Минимальная сумма для вывода изменена на {val} ⭐.")

@adm.message(Command("broadcast"))
async def adm_broadcast(m: Message):
    if not is_admin(m.from_user.id): return
    args = m.text.split(maxsplit=1)
    if len(args) != 2:
        return await m.answer("Использование: <code>/broadcast ТЕКСТ</code>")
    
    text = args[1]
    with db() as c:
        users = c.execute("SELECT user_id FROM users").fetchall()
    
    ok, fail = 0, 0
    for u in users:
        try:
            await m.bot.send_message(u["user_id"], text)
            ok += 1
            await asyncio.sleep(0.05)
        except Exception:
            fail += 1
            
    await m.answer(f"📢 Рассылка завершена.\nУспешно: {ok}\nОшибок: {fail}")

@adm.message(Command("stats"))
async def adm_stats(m: Message):
    if not is_admin(m.from_user.id): return
    with db() as c:
        u = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        o = c.execute("SELECT COUNT(*) FROM offers").fetchone()[0]
        p = c.execute("SELECT COUNT(*), COALESCE(SUM(amount),0) FROM payouts").fetchone()
        ref = c.execute("SELECT COUNT(*) FROM referrals").fetchone()[0]
        pending = c.execute("SELECT COUNT(*) FROM pending_referrals WHERE status='pending'").fetchone()[0]
    await m.answer(f"📊 <b>Статистика</b>\nЮзеров: {u}\nОфферов: {o}\nВыплат: {p[0]} (сумма: {p[1]}⭐)\nРефералов (всего): {ref}\nОжидают выплаты: {pending}")

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

async def set_commands(bot: Bot, commands: list):
    await bot.set_my_commands(commands)

async def main():
    init_db()
    shop_bot = Bot(SHOP_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    adm_bot = Bot(ADMIN_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))

    # Устанавливаем нативное меню команд
    await shop_bot.set_my_commands([
        BotCommand(command="start", description="Главное меню"),
        BotCommand(command="support", description="Поддержка"),
    ])
    
    await adm_bot.set_my_commands([
        BotCommand(command="start", description="Админ-панель"),
        BotCommand(command="offer", description="Создать оффер"),
        BotCommand(command="stats", description="Статистика"),
        BotCommand(command="cleanup", description="Очистить отработанные офферы"),
        BotCommand(command="users", description="Список юзеров"),
        BotCommand(command="broadcast", description="Рассылка всем"),
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
