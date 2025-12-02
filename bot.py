import os
import json
import logging
import re
import html
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any

import pytz
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, error
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

# -------------------------
# Налаштування / константи
# -------------------------
load_dotenv()

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("TOKEN")
DUMMY_PLACEHOLDER = "YOUR_TOKEN_HERE"
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "-1002003419071"))

MAX_REQUESTS_PER_HOUR = int(os.getenv("MAX_REQUESTS_PER_HOUR", "10"))
TIMEZONE = pytz.timezone(os.getenv("TZ", "Europe/Kyiv"))
DATA_FILE = os.getenv("DATA_FILE", "orders_data.json")

TARIFFS = {
    "1_day": "1 день — 20₴",
    "30_days": "30 днів — 70₴",
    "90_days": "90 днів — 150₴",
    "180_days": "180 днів — 190₴",
    "forever": "Назавжди — 250₴"
}

AWAITING_FIO = "awaiting_fio"
AWAITING_DOB = "awaiting_dob"
AWAITING_PHOTO = "awaiting_photo"

# -------------------------
# Логування
# -------------------------
logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# -------------------------
# Утиліти
# -------------------------
def now_iso_with_tz() -> str:
    """Повертає поточний час у форматі ISO з часовим поясом."""
    return datetime.now(TIMEZONE).isoformat()

def parse_iso_datetime(s: str) -> Optional[datetime]:
    """Парсить ISO-рядок у datetime з локалізацією."""
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = TIMEZONE.localize(dt)
        return dt
    except Exception as e:
        logger.warning("parse_iso_datetime error: %s for %s", e, s)
        return None

def escape_markdown_v2(text: Optional[str]) -> str:
    """
    Екранує всі спецсимволи MarkdownV2 для Telegram,
    що містяться у даних користувача.
    """
    if not text:
        return ""
    # Символи, які потрібно екранувати
    mdv2_chars = r'_*[]()~`>#+-=|{}.!'
    return re.sub(f"([{re.escape(mdv2_chars)}])", r'\\\1', text)

def escape_html(text: Optional[str]) -> str:
    """Екранує HTML символи."""
    if text is None:
        return ""
    return html.escape(text)

# -------------------------
# Робота з файлом замовлень
# -------------------------
def load_orders() -> List[Dict[str, Any]]:
    """Завантажує дані замовлень з JSON файлу."""
    if not os.path.exists(DATA_FILE):
        return []
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except Exception as e:
        logger.exception("Failed to load orders: %s", e)
        return []

def save_orders(orders: List[Dict[str, Any]]) -> bool:
    """Зберігає дані замовлень у JSON файл."""
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(orders, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        logger.exception("Failed to save orders: %s", e)
        return False

def get_last_order_for_client(client_id: int) -> Optional[Dict[str, Any]]:
    """Повертає останнє замовлення для клієнта."""
    orders = load_orders()
    cid = str(client_id)
    for row in reversed(orders):
        if str(row.get("client_id")) == cid:
            return row
    return None

def get_order_status(client_id: int) -> Optional[str]:
    """Повертає статус останнього замовлення."""
    row = get_last_order_for_client(client_id)
    return row.get("status") if row else None

def update_order_status(client_id: int, new_status: str) -> bool:
    """Оновлює статус останнього замовлення."""
    orders = load_orders()
    cid = str(client_id)
    for row in reversed(orders):
        if str(row.get("client_id")) == cid:
            row["status"] = new_status
            row["status_updated_at"] = now_iso_with_tz()
            return save_orders(orders)
    return False

def add_request(client_id: int, username: str = "немає", tariff_key: Optional[str] = None,
                 fio: Optional[str] = None, dob: Optional[str] = None) -> bool:
    """Додає новий запит (замовлення)."""
    orders = load_orders()
    new_request = {
        "client_id": str(client_id),
        "username": username or "немає",
        "status": "waiting_req",
        "created_at": now_iso_with_tz(),
        "tariff_key": tariff_key,
        "tariff_text": TARIFFS.get(tariff_key) if tariff_key else None,
        "fio": fio,
        "dob": dob,
    }
    orders.append(new_request)
    return save_orders(orders)

# -------------------------
# Час / ліміти
# -------------------------
def check_request_limit(client_id: int) -> bool:
    """Перевіряє, чи не перевищив клієнт ліміт запитів за останню годину."""
    try:
        orders = load_orders()
        one_hour_ago = datetime.now(TIMEZONE) - timedelta(hours=1)
        count = 0
        cid_str = str(client_id)
        
        for row in orders:
            if str(row.get("client_id")) != cid_str:
                continue
            
            created = row.get("created_at")
            parsed = parse_iso_datetime(created)
            
            if parsed and parsed > one_hour_ago:
                count += 1
                
        return count < MAX_REQUESTS_PER_HOUR
        
    except Exception as e:
        logger.exception("check_request_limit error: %s", e)
        return True

# -------------------------
# Admin check
# -------------------------
async def admin_check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Перевіряє, чи команда викликана з адмін-чату."""
    if update.effective_chat is None:
        return False
        
    if update.effective_chat.id != ADMIN_CHAT_ID:
        try:
            if update.effective_message:
                await update.effective_message.reply_text("Ця команда доступна лише в адмін-чаті.")
        except Exception:
            pass
        return False
        
    return True

# -------------------------
# Хендлери
# -------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обробляє команду /start."""
    context.user_data.clear()
    keyboard = [[InlineKeyboardButton("Продовжити", callback_data="start_menu")]]
    await update.message.reply_text("Вітаємо в додатку FunsDiia ! Натисніть нижче щоб розпочати!", reply_markup=InlineKeyboardMarkup(keyboard))

async def start_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обробляє головне меню."""
    query = update.callback_query
    await query.answer()
    keyboard = [[InlineKeyboardButton("Придбати FunsDiia", callback_data="buy_product")]]
    await query.edit_message_text("Ви на головній сторінці нашого боту. Виберіть опцію нижче.", reply_markup=InlineKeyboardMarkup(keyboard))

async def buy_product(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Відображає список тарифів."""
    query = update.callback_query
    await query.answer()
    
    keyboard = [[InlineKeyboardButton(text, callback_data=f"tariff:{key}")] for key, text in TARIFFS.items()]
    
    tariffs_list = "\n".join([f"• {escape_markdown_v2(text)}" for text in TARIFFS.values()])
    
    message_text = (
        rf"💎 Преміум Додаток \"FunsDiia\"" "\n\n"
        rf"💰 *Тарифи:*" "\n"
        f"{tariffs_list}\n\n"
        rf"⏰ Після вибору тарифу та підтвердження, реквізити будуть відправлені з 10:00 \- 00:00 \." "\n\n"
        rf"*Оберіть необхідний тариф нижче:*"
    )
    
    await query.edit_message_text(message_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="MarkdownV2")


async def select_tariff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обробляє вибір тарифу та запитує ФІО."""
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    
    if ":" not in data:
        # ВИПРАВЛЕНО: Перехід на HTML для надійності
        await query.edit_message_text("Невідомий тариф.")
        return
        
    _, tariff_key = data.split(":", 1)
    
    if not check_request_limit(query.from_user.id):
        # ВИПРАВЛЕНО: Перехід на HTML для надійності
        await query.edit_message_text("Ви перевищили ліміт запитів (10 на годину). Спробуйте пізніше.", parse_mode="HTML")
        return
        
    context.user_data["selected_tariff_key"] = tariff_key
    context.user_data["order_state"] = AWAITING_FIO
    
    selected_tariff_text = TARIFFS.get(tariff_key, 'Невідомий тариф')
    
    # ВИПРАВЛЕНО: Використовуємо HTML для повідомлень клієнту після вибору тарифу
    message_text = (
        f"✅ Ви обрали тариф: <b>{escape_html(selected_tariff_text)}</b>\n\n"
        f"Будь ласка, введіть Ваше <b>повне ім'я, прізвище та по батькові</b> (ФІО) для замовлення:"
    )
    
    await query.edit_message_text(
        message_text,
        parse_mode="HTML"
    )

async def handle_user_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обробляє текстовий ввід користувача (ФІО, ДН)."""
    current_state = context.user_data.get("order_state")
    if current_state is None:
        return
        
    text = update.message.text.strip()
    
    if current_state == AWAITING_FIO:
        context.user_data["fio"] = text
        context.user_data["order_state"] = AWAITING_DOB
        # ВИПРАВЛЕНО: Використовуємо HTML
        await update.message.reply_text("Дякуємо! Тепер введіть  дату народження яку бажаєте використовувати для застосунку FunsDiia в форматі:(ДД.ММ.РРРР).", parse_mode="HTML")
        return
        
    if current_state == AWAITING_DOB:
        context.user_data["dob"] = text
        
        client_id = update.message.from_user.id
        tariff_key = context.user_data.get("selected_tariff_key")
        fio = context.user_data.get("fio")
        dob = context.user_data.get("dob")
        username = update.message.from_user.username or "немає"
        
        ok = add_request(client_id, username=username, tariff_key=tariff_key, fio=fio, dob=dob)
        
        if not ok:
            # ВИПРАВЛЕНО: Використовуємо HTML
            await update.message.reply_text("Помилка при збереженні замовлення. Спробуйте ще раз пізніше.", parse_mode="HTML")
            context.user_data.clear()
            return
            
        context.user_data["order_state"] = AWAITING_PHOTO
        # ВИПРАВЛЕНО: Використовуємо HTML
        await update.message.reply_text(
            "Будь ласка, надішліть фотографію 3×4 (портретне фото).\n\n"
            "Порада: сфотографуйтесь на білому фоні, без зайвих предметів.",
            parse_mode="HTML"
        )
        return

async def handle_all_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обробляє всі вхідні фото та документи, розрізняючи їх за станом користувача."""
    message = update.effective_message
    client_id = message.from_user.id
    
    status = get_order_status(client_id)
    current_state = context.user_data.get("order_state")
    
    is_photo_id_expected = (current_state == AWAITING_PHOTO and status == "waiting_req")

    # --- 1. Обробка ID ФОТО (3x4) --- (ВИКОРИСТАННЯ HTML ДЛЯ БЕЗПЕКИ ID)
    if is_photo_id_expected:
        if not message.photo:
            await message.reply_text("Надішліть, будь ласка, фото у вигляді фото (не як документ) для ID.", parse_mode="HTML")
            return

        last_order = get_last_order_for_client(client_id)
        photo = message.photo[-1]

        if not last_order or not all([last_order.get("fio"), last_order.get("dob"), last_order.get("tariff_text")]):
             await message.reply_text("Будь ласка, спочатку введіть ФІО та дату народження.", parse_mode="HTML")
             context.user_data["order_state"] = AWAITING_FIO
             return
             
        username = message.from_user.username or "немає"
        # Використовуємо HTML для гарантованого копіювання чистого ID
        safe_username = escape_html(f"@{username}") if username != "немає" else "немає"
        safe_fio = escape_html(last_order.get("fio") or "")
        safe_dob = escape_html(last_order.get("dob") or "")
        safe_tariff = escape_html(last_order.get("tariff_text") or "")
        
        caption = (
            f"🖼️ <b>НОВЕ ЗАМОВЛЕННЯ (3x4)</b>\n"
            f"Клієнт ID: <code>{client_id}</code>\n"
            f"Username: @{safe_username}\n"
            f"Тариф: <b>{safe_tariff}</b>\n"
            f"ФІО: <b>{safe_fio}</b>\n"
            f"Дата народження: <b>{safe_dob}</b>\n\n"
            f"АДМИНУ: <code>/send_req {client_id} (реквізити)</code>" # ID захищено тегом <code>
        )

        try:
            file_id = photo.file_id
            await context.bot.send_photo(
                chat_id=ADMIN_CHAT_ID,
                photo=file_id,
                caption=caption,
                parse_mode="HTML" # HTML для адміністратора
            )
        except Exception as e:
            logger.exception("Error forwarding ID photo to admin: %s", e)
            await message.reply_text("Помилка при відправці фото адміністраторам. Спробуйте ще раз.", parse_mode="HTML")
            return

        update_order_status(client_id, "waiting_payment")
        await message.reply_text("Дякуємо, фото отримано. Очікуйте на реквізити для оплати від оператора у робочий час.", parse_mode="HTML")

    # --- 2. Обробка КВИТАНЦІЇ --- (ВИКОРИСТАННЯ HTML ДЛЯ БЕЗПЕКИ ID)
    elif status in ["waiting_payment", "waiting_confirm"]:
        if not (message.photo or message.document):
            await message.reply_text("Надішліть фото або файл квитанції.", parse_mode="HTML")
            return
            
        username = message.from_user.username or "немає"
        safe_username = escape_html(f"@{username}")
        
        caption_text = (
            f"💰 <b>НОВА КВИТАНЦІЯ</b>\n"
            f"Клієнт ID: <code>{client_id}</code>\n"
            f"Username: {safe_username}\n"
            f"Дія: Підтвердіть платіж: <code>/confirm {client_id} ССИЛКА</code>"
        )

        try:
            if message.photo:
                file_id = message.photo[-1].file_id
                await context.bot.send_photo(chat_id=ADMIN_CHAT_ID, photo=file_id, caption=caption_text, parse_mode="HTML")
            elif message.document:
                file_id = message.document.file_id
                await context.bot.send_document(chat_id=ADMIN_CHAT_ID, document=file_id, caption=caption_text, parse_mode="HTML")
            
            update_order_status(client_id, "waiting_confirm")
            await message.reply_text("Ваш платіж перевіряється вручну Це займає приблизно 5-10 хвилин.Вибачте за незручності . Дякуємо!", parse_mode="HTML")

        except Exception as e:
            logger.exception("Error sending payment proof to admin: %s", e)
            await message.reply_text("Помилка при надсиланні квитанції. Спробуйте ще раз.", parse_mode="HTML")

    else:
        # Невідомий/неочікуваний медіа-файл
        await message.reply_text("Неочікуваний медіа-файл. Спробуйте почати замовлення знову /start.", parse_mode="HTML")
        
# -------------------------
# Адмін-команди
# -------------------------
async def send_requisites(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Адмін-команда для надсилання реквізитів клієнту."""
    if not await admin_check(update, context):
        return
        
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("Помилка: Використовуйте формат: /send_req <client_id> <текст реквізитів>")
        return
        
    try:
        client_id = int(args[0].strip())
    except ValueError:
        await update.message.reply_text("Помилка: ID клієнта має бути числом.")
        return
        
    requisites_text = " ".join(args[1:])
    safe_requisites_text = escape_html(requisites_text)
    
    ok = update_order_status(client_id, "waiting_payment")
    if not ok:
        await update.message.reply_text(f"Не знайдено замовлення для оновлення статусу клієнта {client_id}.")
        return
        
    try:
        # Повідомлення клієнту - HTML
        text = f"💳 <b>Ваші реквізити для оплати:</b>\n\n<pre>{safe_requisites_text}</pre>\n\nПісля оплати надішліть будь-ласка скрін оплати."
        await context.bot.send_message(chat_id=client_id, text=text, parse_mode="HTML")
        await update.message.reply_text(f"✅ Реквізити надіслано клієнту {client_id}. Статус оновлено.")
    except Exception as e:
        logger.exception("send_requisites error: %s", e)
        await update.message.reply_text("❌ Помилка: Не вдалося відправити повідомлення клієнту. Можливо, він заблокував бот.")

async def confirm_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Адмін-команда для підтвердження платежу та надсилання посилання."""
    if not await admin_check(update, context):
        return
        
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("Помилка: Використовуйте формат: /confirm <client_id> <посилання_на_товар>")
        return
        
    try:
        client_id = int(args[0].strip())
    except ValueError:
        await update.message.reply_text("Помилка: ID клієнта має бути числом.")
        return
        
    product_link = args[1].strip()
    
    ok = update_order_status(client_id, "completed")
    if not ok:
        await update.message.reply_text(f"Не знайдено замовлення для оновлення статусу клієнта {client_id}.")
        return
        
    # ФІНАЛЬНЕ ВИПРАВЛЕННЯ: Повністю HTML для посилань
    safe_link = escape_html(product_link)
    
    product_message = (
        f"🥳 <b>Ваше замовлення успішно підтверджено!</b>\n\n"
        f"Дякуємо за оплату. Тепер Ви можете завантажити товар за посиланням нижче:\n\n"
        f"🔗 <a href='{safe_link}'>Отримати Товар</a>" # HTML-посилання
    )
    
    try:
        await context.bot.send_message(chat_id=client_id, text=product_message, parse_mode="HTML") # HTML
        await update.message.reply_text(f"✅ Платіж клієнта {client_id} підтверджено. Посилання на товар ({product_link}) надіслано.")
        
    except Exception as e:
        logger.exception("confirm_payment send to client failed: %s", e)
        await update.message.reply_text("❌ Помилка при відправці повідомлення клієнту. Можливо, він заблокував бот.")


# -------------------------
# Запуск бота
# -------------------------
def main():
    if not TOKEN or TOKEN.strip() == "" or TOKEN == DUMMY_PLACEHOLDER:
        logger.error("ERROR: TELEGRAM TOKEN not set.")
        print("ПОМИЛКА: Будь ласка, вставте ваш СПРАВЖНІЙ токен бота у змінну оточення TELEGRAM_BOT_TOKEN або в .env")
        return

    application = Application.builder().token(TOKEN).build()

    # Командные хендлеры
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(start_menu, pattern="^start_menu$"))
    application.add_handler(CallbackQueryHandler(buy_product, pattern="^buy_product$"))
    application.add_handler(CallbackQueryHandler(select_tariff, pattern="^tariff:"))

    # Обробка тексту від користувача
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_user_input))

    # Єдиний хендлер для обробки ВСІХ фото та документів.
    application.add_handler(MessageHandler(filters.PHOTO | filters.Document.ALL, handle_all_media))

    # Адмін-команди
    application.add_handler(CommandHandler("send_req", send_requisites))
    application.add_handler(CommandHandler("confirm", confirm_payment))

    logger.info("Bot starting...")
    print("Bot started...")
    application.run_polling()

if __name__ == "__main__":

    main()
