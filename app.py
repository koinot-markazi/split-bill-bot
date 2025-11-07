import logging
import sqlite3
import os
import json, re
from datetime import datetime
from typing import Dict, List, Tuple

from google import genai
from google.genai import types

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters
)

# ---------------------- ЛОГИ ----------------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger("splitter-bot")

from dotenv import load_dotenv
load_dotenv()

# ---------------------- КОНФИГ ----------------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

_gemini_client = None
MODEL_ID = "gemini-2.5-flash"  # быстрый вариант; при 404 падаем на pro

def get_gemini_client():
    global _gemini_client
    if _gemini_client is not None:
        return _gemini_client
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY is not set. Export it to enable OCR.")
    _gemini_client = genai.Client(
        api_key=GEMINI_API_KEY,
        http_options=types.HttpOptions(api_version="v1")
    )
    return _gemini_client

def build_resto_ui(conn, session_id: int, current_user_id: int):
    """
    Собирает текст и клавиатуру для ресторанной сессии.
    - Одна кнопка на позицию: '🍽 Название [N]' (N — сколько человек выбрали)
    - Если текущий пользователь выбрал позицию — добавляется '✅'
    - Внизу добавляется кнопка '🧾 Закрыть счёт'
    Возвращает: (text, InlineKeyboardMarkup, creator_id)
    """
    c = conn.cursor()

    # Текст шапки
    msg = "✅ Чек обработан!\n\nВыберите свои позиции (нажмите на нужные, повторное нажатие снимает выбор):\n\n"

    # Все позиции
    c.execute(
        "SELECT id, item_name, price, quantity FROM resto_items WHERE session_id = ? ORDER BY id",
        (session_id,)
    )
    items_rows = c.fetchall()

    # Создатель сессии (для прав на закрытие)
    c.execute("SELECT creator_id FROM resto_sessions WHERE id = ?", (session_id,))
    row = c.fetchone()
    creator_id = row[0] if row else None

    keyboard = []

    for (item_id, name, price, qty) in items_rows:
        total = price * qty
        qty_text = f" x{qty}" if qty > 1 else ""

        # кто выбрал эту позицию
        c.execute("SELECT user_id FROM resto_choices WHERE item_id = ?", (item_id,))
        choosers = [r[0] for r in c.fetchall()]
        count = len(choosers)
        picked_by_me = current_user_id in choosers

        # текст кнопки
        btn_text = f"🍽 {name}"
        if count > 0:
            btn_text += f" [{count}]"
        if picked_by_me:
            btn_text += " ✅"

        # строка текста для позиции
        msg += f"• {name}{qty_text} — {total:,.0f} сум\n"

        # одна кнопка на позицию
        keyboard.append([
            InlineKeyboardButton(btn_text, callback_data=f"item_{item_id}")
        ])

    # Кнопка «Закрыть счёт» — видна всем; проверка прав будет в хендлере
    keyboard.append([InlineKeyboardButton("🧾 Закрыть счёт", callback_data="close_resto")])

    return msg, InlineKeyboardMarkup(keyboard), creator_id


# ---------------------- БД ----------------------
class Database:
    def __init__(self, db_name="split_bot.db"):
        self.db_name = db_name
        self.init_db()

    def get_connection(self):
        return sqlite3.connect(self.db_name)

    def init_db(self):
        conn = self.get_connection()
        c = conn.cursor()

        # Общий счёт (/newbill)
        c.execute("""
            CREATE TABLE IF NOT EXISTS bills (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                creator_id INTEGER NOT NULL,
                creator_username TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                closed_at TIMESTAMP,
                status TEXT DEFAULT 'open'
            )
        """)

        c.execute("""
            CREATE TABLE IF NOT EXISTS bill_participants (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bill_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                username TEXT,
                UNIQUE (bill_id, user_id),
                FOREIGN KEY (bill_id) REFERENCES bills(id)
            )
        """)

        c.execute("""
            CREATE TABLE IF NOT EXISTS expenses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bill_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                username TEXT,
                description TEXT,
                amount REAL NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (bill_id) REFERENCES bills(id)
            )
        """)

        # Ресторанный режим (/resto)
        c.execute("""
            CREATE TABLE IF NOT EXISTS resto_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                creator_id INTEGER NOT NULL,
                creator_username TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                closed_at TIMESTAMP,
                status TEXT DEFAULT 'open'
            )
        """)

        c.execute("""
            CREATE TABLE IF NOT EXISTS resto_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER NOT NULL,
                item_name TEXT NOT NULL,
                price REAL NOT NULL,
                quantity INTEGER DEFAULT 1,
                is_shared BOOLEAN DEFAULT FALSE,
                FOREIGN KEY (session_id) REFERENCES resto_sessions(id)
            )
        """)

        c.execute("""
            CREATE TABLE IF NOT EXISTS resto_choices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                item_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                username TEXT,
                FOREIGN KEY (item_id) REFERENCES resto_items(id)
            )
        """)

        conn.commit()
        conn.close()

db = Database()


# ---------------------- ХЕЛПЕРЫ ----------------------
def minimize_transactions(balances: Dict[int, float]) -> List[Tuple[int, int, float]]:
    """
    Жадный алгоритм минимизации количества переводов.
    Возвращает список (from_user_id, to_user_id, amount)
    """
    txs = []
    debtors = [(uid, -amt) for uid, amt in balances.items() if amt < -0.01]
    creditors = [(uid, amt) for uid, amt in balances.items() if amt > 0.01]

    debtors.sort(key=lambda x: x[1], reverse=True)
    creditors.sort(key=lambda x: x[1], reverse=True)

    i = j = 0
    while i < len(debtors) and j < len(creditors):
        duid, debt = debtors[i]
        cuid, cred = creditors[j]
        amount = min(debt, cred)
        txs.append((duid, cuid, amount))
        debt -= amount
        cred -= amount
        if debt <= 0.01:
            i += 1
        else:
            debtors[i] = (duid, debt)
        if cred <= 0.01:
            j += 1
        else:
            creditors[j] = (cuid, cred)
    return txs

def extract_json(text: str):
    # ```json ... ```
    m = re.search(r"```json\s*(\{[\s\S]*?\})\s*```", text, re.IGNORECASE)
    if m:
        return json.loads(m.group(1))
    # целиком
    try:
        return json.loads(text)
    except:
        # первая {...}
        m = re.search(r"(\{[\s\S]*\})", text)
        if m:
            return json.loads(m.group(1))
    raise ValueError("LLM did not return valid JSON")

# ---------------------- ХЕНДЛЕРЫ ----------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Привет! Я бот для разделения счетов.\n\n"
        "📝 Команды:\n"
        "/newbill — общий счёт и ручные траты\n"
        "/resto — чек из ресторана (фото)\n"
        "/closebill — закрыть текущий счёт/сессию\n"
        "/history — последние 10 записей\n\n"
        "Добавьте меня в группу со своими друзьями."
    )

# /newbill
async def newbill(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    username = update.effective_user.username or update.effective_user.first_name

    conn = db.get_connection()
    c = conn.cursor()

    c.execute("SELECT id FROM bills WHERE chat_id = ? AND status = 'open'", (chat_id,))
    if c.fetchone():
        await update.message.reply_text("❌ Уже есть открытый счет. Закройте его командой /closebill")
        conn.close()
        return

    c.execute(
        "INSERT INTO bills (chat_id, creator_id, creator_username) VALUES (?, ?, ?)",
        (chat_id, user_id, username)
    )
    bill_id = c.lastrowid
    conn.commit()
    conn.close()

    keyboard = [[InlineKeyboardButton("✅ Присоединиться к счету", callback_data=f"join_bill_{bill_id}")]]
    await update.message.reply_text(
        f"💰 Новый счет создан!\nСоздатель: @{username}\n\n"
        "Участники: нажмите кнопку ниже, затем присылайте траты в формате:\n"
        "<описание> <сумма>\n\nНапример: Пицца 50000",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def join_bill_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    bill_id = int(q.data.split("_")[2])
    user_id = q.from_user.id
    username = q.from_user.username or q.from_user.first_name

    conn = db.get_connection()
    c = conn.cursor()

    c.execute("SELECT status FROM bills WHERE id = ?", (bill_id,))
    r = c.fetchone()
    if not r or r[0] != "open":
        await q.edit_message_text("❌ Этот счет уже закрыт.")
        conn.close()
        return

    try:
        c.execute(
            "INSERT INTO bill_participants (bill_id, user_id, username) VALUES (?, ?, ?)",
            (bill_id, user_id, username)
        )
        conn.commit()

        c.execute("SELECT username FROM bill_participants WHERE bill_id = ?", (bill_id,))
        parts = [row[0] for row in c.fetchall()]

        keyboard = [[InlineKeyboardButton("✅ Присоединиться к счету", callback_data=f"join_bill_{bill_id}")]]
        await q.edit_message_text(
            q.message.text + f"\n\nУчастники ({len(parts)}): " + ", ".join([f"@{p}" for p in parts]),
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    except sqlite3.IntegrityError:
        await q.answer("Вы уже в этом счете!", show_alert=True)
    finally:
        conn.close()

async def handle_expense(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    username = update.effective_user.username or update.effective_user.first_name
    text = update.message.text.strip()

    conn = db.get_connection()
    c = conn.cursor()

    c.execute("SELECT id FROM bills WHERE chat_id = ? AND status = 'open'", (chat_id,))
    r = c.fetchone()
    if not r:
        conn.close()
        return
    bill_id = r[0]

    c.execute("SELECT id FROM bill_participants WHERE bill_id = ? AND user_id = ?", (bill_id, user_id))
    if not c.fetchone():
        conn.close()
        return

    parts_ = text.rsplit(maxsplit=1)
    if len(parts_) != 2:
        conn.close()
        return
    description, amount_str = parts_

    try:
        amount = float(amount_str.replace(" ", "").replace(",", ""))
    except ValueError:
        conn.close()
        return

    c.execute(
        "INSERT INTO expenses (bill_id, user_id, username, description, amount) VALUES (?, ?, ?, ?, ?)",
        (bill_id, user_id, username, description, amount)
    )
    conn.commit()
    conn.close()

    await update.message.reply_text(f"✅ Добавлено: {description} — {amount:,.0f} сум")

# /resto
async def resto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    username = update.effective_user.username or update.effective_user.first_name

    conn = db.get_connection()
    c = conn.cursor()

    c.execute("SELECT id FROM resto_sessions WHERE chat_id = ? AND status = 'open'", (chat_id,))
    if c.fetchone():
        await update.message.reply_text("❌ Уже есть открытая /resto сессия. Закройте её /closebill")
        conn.close()
        return

    c.execute(
        "INSERT INTO resto_sessions (chat_id, creator_id, creator_username) VALUES (?, ?, ?)",
        (chat_id, user_id, username)
    )
    conn.commit()
    conn.close()

    await update.message.reply_text(
        f"🍽 Сессия ресторана создана!\nСоздатель: @{username}\n\n"
        "📸 Отправьте фото чека, чтобы я его обработал."
    )

async def handle_receipt_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    conn = db.get_connection()
    c = conn.cursor()

    c.execute("SELECT id, creator_id FROM resto_sessions WHERE chat_id = ? AND status = 'open'", (chat_id,))
    r = c.fetchone()
    if not r:
        conn.close()
        return
    session_id, creator_id = r

    if user_id != creator_id:
        await update.message.reply_text("❌ Только создатель сессии может загружать чек.")
        conn.close()
        return

    c.execute("SELECT COUNT(*) FROM resto_items WHERE session_id = ?", (session_id,))
    if c.fetchone()[0] > 0:
        await update.message.reply_text("❌ Чек уже загружен.")
        conn.close()
        return

    conn.close()
    await update.message.reply_text("⏳ Обрабатываю чек...")

    # скачиваем фото
    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    photo_path = f"temp_receipt_{chat_id}.jpg"
    await file.download_to_drive(photo_path)

    try:
        client = get_gemini_client()
        with open(photo_path, "rb") as f:
            image_data = f.read()

        prompt = """
        Извлеки все позиции из ресторанного чека и верни строго JSON:
        {
          "items": [
            {"name": "string", "price": number, "quantity": number}
          ]
        }
        Правила:
        - Цена только числом (без валюты)
        - Учитывай множители (x2, ×3 и т.п.) в quantity
        - Включай блюда, напитки, сервис/чаевые
        - Никаких комментариев, только JSON
        """

        try:
            resp = client.models.generate_content(
                model=MODEL_ID,
                contents=[
                    types.Part.from_bytes(data=image_data, mime_type="image/jpeg"),
                    prompt
                ]
            )
        except Exception as e:
            if "404" in str(e).lower() or "not found" in str(e).lower():
                resp = client.models.generate_content(
                    model="gemini-2.5-pro",
                    contents=[
                        types.Part.from_bytes(data=image_data, mime_type="image/jpeg"),
                        prompt
                    ]
                )
            else:
                raise

        text = (resp.text or "").strip()

        # извлекаем JSON
        try:
            data = extract_json(text)
        except Exception:
            await update.message.reply_text("❌ Не удалось извлечь позиции. Попробуйте другое фото.")
            return

        items = data.get("items", [])
        if not items:
            await update.message.reply_text("❌ Не удалось распознать позиции в чеке.")
            return

        conn = db.get_connection()
        c = conn.cursor()
        for item in items:
            name = (item.get("name") or "").strip()
            try:
                price = float(item.get("price", 0) or 0)
            except Exception:
                price = 0.0
            try:
                qty = int(item.get("quantity", 1) or 1)
            except Exception:
                qty = 1
            if not name or price <= 0:
                continue

            c.execute(
                "INSERT INTO resto_items (session_id, item_name, price, quantity) VALUES (?, ?, ?, ?)",
                (session_id, name, price, qty)
            )

        conn.commit()

        # соберём текст и клавиатуру с учётом текущего пользователя
        msg, reply_markup, _creator_id = build_resto_ui(conn, session_id, user_id)

        conn.close()
        await update.message.reply_text(msg, reply_markup=reply_markup)

    except Exception as e:
        logger.exception("Error processing receipt")
        await update.message.reply_text(f"❌ Ошибка при обработке чека: {e}")
    finally:
        if os.path.exists(photo_path):
            os.remove(photo_path)

async def handle_item_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    data = q.data
    user_id = q.from_user.id

    # Обработка закрытия счёта (кнопка внизу)
    if data == "close_resto":
        # проверим, что инициатор — создатель сессии
        conn = db.get_connection(); c = conn.cursor()
        # по текущему сообщению находим последнюю открытую сессию в чате
        chat_id = q.message.chat.id
        c.execute("SELECT id, creator_id FROM resto_sessions WHERE chat_id = ? AND status = 'open' ORDER BY id DESC LIMIT 1", (chat_id,))
        row = c.fetchone()
        if not row:
            conn.close()
            await q.answer("Нет открытой сессии.", show_alert=True)
            return
        session_id, creator_id = row
        if user_id != creator_id:
            conn.close()
            await q.answer("Только создатель может закрыть счёт.", show_alert=True)
            return
        # закрываем
        chat_id = q.message.chat.id
        await close_resto(update, context, session_id, conn, chat_id=chat_id)
        return

    # Обработка выбора позиции
    if not data.startswith("item_"):
        return

    item_id = int(data.split("_")[1])

    conn = db.get_connection()
    c = conn.cursor()

    # Проверим статус сессии
    c.execute("""
        SELECT rs.id, rs.status
        FROM resto_sessions rs
        JOIN resto_items ri ON rs.id = ri.session_id
        WHERE ri.id = ?
    """, (item_id,))
    r = c.fetchone()
    if not r or r[1] != "open":
        conn.close()
        await q.answer("❌ Эта сессия уже закрыта.", show_alert=True)
        return

    # Тогглим выбор: если уже выбран — снять; если не выбран — выбрать
    c.execute("SELECT 1 FROM resto_choices WHERE item_id = ? AND user_id = ?", (item_id, user_id))
    exists = c.fetchone() is not None
    if exists:
        c.execute("DELETE FROM resto_choices WHERE item_id = ? AND user_id = ?", (item_id, user_id))
        picked_msg = "Выбор снят"
    else:
        c.execute("INSERT INTO resto_choices (item_id, user_id, username) VALUES (?, ?, ?)",
                  (item_id, user_id, q.from_user.username or q.from_user.first_name))
        picked_msg = "Вы выбрали блюдо"

    # Узнаем session_id для сборки UI
    c.execute("SELECT session_id FROM resto_items WHERE id = ?", (item_id,))
    session_id = c.fetchone()[0]

    conn.commit()

    # Пересоберём текст и клавиатуру, отметив текущего юзера
    msg, markup, _creator_id = build_resto_ui(conn, session_id, user_id)
    conn.close()

    # Обновим сообщение (текст и клавиатуру)
    try:
        await q.edit_message_text(msg, reply_markup=markup)
    except Exception:
        # если редактировать текст нельзя (старые сообщения/правила TG), хотя бы клавиатуру
        await q.edit_message_reply_markup(reply_markup=markup)

    await q.answer(picked_msg)


# /closebill
async def closebill(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    conn = db.get_connection()
    c = conn.cursor()

    c.execute("SELECT id, creator_id FROM bills WHERE chat_id = ? AND status = 'open'", (chat_id,))
    bill_res = c.fetchone()

    c.execute("SELECT id, creator_id FROM resto_sessions WHERE chat_id = ? AND status = 'open'", (chat_id,))
    resto_res = c.fetchone()

    if bill_res:
        bill_id, creator_id = bill_res
        if user_id != creator_id:
            await update.message.reply_text("❌ Только создатель счета может его закрыть.")
            conn.close()
            return
        await close_newbill(update, context, bill_id, conn)
        return

    if resto_res:
        session_id, creator_id = resto_res
        if user_id != creator_id:
            await update.message.reply_text("❌ Только создатель сессии может её закрыть.")
            conn.close()
            return
        await close_resto(update, context, session_id, conn)
        return

    conn.close()
    await update.message.reply_text("❌ Нет открытых счетов в этом чате.")

async def close_newbill(update: Update, context: ContextTypes.DEFAULT_TYPE, bill_id: int, conn):
    c = conn.cursor()

    c.execute("SELECT user_id, username FROM bill_participants WHERE bill_id = ?", (bill_id,))
    participants = {row[0]: row[1] for row in c.fetchall()}
    if not participants:
        await update.message.reply_text("❌ Нет участников в счете.")
        conn.close()
        return

    c.execute("SELECT user_id, amount FROM expenses WHERE bill_id = ?", (bill_id,))
    expenses = c.fetchall()
    if not expenses:
        await update.message.reply_text("❌ Нет расходов для расчета.")
        conn.close()
        return

    total = sum(a for _, a in expenses)
    per_person = total / len(participants)

    user_paid = {}
    for uid, amt in expenses:
        user_paid[uid] = user_paid.get(uid, 0) + amt

    balances = {uid: user_paid.get(uid, 0) - per_person for uid in participants}
    txs = minimize_transactions(balances)

    msg = "💰 Счет закрыт!\n\n"
    msg += f"Общая сумма: {total:,.0f} сум\nНа человека: {per_person:,.0f} сум\nУчастников: {len(participants)}\n\n"
    msg += "📊 Расходы:\n"
    for uid, name in participants.items():
        msg += f"@{name}: {user_paid.get(uid, 0):,.0f} сум\n"

    msg += "\n💸 Расчеты:\n"
    if txs:
        for from_id, to_id, amount in txs:
            msg += f"@{participants[from_id]} → @{participants[to_id]}: {amount:,.0f} сум\n"
    else:
        msg += "Все уже расплатились! ✅\n"

    c.execute("UPDATE bills SET status='closed', closed_at=? WHERE id=?", (datetime.now(), bill_id))
    conn.commit()
    conn.close()

    await update.message.reply_text(msg)

async def close_resto(update: Update, context: ContextTypes.DEFAULT_TYPE, session_id: int, conn, chat_id: int | None = None):

    c = conn.cursor()

    c.execute("""
        SELECT DISTINCT rc.user_id, rc.username
        FROM resto_choices rc
        JOIN resto_items ri ON rc.item_id = ri.id
        WHERE ri.session_id = ?
    """, (session_id,))
    participants = {row[0]: row[1] for row in c.fetchall()}

    c.execute("SELECT creator_id, creator_username FROM resto_sessions WHERE id = ?", (session_id,))
    creator_id, creator_name = c.fetchone()
    if creator_id not in participants:
        participants[creator_id] = creator_name

    if not participants:
        await update.message.reply_text("❌ Никто не выбрал блюда.")
        conn.close()
        return

    user_totals = {uid: 0.0 for uid in participants}
    c.execute("SELECT id, item_name, price, quantity, is_shared FROM resto_items WHERE session_id = ?", (session_id,))
    items = c.fetchall()

    shared_total = 0.0
    for item_id, name, price, qty, is_shared in items:
        total_price = price * qty
        if is_shared:
            shared_total += total_price
        else:
            c.execute("SELECT user_id FROM resto_choices WHERE item_id = ?", (item_id,))
            choosers = [row[0] for row in c.fetchall()]
            if choosers:
                split = total_price / len(choosers)
                for uid in choosers:
                    user_totals[uid] += split

    if shared_total > 0:
        per_person = shared_total / len(participants)
        for uid in participants:
            user_totals[uid] += per_person

    total = sum(user_totals.values())
    msg = "🍽 Чек из ресторана разделен!\n\n"
    msg += f"Общая сумма: {total:,.0f} сум\nУчастников: {len(participants)}\n\n"
    msg += "💰 К оплате:\n"
    for uid, name in participants.items():
        msg += f"@{name}: {user_totals[uid]:,.0f} сум\n"

    c.execute("UPDATE resto_sessions SET status='closed', closed_at=? WHERE id=?", (datetime.now(), session_id))
    conn.commit()
    conn.close()

    if chat_id is None:
        # попробуем вытащить из update, если это не callback
        try:
            chat_id = update.effective_chat.id
        except Exception:
            chat_id = None

    if chat_id:
        await context.bot.send_message(chat_id=chat_id, text=msg)
    else:
        # последний шанс — через объект сообщения, если он есть
        if getattr(update, "callback_query", None) and update.callback_query and update.callback_query.message:
            await update.callback_query.message.reply_text(msg)
        elif getattr(update, "message", None):
            await update.message.reply_text(msg)

# /history
async def history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    conn = db.get_connection()
    c = conn.cursor()

    c.execute("""
        SELECT id, creator_username, created_at, closed_at, status
        FROM bills
        WHERE chat_id = ?
        ORDER BY created_at DESC
        LIMIT 10
    """, (chat_id,))
    bills = c.fetchall()

    c.execute("""
        SELECT id, creator_username, created_at, closed_at, status
        FROM resto_sessions
        WHERE chat_id = ?
        ORDER BY created_at DESC
        LIMIT 10
    """, (chat_id,))
    restos = c.fetchall()

    msg = "📜 История:\n\n"
    if bills:
        msg += "💰 /newbill:\n"
        for bid, creator, created, closed, status in bills:
            emoji = "✅" if status == "closed" else "🔓"
            # created может быть строкой — не парсим, просто показываем
            msg += f"{emoji} #{bid} — @{creator} ({created})\n"
        msg += "\n"

    if restos:
        msg += "🍽 /resto:\n"
        for sid, creator, created, closed, status in restos:
            emoji = "✅" if status == "closed" else "🔓"
            msg += f"{emoji} #{sid} — @{creator} ({created})\n"

    if not bills and not restos:
        msg = "📜 История пуста. Создайте первый счёт с помощью /newbill или /resto."

    conn.close()
    await update.message.reply_text(msg)

   

# ---------------------- MAIN ----------------------
def main():
    if not TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN is not set. Export it and rerun.")
        return

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("newbill", newbill))
    app.add_handler(CommandHandler("resto", resto))
    app.add_handler(CommandHandler("closebill", closebill))
    app.add_handler(CommandHandler("history", history))  # <— теперь точно есть

    app.add_handler(CallbackQueryHandler(join_bill_callback, pattern=r"^join_bill_"))

    app.add_handler(CallbackQueryHandler(handle_item_choice, pattern=r"^item_"))
    app.add_handler(CallbackQueryHandler(handle_item_choice, pattern=r"^close_resto$"))


    app.add_handler(MessageHandler(filters.PHOTO, handle_receipt_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_expense))

    logger.info("Bot started. Polling...")
    app.run_polling()

if __name__ == "__main__":
    main()
