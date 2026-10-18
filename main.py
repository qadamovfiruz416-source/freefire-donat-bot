import asyncio
import os
import sqlite3
import time
import types

from telegram import Update, InlineKeyboardButton as Btn, InlineKeyboardMarkup as Kb
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters,
)

import donatix

# ===== ИНҲОРО ТАҒЙИР ДИҲЕД =====

TOKEN = os.environ["TOKEN"]
ADMIN_ID = int(os.environ["ADMIN_ID"])
CARD_NUMBER = os.environ["CARD_NUMBER"]
CARD_NAME = "DC Next"

SUPPORT_TEXT = "Барои кумак ба админ нависед: @USERNAME"
MIN_TOPUP = 5
AUTO_DONATE = True
PACKS = {
    "a100": ("💎 110 алмос", 9.50),
    "a310": ("💎 341 алмос", 30),
    "a520": ("💎 572 алмос", 50),
    "v0": ("🎟 Вачер Лайт", 5),
    "v1": ("🎟 Вачери ҳафтагӣ", 18),
    "v2": ("🎟 Вачери моҳона", 75),
}
# =================================

DB_FILE = os.environ.get("DB_PATH") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "donat.db")
STATUS = {"wait_donat": "⏳ интизор", "done": "✅ анҷом", "canceled": "❌ бекор", "donating": "🔄 дар кор"}

db = sqlite3.connect(DB_FILE, check_same_thread=False)
db.row_factory = sqlite3.Row
db.executescript(
    """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY, name TEXT, username TEXT,
    balance INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS topups (
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER,
    amount INTEGER, status TEXT, created INTEGER);
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER,
    pack TEXT, price INTEGER, gid TEXT, status TEXT, created INTEGER);
"""
)
db.commit()


for _col in ("pack_key TEXT", "donatix_id TEXT", "attempt INTEGER NOT NULL DEFAULT 0"):
    try:
        db.execute("ALTER TABLE orders ADD COLUMN " + _col)
    except sqlite3.OperationalError:
        pass
db.commit()


def upsert_user(u):
    db.execute(
        "INSERT INTO users(id, name, username) VALUES(?,?,?) "
        "ON CONFLICT(id) DO UPDATE SET name=excluded.name, username=excluded.username",
        (u.id, u.full_name, u.username or ""),
    )
    db.commit()


def balance(uid):
    r = db.execute("SELECT balance FROM users WHERE id=?", (uid,)).fetchone()
    return r["balance"] if r else 0


def user_label(uid):
    r = db.execute("SELECT name, username FROM users WHERE id=?", (uid,)).fetchone()
    if not r:
        return str(uid)
    un = f"@{r['username']}" if r["username"] else "ном надорад"
    return f"{r['name']} ({un})"


def menu_kb():
    return Kb([
        [Btn("🎮 Бозиҳо", callback_data="games")],
        [Btn("💰 Баланс", callback_data="bal"),
         Btn("📦 Фармоишҳои ман", callback_data="orders")],
        [Btn("🆘 Дастгирӣ", callback_data="support")],
    ])


def back_kb(rows=None):
    return Kb((rows or []) + [[Btn("⬅️ Меню", callback_data="menu")]])


def menu_text(uid, name):
    return (
        f"Салом, {name}! Хуш омадед ба Free Fire Donat TJ 🎮\n\n"
        "Донат ба Free Fire — зуд ва бо нархи ҳалол.\n\n"
        f"💰 Баланс: {balance(uid)} сомонӣ"
    )


async def safe_answer(q):
    try:
        await q.answer()
    except Exception:
        pass


async def edit(q, text, kb=None):
    try:
        await q.edit_message_text(text, reply_markup=kb)
    except Exception:
        pass


async def mark(q, text):
    try:
        if q.message.photo or q.message.document:
            await q.edit_message_caption(caption=text)
        else:
            await q.edit_message_text(text)
    except Exception:
        pass


async def tell(context, uid, text, kb=None):
    try:
        await context.bot.send_message(uid, text, reply_markup=kb)
    except Exception:
        pass


async def do_donation(order):
    raise NotImplementedError("API нест")


_tasks = set()


def spawn(coro):
    t = asyncio.create_task(coro)
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)


async def send_admin_manual(context, oid, reason=None):
    o = db.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    text = (
        f"💎 Фармоиши #{oid}: донат лозим аст\n"
        f"Мизоҷ: {user_label(o['user_id'])}\n"
        f"Баста: {o['pack']} — {o['price']} сомонӣ\n"
        f"🆔 Free Fire ID: {o['gid']}\n"
    )
    if reason:
        text += f"\n⚠️ Худкор нашуд: {reason}\n"
    text += "\nДонатро дастӣ анҷом диҳед ва тугмаро пахш кунед."
    rows = [[Btn("✅ Донат анҷом шуд", callback_data=f"done:{oid}")]]
    if AUTO_DONATE and donatix.KEY:
        rows.append([Btn("🔁 Аз нав кӯшиш (худкор)", callback_data=f"retry:{oid}")])
    rows.append([Btn("❌ Бекор кардан (пул бармегардад)", callback_data=f"cancel:{oid}")])
    await context.bot.send_message(ADMIN_ID, text, reply_markup=Kb(rows))


async def place_order(context, u, key, gid, reply):
    name, price = PACKS[key]
    cur = db.execute(
        "UPDATE users SET balance=balance-? WHERE id=? AND balance>=?",
        (price, u.id, price),
    )
    if cur.rowcount != 1:
        db.rollback()
        context.user_data.clear()
        await reply("❌ Баланс кофӣ нест.", reply_markup=menu_kb())
        return
    cur = db.execute(
        "INSERT INTO orders(user_id, pack, price, gid, status, created, pack_key) "
        "VALUES(?,?,?,?,?,?,?)",
        (u.id, name, price, gid, "wait_donat", int(time.time()), key),
    )
    oid = cur.lastrowid
    db.commit()
    context.user_data.clear()
    await reply(
        f"✅ Фармоиши #{oid} қабул шуд.\n"
        f"{name} → 🆔 {gid}\n"
        f"Аз баланс: -{price} сомонӣ\n"
        f"💰 Баланс: {balance(u.id)} сомонӣ\n\n"
        "Донат дар чанд дақиқа анҷом мешавад.",
        reply_markup=menu_kb(),
    )
    await process_order(context, oid)


async def process_order(context, oid):
    if AUTO_DONATE and donatix.KEY:
        cur = db.execute(
            "UPDATE orders SET status='donating' WHERE id=? AND status='wait_donat'",
            (oid,),
        )
        db.commit()
        if cur.rowcount == 1:
            spawn(run_auto(context, oid))
            return
    await send_admin_manual(context, oid)


async def run_auto(context, oid):
    try:
        kind, info = await donatix.auto_donate(db, oid)
    except Exception as e:
        kind, info = "manual", f"хатои дохилӣ: {e}"
    o = db.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    if kind == "done":
        cur = db.execute(
            "UPDATE orders SET status='done' WHERE id=? AND status='donating'", (oid,)
        )
        db.commit()
        if cur.rowcount == 1:
            await notify_done(context, oid)
    elif kind == "player":
        cur = db.execute(
            "UPDATE orders SET status='canceled' WHERE id=? AND status='donating'",
            (oid,),
        )
        if cur.rowcount == 1:
            db.execute(
                "UPDATE users SET balance=balance+? WHERE id=?",
                (o["price"], o["user_id"]),
            )
        db.commit()
        if cur.rowcount == 1:
            await tell(
                context, o["user_id"],
                f"❌ ID {o['gid']} дар Free Fire ёфт нашуд. Пул ба баланси шумо баргашт.\n"
                "ID-ро санҷед ва аз нав кӯшиш кунед.",
                menu_kb(),
            )
            await context.bot.send_message(
                ADMIN_ID,
                f"ℹ️ Фармоиши #{oid} бекор шуд: ID нодуруст ({o['gid']}). Пул ба баланс баргашт.",
            )
    else:
        cur = db.execute(
            "UPDATE orders SET status='wait_donat' WHERE id=? AND status='donating'",
            (oid,),
        )
        db.commit()
        if cur.rowcount == 1:
            await send_admin_manual(context, oid, info)
            await tell(
                context, o["user_id"],
                "⏳ Фармоиши шумо дар коркард аст. Админ ба зудӣ анҷом медиҳад.",
            )


async def notify_done(context, oid):
    o = db.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    total = db.execute(
        "SELECT COALESCE(SUM(price),0) AS s FROM orders WHERE user_id=? AND status='done'",
        (o["user_id"],),
    ).fetchone()["s"]
    await tell(
        context, o["user_id"],
        f"🎉 Донат анҷом ёфт! {o['pack']} ба ID {o['gid']} фиристода шуд.\n"
        "Бозии хуш!",
        menu_kb(),
    )
    await context.bot.send_message(
        ADMIN_ID,
        f"📊 Фармоиши #{oid} анҷом ёфт\n"
        f"Мизоҷ: {user_label(o['user_id'])}\n"
        f"🆔 ID: {o['gid']}\n"
        f"💎 Донат: {o['pack']}\n"
        f"💰 Нарх: {o['price']} сомонӣ\n"
        f"Ҷамъи харидҳои ин мизоҷ: {total} сомонӣ",
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    upsert_user(u)
    context.user_data.clear()
    await update.message.reply_text(
        menu_text(u.id, u.first_name), reply_markup=menu_kb()
    )


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await safe_answer(q)
    u = q.from_user
    upsert_user(u)
    d = q.data

    if d == "menu":
        context.user_data.clear()
        await edit(q, menu_text(u.id, u.first_name), menu_kb())

    elif d == "games":
        context.user_data.clear()
        await edit(q, "Бозиро интихоб кунед:", Kb([
            [Btn("🔥 Free Fire", callback_data="ff")],
            [Btn("⬅️ Меню", callback_data="menu")],
        ]))

    elif d == "ff":
        context.user_data.clear()
        rows = [
            [Btn(f"{n} — {p} сомонӣ", callback_data=f"p:{k}")]
            for k, (n, p) in PACKS.items()
        ]
        rows.append([Btn("⬅️ Бозгашт", callback_data="games")])
        await edit(
            q,
            f"🔥 Free Fire\n\n💰 Баланси шумо: {balance(u.id)} сомонӣ\n\nБастаро интихоб кунед:",
            Kb(rows),
        )

    elif d.startswith("p:"):
        key = d[2:]
        if key not in PACKS:
            return
        name, price = PACKS[key]
        bal = balance(u.id)
        if bal < price:
            need = price - bal
            context.user_data.clear()
            context.user_data["suggest"] = need
            await edit(
                q,
                f"{name} — {price} сомонӣ\n\n❌ Баланс кофӣ нест.\n"
                f"💰 Баланси шумо: {bal} сомонӣ\n"
                f"Намерасад: {need} сомонӣ\n\nАввал балансро пур кунед.",
                Kb([
                    [Btn("➕ Пур кардани баланс", callback_data="topup")],
                    [Btn("⬅️ Бозгашт", callback_data="ff")],
                ]),
            )
        else:
            context.user_data.clear()
            context.user_data["state"] = "wait_id"
            context.user_data["pack"] = key
            await edit(
                q,
                f"Шумо интихоб кардед: {name} — {price} сомонӣ\n\n"
                "Акнун 🆔 рақами Free Fire-и худро фиристед:",
                Kb([[Btn("❌ Бекор кардан", callback_data="menu")]]),
            )

    elif d == "bal":
        context.user_data.clear()
        await edit(
            q,
            f"💰 Баланси шумо: {balance(u.id)} сомонӣ",
            back_kb([[Btn("➕ Пур кардани баланс", callback_data="topup")]]),
        )

    elif d == "topup":
        need = context.user_data.get("suggest")
        context.user_data.clear()
        context.user_data["state"] = "topup_amount"
        text = (
            "➕ Пур кардани баланс\n\n"
            f"Чанд сомонӣ мегузоред? Рақамро нависед (камаш {MIN_TOPUP})."
        )
        if need:
            text += f"\n\nБарои бастаи интихобкардаатон {need} сомонӣ намерасад."
        await edit(q, text, back_kb())

    elif d == "orders":
        context.user_data.clear()
        rows = db.execute(
            "SELECT * FROM orders WHERE user_id=? ORDER BY id DESC LIMIT 10", (u.id,)
        ).fetchall()
        if not rows:
            text = "📦 Шумо ҳанӯз фармоиш надоред."
        else:
            lines = [
                f"#{r['id']} · {r['pack']} · {r['price']} сомонӣ · "
                f"{STATUS.get(r['status'], r['status'])}\n    🆔 {r['gid']}"
                for r in rows
            ]
            text = "📦 Фармоишҳои охирини шумо:\n\n" + "\n\n".join(lines)
        await edit(q, text, back_kb())

    elif d.startswith("cfm:"):
        if context.user_data.get("state") != "confirm_id":
            await edit(q, "Вақт гузашт. Аз меню аз нав оғоз кунед.", back_kb())
            return
        if d == "cfm:no":
            context.user_data["state"] = "wait_id"
            await edit(q, "🆔 ID-и дурустро нависед:")
            return
        key = context.user_data.get("pack")
        gid = context.user_data.get("gid")
        await edit(q, f"✅ Тасдиқ шуд: 🆔 {gid}")
        await place_order(context, u, key, gid, q.message.reply_text)

    elif d == "support":
        context.user_data.clear()
        await edit(q, f"🆘 Дастгирӣ\n\n{SUPPORT_TEXT}", back_kb())


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    upsert_user(u)
    state = context.user_data.get("state")
    text = update.message.text.strip()

    if state == "topup_amount":
        if not text.isdigit() or int(text) < MIN_TOPUP:
            await update.message.reply_text(
                f"Маблағро бо рақам нависед (камаш {MIN_TOPUP} сомонӣ), масалан 20."
            )
            return
        context.user_data["amount"] = int(text)
        context.user_data["state"] = "topup_receipt"
        await update.message.reply_text(
            f"💳 Пулро ба {CARD_NAME} гузаронед:\n{CARD_NUMBER}\n\n"
            f"Маблағ: {text} сомонӣ\n\n"
            "📸 Баъд аз пардохт расид (чек)-ро ба сурат фиристед.",
            reply_markup=back_kb(),
        )

    elif state == "wait_id":
        if not text.isdigit() or not (6 <= len(text) <= 14):
            await update.message.reply_text(
                "ID нодуруст аст. Танҳо рақамҳоро нависед (масалан 1115522567)."
            )
            return
        key = context.user_data["pack"]
        if AUTO_DONATE and donatix.KEY:
            await update.message.reply_text("⏳ ID-ро месанҷам...")
            res, nick = await donatix.check_player(key, text)
            if res == "bad":
                await update.message.reply_text(
                    "❌ Ин ID дар Free Fire (СНГ) ёфт нашуд. ID-ро санҷед ва дубора нависед:"
                )
                return
            if res == "ok":
                context.user_data["state"] = "confirm_id"
                context.user_data["gid"] = text
                await update.message.reply_text(
                    f"👤 Аккаунт: {nick}\n🆔 {text}\n\nИн аккаунти шумост?",
                    reply_markup=Kb([[
                        Btn("✅ Ҳа, давом диҳед", callback_data="cfm:yes"),
                        Btn("❌ Не, иваз мекунам", callback_data="cfm:no"),
                    ]]),
                )
                return
        await place_order(context, u, key, text, update.message.reply_text)
    else:
        await update.message.reply_text(
            menu_text(u.id, u.first_name), reply_markup=menu_kb()
        )


async def on_receipt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    upsert_user(u)
    if context.user_data.get("state") != "topup_receipt":
        await update.message.reply_text(
            "Аввал «💰 Баланс» → «➕ Пур кардани баланс»-ро пахш кунед.",
            reply_markup=menu_kb(),
        )
        return
    amount = context.user_data["amount"]
    cur = db.execute(
        "INSERT INTO topups(user_id, amount, status, created) VALUES(?,?,?,?)",
        (u.id, amount, "checking", int(time.time())),
    )
    tid = cur.lastrowid
    db.commit()
    context.user_data.clear()
    caption = (
        f"🧾 Пур кардани баланс #{tid}\n"
        f"Мизоҷ: {user_label(u.id)}\n"
        f"Маблағ: {amount} сомонӣ\n\nПул гузашт?"
    )
    buttons = Kb([[
        Btn("✅ Ҳа, сум гузашт", callback_data=f"tok:{tid}"),
        Btn("❌ Не, нагузашт", callback_data=f"tno:{tid}"),
    ]])
    if update.message.photo:
        await context.bot.send_photo(
            ADMIN_ID, update.message.photo[-1].file_id,
            caption=caption, reply_markup=buttons,
        )
    else:
        await context.bot.send_document(
            ADMIN_ID, update.message.document.file_id,
            caption=caption, reply_markup=buttons,
        )
    await update.message.reply_text(
        "✅ Чек ба админ фиристода шуд. Лутфан интизор шавед...",
        reply_markup=menu_kb(),
    )


async def admin_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await safe_answer(q)
    if q.from_user.id != ADMIN_ID:
        return
    action, sid = q.data.split(":")
    sid = int(sid)

    if action == "retry":
        cur = db.execute(
            "UPDATE orders SET status='donating' WHERE id=? AND status='wait_donat'",
            (sid,),
        )
        db.commit()
        if cur.rowcount == 1:
            await mark(q, f"🔁 Фармоиши #{sid}: худкор аз нав оғоз шуд...")
            spawn(run_auto(context, sid))
        else:
            await mark(q, f"Фармоиши #{sid} ҳоло дар ҳолати дигар аст.")
        return

    if action in ("tok", "tno"):
        t = db.execute("SELECT * FROM topups WHERE id=?", (sid,)).fetchone()
        if not t:
            await mark(q, "Ёфт нашуд.")
            return
        new_status = "approved" if action == "tok" else "rejected"
        cur = db.execute(
            "UPDATE topups SET status=? WHERE id=? AND status='checking'",
            (new_status, sid),
        )
        if cur.rowcount != 1:
            db.rollback()
            await mark(q, f"Пур кардани #{sid} аллакай коркард шудааст.")
            return
        if action == "tok":
            db.execute(
                "UPDATE users SET balance=balance+? WHERE id=?",
                (t["amount"], t["user_id"]),
            )
            db.commit()
            await tell(
                context, t["user_id"],
                f"✅ Ҳа, сум гузашт! Баланс {t['amount']} сомонӣ пур шуд.\n"
                f"💰 Баланси шумо: {balance(t['user_id'])} сомонӣ",
                menu_kb(),
            )
            await mark(q, f"✅ Пур кардани #{sid}: {t['amount']} сомонӣ ба баланс гузашт.")
        else:
            db.commit()
            await tell(
                context, t["user_id"],
                "❌ Пардохт ёфт нашуд. Агар пул гузаронида бошед, "
                "ба дастгирӣ нависед ё аз нав кӯшиш кунед.",
                menu_kb(),
            )
            await mark(q, f"❌ Пур кардани #{sid}: пардохт ёфт нашуд.")

    elif action in ("done", "cancel"):
        o = db.execute("SELECT * FROM orders WHERE id=?", (sid,)).fetchone()
        if not o:
            await mark(q, "Фармоиш ёфт нашуд.")
            return
        new_status = "done" if action == "done" else "canceled"
        cur = db.execute(
            "UPDATE orders SET status=? WHERE id=? AND status='wait_donat'",
            (new_status, sid),
        )
        if cur.rowcount != 1:
            db.rollback()
            await mark(q, f"Фармоиши #{sid} аллакай коркард шудааст.")
            return
        if action == "done":
            db.commit()
            await mark(q, f"🎉 Фармоиши #{sid}: донат анҷом ёфт.")
            await notify_done(context, sid)
        else:
            db.execute(
                "UPDATE users SET balance=balance+? WHERE id=?",
                (o["price"], o["user_id"]),
            )
            db.commit()
            await tell(
                context, o["user_id"],
                f"❌ Фармоиши #{sid} бекор шуд. {o['price']} сомонӣ ба баланси шумо баргашт.\n"
                f"💰 Баланс: {balance(o['user_id'])} сомонӣ",
                menu_kb(),
            )
            await mark(q, f"❌ Фармоиши #{sid} бекор шуд, пул баргашт.")


async def resume_auto(app):
    ctx = types.SimpleNamespace(bot=app.bot)
    for r in db.execute("SELECT id FROM orders WHERE status='donating'").fetchall():
        spawn(run_auto(ctx, r["id"]))


def main():
    app = Application.builder().token(TOKEN).post_init(resume_auto).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(admin_buttons, pattern="^(tok|tno|done|cancel|retry):"))
    app.add_handler(CallbackQueryHandler(
        on_button, pattern="^(menu|games|ff|bal|topup|orders|support|p:.*|cfm:.*)$"
    ))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, on_receipt))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    print("Free Fire Donat TJ фаъол шуд!")
    app.run_polling()


if __name__ == "__main__":
    main()
