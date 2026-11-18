import asyncio, logging, os, re, secrets, sqlite3
from datetime import datetime, timedelta
from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
)

SHOP_TOKEN = os.getenv("SHOP_TOKEN", "PASTE_SHOP_TOKEN")
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "PASTE_ADMIN_TOKEN")
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "123").split(",") if x.strip().isdigit()}
SHOP_USERNAME = os.getenv("SHOP_USERNAME", "your_shop_bot")
SUPPORT = os.getenv("SUPPORT", "@your_support")
DB_PATH = os.getenv("DB_PATH", "offers.db")
MIN_WITHDRAW = int(os.getenv("MIN_WITHDRAW", "500"))
PAYOUT_DELAY_SEC = 7
OFFER_TTL_HOURS = 4

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
            user_id INTEGER PRIMARY KEY, username TEXT, full_name TEXT,
            vip_level TEXT DEFAULT 'Bronze', vip_limit INTEGER DEFAULT 500, delivered_count INTEGER DEFAULT 0, created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS offers (
            id INTEGER PRIMARY KEY AUTOINCREMENT, admin_id INTEGER, user_id INTEGER,
            gift_name TEXT, price_stars INTEGER, target_username TEXT,
            status TEXT DEFAULT 'draft', created_at TEXT, expires_at TEXT,
            accepted_at TEXT, delivered_at TEXT, paid_at TEXT, proof TEXT,
            nft_link TEXT
        );
        CREATE TABLE IF NOT EXISTS balances (
            user_id INTEGER PRIMARY KEY, stars INTEGER DEFAULT 0, updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS payouts (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, amount INTEGER,
            username TEXT, txid TEXT, created_at TEXT
        );
        """)

def upsert_user(uid, username, full_name):
    with db() as c:
        if not c.execute("SELECT 1 FROM users WHERE user_id=?", (uid,)).fetchone():
            c.execute("INSERT INTO users(user_id,username,full_name,created_at) VALUES (?,?,?,?)", (uid, username, full_name, now()))
        else:
            c.execute("UPDATE users SET username=COALESCE(?,username), full_name=COALESCE(?,full_name) WHERE user_id=?", (username, full_name, uid))

def get_user(uid):
    with db() as c: return c.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()
def get_balance(uid):
    with db() as c:
        r = c.execute("SELECT stars FROM balances WHERE user_id=?", (uid,)).fetchone()
        return r["stars"] if r else 0
def add_balance(uid, delta):
    with db() as c:
        c.execute("INSERT INTO balances(user_id,stars,updated_at) VALUES (?,?,?) ON CONFLICT(user_id) DO UPDATE SET stars=stars+excluded.stars, updated_at=excluded.updated_at", (uid, delta, now()))
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

# ================= FSM =================
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
    return (f"<b>Пользователь предлагает Вам {o['price_stars']} ⭐ за {o['gift_name']}</b>\n\n"
            f"Предложение действует ещё <b>{time_left}</b>")

def kb_menu(uid):
    bal = get_balance(uid)
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"💰 Баланс: {bal} ⭐", callback_data="m:balance")],
        [InlineKeyboardButton(text="💸 Вывести", callback_data="m:withdraw")],
    ])

# ================= BOTS =================
shop = Router()
adm = Router()

@shop.message(Command("start"))
async def shop_start(m: Message, command: CommandObject):
    payload = (command.args or "").strip()
    upsert_user(m.from_user.id, m.from_user.username, m.from_user.full_name)
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
            try: await m.bot.send_message(o["admin_id"], f"🔔 Оффер #{oid} открыт: @{m.from_user.username or '—'}")
            except Exception: pass
        set_status(oid, "accepted")
        await m.answer(
            f"✅ Оффер принят.\n\nПередайте подарок <b>{o['gift_name']}</b> на "
            f"<b>{o['target_username']}</b> через Telegram → Подарки → Отправить.\n\nПосле передачи нажмите кнопку.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📤 Я передал подарок", callback_data=f"u:delivered:{oid}")]]))
        return
    await m.answer("👋 Добро пожаловать! Здесь вы можете получать предложения и выводить Stars.", reply_markup=kb_menu(m.from_user.id))

@shop.callback_query(F.data.startswith("u:delivered:"))
async def cb_delivered(cb: CallbackQuery, state: FSMContext):
    oid = int(cb.data.split(":")[2]); o = get_offer(oid)
    if not o or o["user_id"] != cb.from_user.id: return await cb.answer("Не ваш", show_alert=True)
    await state.update_data(offer_id=oid)
    await state.set_state(UserFlow.proof)
    await cb.message.answer("📎 Пришлите скриншот/чек передачи подарка.")
    await cb.answer()

@shop.message(UserFlow.proof)
async def receive_proof(m: Message, state: FSMContext):
    d = await state.get_data(); oid = d.get("offer_id"); await state.clear()
    if not oid: return
    o = get_offer(oid)
    if not o or o["user_id"] != m.from_user.id: return await m.answer("Ошибка.")
    proof = m.text or (m.photo[-1].file_id if m.photo else "none")
    set_status(oid, "paid", proof=proof)
    newbal = add_balance(o["user_id"], o["price_stars"])
    await m.answer(f"✅ Подтверждение принято.\n<b>{o['price_stars']} ⭐ Stars</b> зачислены на баланс.\nБаланс: <b>{newbal} ⭐</b>", reply_markup=kb_menu(m.from_user.id))
    try:
        await m.bot.send_message(o["admin_id"], f"📥 Оффер #{oid}: proof от {m.from_user.id}")
        if m.photo: await m.bot.send_photo(o["admin_id"], m.photo[-1].file_id, caption=f"proof #{oid}")
    except Exception: pass

@shop.callback_query(F.data == "m:balance")
async def shop_balance(cb: CallbackQuery):
    bal = get_balance(cb.from_user.id)
    await cb.message.answer(f"💰 <b>Баланс:</b> {bal} ⭐\nМинимум: {MIN_WITHDRAW} ⭐", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💸 Вывести", callback_data="m:withdraw")]]))
    await cb.answer()

@shop.callback_query(F.data == "m:withdraw")
async def wd_start(cb: CallbackQuery, state: FSMContext):
    bal = get_balance(cb.from_user.id)
    if bal < MIN_WITHDRAW: return await cb.message.answer(f"⚠️ Минимум для вывода — <b>{MIN_WITHDRAW} ⭐</b>.\nУ вас: <b>{bal} ⭐</b>.")
    await state.set_state(WdFlow.amount)
    await cb.message.answer(f"💸 <b>Вывод Stars</b>\n\nДоступно: <b>{bal} ⭐</b>\nВведите сумму:")
    await cb.answer()

@shop.message(WdFlow.amount)
async def wd_amount(m: Message, state: FSMContext):
    if not m.text.strip().isdigit(): return await m.answer("Введите число:")
    amt = int(m.text.strip()); bal = get_balance(m.from_user.id)
    if amt < MIN_WITHDRAW or amt > bal: return await m.answer(f"Неверная сумма. Доступно: {bal} ⭐.")
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


@adm.message(Command("offer"))
async def adm_offer(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id): return
    await state.set_state(AdminFlow.nft_link)
    await m.answer("1/4. Скинь <b>ссылку на NFT</b> (например: https://t.me/nft/PlushPepe-112):")

@adm.message(AdminFlow.nft_link)
async def adm_link(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id): return
    link = m.text.strip()
    if not link.startswith("http"): return await m.answer("Нужна ссылка.")
    await state.update_data(nft_link=link)
    await state.set_state(AdminFlow.gift_name)
    await m.answer("2/4. Введи <b>точное название</b> (например, Fine Pen #28,596):")

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
async def adm_yes(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id): return
    d = await state.get_data(); await state.clear()
    if not d.get("gift_name"): return await m.answer("Нет данных.")
    oid = create_draft(m.from_user.id, d["gift_name"], d["price"], d["target"], d["nft_link"])
    o = get_offer(oid)
    link = f"https://t.me/{SHOP_USERNAME}?start=o_{oid}"
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✖️ Отклонить", url=f"https://t.me/{SHOP_USERNAME}"), InlineKeyboardButton(text="✅ Принять", url=link)]])
    await m.answer(offer_text(o) + f"\n\n<a href='{o['nft_link']}'>Посмотреть NFT</a>", reply_markup=kb, disable_web_page_preview=False)
    await m.answer(f"📨 <b>Оффер #{oid} готов.</b>\n\nПерешли карточку выше жертве.")

@adm.message(Command("no"))
async def adm_no(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id): return
    await state.clear(); await m.answer("Отменено.")

# ================= WEB SERVER & PINGER =================
async def handle_ping(request):
    return web.Response(text="OK")

async def self_ping(url):
    import aiohttp
    while True:
        await asyncio.sleep(600) # 10 минут
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as resp:
                    log.info(f"Self-ping status: {resp.status}")
        except Exception as e:
            log.error(f"Ping error: {e}")

async def main():
    init_db()
    shop_bot = Bot(SHOP_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    adm_bot = Bot(ADMIN_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))

    shop_dp = Dispatcher(storage=MemoryStorage())
    shop_dp.include_router(shop)
    adm_dp = Dispatcher(storage=MemoryStorage())
    adm_dp.include_router(adm)

    # Веб-сервер для Render
    app = web.Application()
    app.router.add_get('/', handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', int(os.getenv("PORT", 8080)))
    await site.start()
    
    # Пинг самого себя, чтобы не засыпал
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
