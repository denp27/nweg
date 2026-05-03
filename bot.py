#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import json
import secrets
from datetime import datetime, timedelta
from collections import defaultdict
from typing import Dict, Optional, Tuple

import pytz
import aiohttp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, InputMediaVideo
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    PreCheckoutQueryHandler, ContextTypes, filters
)

# ========== КОНФИГУРАЦИЯ ==========
BOT_TOKEN = "YOUR_BOT_TOKEN_HERE"  # ЗАМЕНИТЕ
TIMEZONE = pytz.timezone("Europe/Moscow")

# API для платежей
CRYPTOBOT_TOKEN = "YOUR_CRYPTOBOT_TOKEN"
CRYPTOBOT_API_URL = "https://pay.crypt.bot/api"

PLATEGA_API_KEY = "YOUR_PLATEGA_API_KEY"
PLATEGA_SHOP_ID = "YOUR_SHOP_ID"
PLATEGA_API_URL = "https://platega.io/api/v1"

# Файлы для данных
SUBS_FILE = "subscriptions.json"
PENDING_FILE = "pending_payments.json"
CACHE_FILE = "message_cache.json"
MEDIA_CACHE_FILE = "media_cache.json"

# Хранилища
subscriptions: Dict[int, dict] = {}
pending_payments: Dict[str, dict] = {}
message_cache: Dict[int, Dict[int, dict]] = defaultdict(dict)
media_cache: Dict[int, Dict[int, dict]] = defaultdict(dict)
notified_users: Dict[int, dict] = {}

# Цены
PRICES = {
    "1_day": {"stars": 5, "rub": 49, "crypto_usdt": 0.5},
    "7_days": {"stars": 10, "rub": 199, "crypto_usdt": 2},
    "1_month": {"stars": 15, "rub": 399, "crypto_usdt": 5},
}

PLANS = {
    "1_day": timedelta(days=1),
    "7_days": timedelta(days=7),
    "1_month": timedelta(days=30),
}

# ========== ЗАГРУЗКА / СОХРАНЕНИЕ ==========
def load_data():
    global subscriptions, pending_payments, message_cache, media_cache
    try:
        if os.path.exists(SUBS_FILE):
            with open(SUBS_FILE, "r") as f:
                data = json.load(f)
                subscriptions = {}
                for k, v in data.items():
                    subscriptions[int(k)] = {
                        "expiry": datetime.fromisoformat(v["expiry"]),
                        "plan": v.get("plan", "unknown")
                    }
    except:
        pass
    try:
        if os.path.exists(PENDING_FILE):
            with open(PENDING_FILE, "r") as f:
                pending_payments = json.load(f)
    except:
        pass
    try:
        if os.path.exists(CACHE_FILE):
            with open(CACHE_FILE, "r") as f:
                data = json.load(f)
                message_cache = defaultdict(dict)
                for uid, msgs in data.items():
                    message_cache[int(uid)] = {int(mid): msg for mid, msg in msgs.items()}
    except:
        pass
    try:
        if os.path.exists(MEDIA_CACHE_FILE):
            with open(MEDIA_CACHE_FILE, "r") as f:
                data = json.load(f)
                media_cache = defaultdict(dict)
                for uid, msgs in data.items():
                    media_cache[int(uid)] = {int(mid): msg for mid, msg in msgs.items()}
    except:
        pass

def save_data():
    with open(SUBS_FILE, "w") as f:
        data = {str(uid): {"expiry": v["expiry"].isoformat(), "plan": v["plan"]} for uid, v in subscriptions.items()}
        json.dump(data, f, indent=2)
    with open(PENDING_FILE, "w") as f:
        json.dump(pending_payments, f, indent=2)
    with open(CACHE_FILE, "w") as f:
        cache_to_save = {str(uid): {str(mid): msg for mid, msg in msgs.items()} for uid, msgs in message_cache.items()}
        json.dump(cache_to_save, f, indent=2)
    with open(MEDIA_CACHE_FILE, "w") as f:
        media_to_save = {str(uid): {str(mid): msg for mid, msg in msgs.items()} for uid, msgs in media_cache.items()}
        json.dump(media_to_save, f, indent=2)

async def has_subscription(user_id: int) -> Tuple[bool, Optional[str], Optional[datetime]]:
    if user_id not in subscriptions:
        return False, None, None
    sub = subscriptions[user_id]
    if sub["expiry"] > datetime.now(pytz.UTC):
        return True, sub["plan"], sub["expiry"]
    return False, None, None

def activate_subscription(user_id: int, plan: str):
    expiry = datetime.now(pytz.UTC) + PLANS[plan]
    subscriptions[user_id] = {"expiry": expiry, "plan": plan}
    save_data()
    print(f"[ACTIVATE] User {user_id} activated {plan} until {expiry}")

# ========== ПЛАТЕЖИ CRYPTOBOT ==========
async def create_crypto_invoice(amount: float, asset: str, user_id: int, plan: str) -> Tuple[Optional[str], Optional[str]]:
    try:
        url = f"{CRYPTOBOT_API_URL}/createInvoice"
        headers = {"Crypto-Pay-API-Token": CRYPTOBOT_TOKEN}
        params = {
            "asset": asset,
            "amount": str(amount),
            "description": f"Subscription {plan} for user {user_id}",
            "paid_btn_name": "callback",
        }
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, params=params) as resp:
                data = await resp.json()
                if data.get("ok"):
                    invoice = data["result"]
                    invoice_id = str(invoice["invoice_id"])
                    payment_id = f"crypto_{invoice_id}"
                    pending_payments[payment_id] = {
                        "user_id": user_id, "plan": plan, "method": "crypto",
                        "asset": asset, "amount": amount, "invoice_id": invoice_id,
                        "status": "pending", "created_at": datetime.now().isoformat()
                    }
                    save_data()
                    return invoice["bot_invoice_url"], payment_id
    except Exception as e:
        print(f"[CRYPTO] Error: {e}")
    return None, None

async def check_crypto_payment(invoice_id: str) -> bool:
    try:
        url = f"{CRYPTOBOT_API_URL}/getInvoices"
        headers = {"Crypto-Pay-API-Token": CRYPTOBOT_TOKEN}
        params = {"invoice_ids": invoice_id}
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, params=params) as resp:
                data = await resp.json()
                if data.get("ok") and data["result"]["items"]:
                    return data["result"]["items"][0].get("status") == "paid"
    except:
        pass
    return False

# ========== ПЛАТЕЖИ PLATEGA.IO ==========
async def create_platega_payment(amount: float, user_id: int, plan: str) -> Tuple[Optional[str], Optional[str]]:
    try:
        payment_id = f"platega_{secrets.token_hex(8)}"
        url = f"{PLATEGA_API_URL}/invoice/create"
        headers = {"API-Key": PLATEGA_API_KEY, "Content-Type": "application/json"}
        payload = {
            "shop_id": PLATEGA_SHOP_ID, "amount": amount, "currency": "RUB",
            "order_id": payment_id, "description": f"Subscription {plan} for user {user_id}"
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, headers=headers) as resp:
                data = await resp.json()
                if data.get("url"):
                    pending_payments[payment_id] = {
                        "user_id": user_id, "plan": plan, "method": "platega",
                        "amount": amount, "status": "pending", "created_at": datetime.now().isoformat()
                    }
                    save_data()
                    return data["url"], payment_id
    except Exception as e:
        print(f"[PLATEGA] Error: {e}")
    return None, None

async def check_platega_payment(payment_id: str) -> bool:
    try:
        url = f"{PLATEGA_API_URL}/invoice/info"
        headers = {"API-Key": PLATEGA_API_KEY}
        params = {"order_id": payment_id}
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, params=params) as resp:
                data = await resp.json()
                return data.get("status") == "paid"
    except:
        return False

# ========== ФОНОВЫЕ ЗАДАЧИ (через JobQueue) ==========
async def check_expiring_subscriptions(context: ContextTypes.DEFAULT_TYPE):
    """Проверяет подписки и отправляет уведомления"""
    now = datetime.now(pytz.UTC)
    to_remove = []
    
    for user_id, sub in list(subscriptions.items()):
        expiry = sub["expiry"]
        plan = sub["plan"]
        time_left = expiry - now
        hours_left = time_left.total_seconds() / 3600
        
        # Уведомления за час, 30 минут, 10 минут
        if 0.9 < hours_left <= 1.1 and notified_users.get(user_id, {}).get("1h") != expiry.isoformat():
            await context.bot.send_message(
                chat_id=user_id,
                text=f"⏰ Напоминание: Ваша подписка истечёт через 1 час!\nПлан: {plan}\nПродлите подписку через /start"
            )
            if user_id not in notified_users:
                notified_users[user_id] = {}
            notified_users[user_id]["1h"] = expiry.isoformat()
        
        elif 0.45 < hours_left <= 0.55 and notified_users.get(user_id, {}).get("30m") != expiry.isoformat():
            await context.bot.send_message(
                chat_id=user_id,
                text=f"⏰ Напоминание: Ваша подписка истечёт через 30 минут!\nПлан: {plan}\nПродлите подписку через /start"
            )
            if user_id not in notified_users:
                notified_users[user_id] = {}
            notified_users[user_id]["30m"] = expiry.isoformat()
        
        elif 0.15 < hours_left <= 0.2 and notified_users.get(user_id, {}).get("10m") != expiry.isoformat():
            await context.bot.send_message(
                chat_id=user_id,
                text=f"⚠️ Ваша подписка истечёт через 10 минут!\nПлан: {plan}\nСрочно продлите подписку через /start"
            )
            if user_id not in notified_users:
                notified_users[user_id] = {}
            notified_users[user_id]["10m"] = expiry.isoformat()
        
        # Подписка истекла
        elif expiry <= now:
            await context.bot.send_message(
                chat_id=user_id,
                text=f"❌ Ваша подписка истекла!\nПлан: {plan}\nИспользуйте /start для продления"
            )
            to_remove.append(user_id)
    
    for user_id in to_remove:
        del subscriptions[user_id]
        if user_id in notified_users:
            del notified_users[user_id]
    
    if to_remove:
        save_data()

async def check_payments_job(context: ContextTypes.DEFAULT_TYPE):
    """Проверяет ожидающие платежи"""
    to_remove = []
    for payment_id, payment in list(pending_payments.items()):
        user_id = payment["user_id"]
        method = payment["method"]
        
        if method == "stars":
            continue
        
        created_at = datetime.fromisoformat(payment.get("created_at", datetime.now().isoformat()))
        if datetime.now() - created_at > timedelta(days=7):
            to_remove.append(payment_id)
            continue
        
        if method == "crypto" and payment.get("invoice_id"):
            if await check_crypto_payment(payment["invoice_id"]):
                activate_subscription(user_id, payment["plan"])
                to_remove.append(payment_id)
                await context.bot.send_message(
                    chat_id=user_id,
                    text=f"✅ Подписка активирована через CryptoBot!\nПлан: {payment['plan']}"
                )
        
        elif method == "platega":
            if await check_platega_payment(payment_id):
                activate_subscription(user_id, payment["plan"])
                to_remove.append(payment_id)
                await context.bot.send_message(
                    chat_id=user_id,
                    text=f"✅ Подписка активирована через Platega.io!\nПлан: {payment['plan']}"
                )
    
    for payment_id in to_remove:
        if payment_id in pending_payments:
            del pending_payments[payment_id]
    
    if to_remove:
        save_data()

# ========== КЛАВИАТУРЫ ==========
def main_menu():
    buttons = [
        [InlineKeyboardButton("💰 Купить подписку", callback_data="show_tariffs")],
        [InlineKeyboardButton("📸 Удалённые медиа", callback_data="deleted_media")],
        [InlineKeyboardButton("❓ FAQ", callback_data="faq")],
        [InlineKeyboardButton("🔍 Проверить подписку", callback_data="check_sub")],
        [InlineKeyboardButton("🎁 Пробный доступ (2 часа)", callback_data="trial")],
    ]
    return InlineKeyboardMarkup(buttons)

def tariffs_keyboard():
    buttons = [
        [InlineKeyboardButton("📅 1 день - 5⭐ / 49₽ / 0.5 USDT", callback_data="tariff_1_day")],
        [InlineKeyboardButton("📆 7 дней - 10⭐ / 199₽ / 2 USDT", callback_data="tariff_7_days")],
        [InlineKeyboardButton("🗓 1 месяц - 15⭐ / 399₽ / 5 USDT", callback_data="tariff_1_month")],
        [InlineKeyboardButton("🔙 Назад", callback_data="back_to_menu")],
    ]
    return InlineKeyboardMarkup(buttons)

def payment_methods_keyboard(plan: str):
    buttons = [
        [InlineKeyboardButton("⭐ Telegram Stars", callback_data=f"pay_stars_{plan}")],
        [InlineKeyboardButton("🪙 CryptoBot (USDT)", callback_data=f"pay_crypto_{plan}")],
        [InlineKeyboardButton("💳 Platega.io (Карты/СБП)", callback_data=f"pay_platega_{plan}")],
        [InlineKeyboardButton("🔙 Назад", callback_data="show_tariffs")],
    ]
    return InlineKeyboardMarkup(buttons)

def faq_keyboard():
    buttons = [
        [InlineKeyboardButton("📖 Как подключить бота?", callback_data="faq_connect")],
        [InlineKeyboardButton("❌ Как работает удаление?", callback_data="faq_delete")],
        [InlineKeyboardButton("💾 Как сохранить медиа?", callback_data="faq_save")],
        [InlineKeyboardButton("💳 Способы оплаты", callback_data="faq_payment")],
        [InlineKeyboardButton("🔙 Назад", callback_data="back_to_menu")],
    ]
    return InlineKeyboardMarkup(buttons)

def get_media_emoji(media_type: str) -> str:
    emojis = {
        "photo": "📷", "video": "🎥", "video_note": "🔄", "voice": "🎙️",
        "animation": "🎬", "sticker": "🏷️", "timered_photo": "⏰📷", "timered_video": "⏰🎥"
    }
    return emojis.get(media_type, "📎")

# ========== КОМАНДЫ ==========
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    has_sub, plan, expiry = await has_subscription(uid)
    
    bot_info = await context.bot.get_me()
    
    if has_sub:
        await update.message.reply_text(
            f"✅ Активная подписка\n\n"
            f"📌 План: {plan}\n"
            f"⏰ Действует до: {expiry.astimezone(TIMEZONE).strftime('%d.%m.%Y %H:%M:%S')}\n\n"
            f"📌 Бот отслеживает все изменения в чатах\n"
            f"💾 Просто ответьте на любое медиа — оно сохранится!\n"
            f"📸 Удалённые медиа можно посмотреть в меню",
            reply_markup=main_menu()
        )
    else:
        await update.message.reply_text(
            f"👋 Привет! Я бот для Telegram Business\n\n"
            f"🤖 Мои возможности:\n"
            f"✅ Отслеживание удалённых и изменённых сообщений\n"
            f"✅ Сохранение любых медиа (фото, видео, GIF, стикеры)\n"
            f"✅ Просмотр удалённых медиа\n"
            f"✅ Уведомления об окончании подписки\n\n"
            f"📌 **Как подключить:**\n"
            f"1. Настройки Telegram → Telegram для бизнеса\n"
            f"2. Чат-боты → Добавить\n"
            f"3. Введите @{bot_info.username}\n\n"
            f"Выберите действие:",
            reply_markup=main_menu()
        )

async def cmd_faq(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❓ FAQ", reply_markup=faq_keyboard())

async def cmd_check_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_payments = {pid: p for pid, p in pending_payments.items() if p["user_id"] == user_id}
    
    if not user_payments:
        await update.message.reply_text("❌ У вас нет ожидающих платежей")
        return
    
    activated = []
    for payment_id, payment in user_payments.items():
        method = payment["method"]
        if method == "crypto" and payment.get("invoice_id") and await check_crypto_payment(payment["invoice_id"]):
            activate_subscription(user_id, payment["plan"])
            activated.append(payment_id)
            await update.message.reply_text(f"✅ Подписка активирована через CryptoBot!\nПлан: {payment['plan']}")
        elif method == "platega" and await check_platega_payment(payment_id):
            activate_subscription(user_id, payment["plan"])
            activated.append(payment_id)
            await update.message.reply_text(f"✅ Подписка активирована через Platega.io!\nПлан: {payment['plan']}")
        elif method == "stars":
            await update.message.reply_text(f"⭐ Платёж через Stars ожидает оплаты\nID: {payment_id}")
    
    for payment_id in activated:
        if payment_id in pending_payments:
            del pending_payments[payment_id]
    
    if activated:
        save_data()

# ========== ПРОСМОТР УДАЛЁННЫХ МЕДИА ==========
async def show_deleted_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    
    has_sub, _, _ = await has_subscription(user_id)
    if not has_sub:
        await query.edit_message_text("❌ Нет активной подписки для просмотра удалённых медиа")
        return
    
    if user_id not in media_cache or not media_cache[user_id]:
        await query.edit_message_text("📭 У вас пока нет сохранённых удалённых медиа", reply_markup=main_menu())
        return
    
    buttons = []
    for msg_id, media in list(media_cache[user_id].items())[:20]:
        media_type = media.get("type", "unknown")
        date = datetime.fromisoformat(media["date"]).astimezone(TIMEZONE).strftime('%d.%m %H:%M')
        buttons.append([InlineKeyboardButton(
            f"{get_media_emoji(media_type)} {media_type} от {date}",
            callback_data=f"view_media_{msg_id}"
        )])
    
    buttons.append([InlineKeyboardButton("🔙 Назад", callback_data="back_to_menu")])
    await query.edit_message_text(
        "📸 **Удалённые медиа**\n\nВыберите файл для просмотра:",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode="Markdown"
    )

async def view_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    msg_id = int(query.data.replace("view_media_", ""))
    
    if user_id not in media_cache or msg_id not in media_cache[user_id]:
        await query.edit_message_text("❌ Медиа не найдено", reply_markup=main_menu())
        return
    
    media = media_cache[user_id][msg_id]
    media_type = media.get("type")
    file_path = media.get("file_path")
    caption = f"📸 Удалённое медиа\n📅 Получено: {datetime.fromisoformat(media['date']).astimezone(TIMEZONE).strftime('%d.%m.%Y %H:%M:%S')}"
    if "deleted_date" in media:
        caption += f"\n🗑 Удалено: {datetime.fromisoformat(media['deleted_date']).astimezone(TIMEZONE).strftime('%d.%m.%Y %H:%M:%S')}"
    
    try:
        if media_type in ["photo", "timered_photo"]:
            with open(file_path, 'rb') as f:
                await query.edit_message_media(InputMediaPhoto(media=f, caption=caption))
        elif media_type in ["video", "video_note", "timered_video"]:
            with open(file_path, 'rb') as f:
                await query.edit_message_media(InputMediaVideo(media=f, caption=caption))
        elif media_type == "animation":
            with open(file_path, 'rb') as f:
                await context.bot.send_animation(chat_id=user_id, animation=f, caption=caption)
            await query.delete_message()
        elif media_type == "sticker":
            with open(file_path, 'rb') as f:
                await context.bot.send_sticker(chat_id=user_id, sticker=f)
            await query.delete_message()
        elif media_type == "voice":
            with open(file_path, 'rb') as f:
                await context.bot.send_voice(chat_id=user_id, voice=f, caption=caption)
            await query.delete_message()
        
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 К списку", callback_data="deleted_media")]])
        await context.bot.send_message(chat_id=user_id, text="Выберите действие:", reply_markup=keyboard)
    
    except Exception as e:
        print(f"[VIEW_MEDIA] Error: {e}")
        await query.edit_message_text("❌ Ошибка загрузки медиа", reply_markup=main_menu())

# ========== УНИВЕРСАЛЬНЫЙ ОБРАБОТЧИК BUSINESS API ==========
async def handle_all_updates(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # 1. Подключение бота
    if hasattr(update, 'business_connection') and update.business_connection:
        conn = update.business_connection
        user_id = conn.user.id
        chat_id = conn.user_chat_id
        
        print(f"[BUSINESS] Bot connected to user {user_id}")
        
        await context.bot.send_message(
            chat_id=chat_id,
            text="🤖 **Бот успешно подключен к вашему бизнес-аккаунту!**\n\n"
                 "Теперь я буду:\n"
                 "✅ Отслеживать удалённые и изменённые сообщения\n"
                 "✅ Сохранять все медиа (фото, видео, GIF, стикеры)\n"
                 "✅ Сохранять одноразовые фото и видео\n"
                 "✅ Присылать копии удалённых сообщений\n\n"
                 "💡 **Совет:** Просто ответьте на любое сообщение с медиа, чтобы сохранить его!\n\n"
                 "Используйте /start для покупки подписки",
            parse_mode="Markdown"
        )
        
        has_sub, _, _ = await has_subscription(user_id)
        if not has_sub:
            await context.bot.send_message(
                chat_id=chat_id,
                text="⚡️ **Для полного функционала необходима подписка.**\n\n"
                     "🎁 У вас есть **2 часа пробного доступа**!\n"
                     "Используйте /start и нажмите «Пробный доступ»",
                parse_mode="Markdown"
            )
        return
    
    # 2. Кэширование новых сообщений
    if hasattr(update, 'business_message') and update.business_message:
        msg = update.business_message
        user_id = msg.from_user.id
        
        has_sub, _, _ = await has_subscription(user_id)
        if not has_sub:
            return
        
        # Кэшируем текст
        if msg.text or msg.caption:
            message_cache[user_id][msg.message_id] = {
                "text": msg.text or msg.caption or "",
                "date": msg.date.isoformat(),
                "chat_id": msg.chat_id
            }
        
        # Сохраняем медиа
        media_saved = False
        media_type = None
        file_path = None
        
        os.makedirs(f"media/{user_id}", exist_ok=True)
        
        if msg.photo:
            file = await msg.photo[-1].get_file()
            file_path = f"media/{user_id}/photo_{msg.message_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jpg"
            await file.download_to_drive(file_path)
            media_type = "photo"
            media_saved = True
        elif msg.video:
            file = await msg.video.get_file()
            file_path = f"media/{user_id}/video_{msg.message_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.mp4"
            await file.download_to_drive(file_path)
            media_type = "video"
            media_saved = True
        elif msg.video_note:
            file = await msg.video_note.get_file()
            file_path = f"media/{user_id}/videonote_{msg.message_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.mp4"
            await file.download_to_drive(file_path)
            media_type = "video_note"
            media_saved = True
        elif msg.voice:
            file = await msg.voice.get_file()
            file_path = f"media/{user_id}/voice_{msg.message_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.ogg"
            await file.download_to_drive(file_path)
            media_type = "voice"
            media_saved = True
        elif msg.animation:
            file = await msg.animation.get_file()
            file_path = f"media/{user_id}/gif_{msg.message_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.gif"
            await file.download_to_drive(file_path)
            media_type = "animation"
            media_saved = True
        elif msg.sticker:
            file = await msg.sticker.get_file()
            ext = "webp"
            if msg.sticker.is_animated:
                ext = "tgs"
            elif msg.sticker.is_video:
                ext = "webm"
            file_path = f"media/{user_id}/sticker_{msg.message_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.{ext}"
            await file.download_to_drive(file_path)
            media_type = "sticker"
            media_saved = True
        
        if media_saved and file_path:
            media_cache[user_id][msg.message_id] = {
                "type": media_type,
                "file_path": file_path,
                "date": msg.date.isoformat(),
                "chat_id": msg.chat_id
            }
            save_data()
            print(f"[BUSINESS] Saved {media_type} from user {user_id}")
        
        save_data()
        return
    
    # 3. Обработка изменённых сообщений
    if hasattr(update, 'edited_business_message') and update.edited_business_message:
        edited = update.edited_business_message
        user_id = edited.from_user.id
        msg_id = edited.message_id
        new_text = edited.text or edited.caption or ""
        
        has_sub, _, _ = await has_subscription(user_id)
        if has_sub and user_id in message_cache and msg_id in message_cache[user_id]:
            old_text = message_cache[user_id][msg_id]["text"]
            if old_text and old_text != new_text:
                report = (
                    f"✏️ **Вы изменили сообщение**\n\n"
                    f"❌ Было: {old_text[:300]}\n"
                    f"🔘 Стало: {new_text[:300]}\n"
                    f"🕐 Время: {datetime.now(TIMEZONE).strftime('%H:%M:%S')}"
                )
                await context.bot.send_message(chat_id=edited.chat_id, text=report, parse_mode="Markdown")
                message_cache[user_id][msg_id]["text"] = new_text
                save_data()
        return
    
    # 4. Обработка удалённых сообщений
    if hasattr(update, 'deleted_business_messages') and update.deleted_business_messages:
        deleted = update.deleted_business_messages
        user_id = None
        
        if hasattr(deleted, 'from_user') and deleted.from_user:
            user_id = deleted.from_user.id
        
        has_sub, _, _ = await has_subscription(user_id) if user_id else (False, None, None)
        
        if user_id and has_sub:
            chat_id = deleted.chat.id
            
            for msg_id in deleted.message_ids:
                if user_id in message_cache and msg_id in message_cache[user_id]:
                    cached = message_cache[user_id][msg_id]
                    report = (
                        f"🗑 **Вы удалили сообщение**\n\n"
                        f"📝 Текст: {cached['text'][:300] if cached['text'] else '[нет текста]'}\n"
                        f"📅 Отправлено: {datetime.fromisoformat(cached['date']).astimezone(TIMEZONE).strftime('%d.%m.%Y %H:%M:%S')}\n"
                        f"🗑 Удалено: {datetime.now(TIMEZONE).strftime('%d.%m.%Y %H:%M:%S')}"
                    )
                    await context.bot.send_message(chat_id=chat_id, text=report, parse_mode="Markdown")
                
                if user_id in media_cache and msg_id in media_cache[user_id]:
                    media_cache[user_id][msg_id]["deleted_date"] = datetime.now().isoformat()
                    save_data()
                    
                    media_type = media_cache[user_id][msg_id].get("type", "медиа")
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=f"📸 **{get_media_emoji(media_type)} Удалённое {media_type} сохранено**\n\n"
                             f"Вы можете посмотреть его в меню «Удалённые медиа»"
                    )
            
            print(f"[BUSINESS] Processed {len(deleted.message_ids)} deleted messages")

# ========== АВТОСОХРАНЕНИЕ МЕДИА ==========
async def auto_save_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.reply_to_message:
        return
    
    user_id = update.effective_user.id
    has_sub, _, _ = await has_subscription(user_id)
    if not has_sub:
        await update.message.reply_text("❌ Нет активной подписки для сохранения медиа.\nИспользуйте /start")
        return
    
    reply = update.message.reply_to_message
    os.makedirs(f"media/{user_id}", exist_ok=True)
    
    saved = False
    media_type = ""
    
    try:
        if reply.photo:
            file = await reply.photo[-1].get_file()
            await file.download_to_drive(f"media/{user_id}/saved_photo_{reply.message_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jpg")
            saved = True
            media_type = "фото"
        elif reply.video:
            file = await reply.video.get_file()
            await file.download_to_drive(f"media/{user_id}/saved_video_{reply.message_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.mp4")
            saved = True
            media_type = "видео"
        elif reply.voice:
            file = await reply.voice.get_file()
            await file.download_to_drive(f"media/{user_id}/saved_voice_{reply.message_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.ogg")
            saved = True
            media_type = "голосовое"
        elif reply.video_note:
            file = await reply.video_note.get_file()
            await file.download_to_drive(f"media/{user_id}/saved_videonote_{reply.message_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.mp4")
            saved = True
            media_type = "кружок"
        elif reply.animation:
            file = await reply.animation.get_file()
            await file.download_to_drive(f"media/{user_id}/saved_gif_{reply.message_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.gif")
            saved = True
            media_type = "GIF"
        elif reply.sticker:
            file = await reply.sticker.get_file()
            ext = "webp"
            if reply.sticker.is_animated:
                ext = "tgs"
            elif reply.sticker.is_video:
                ext = "webm"
            await file.download_to_drive(f"media/{user_id}/saved_sticker_{reply.message_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.{ext}")
            saved = True
            media_type = "стикер"
    except Exception as e:
        print(f"[SAVE_MEDIA] Error: {e}")
    
    if saved:
        await update.message.reply_text(
            f"✅ **{media_type} сохранено!**\n\n"
            f"📅 Время: {datetime.now(TIMEZONE).strftime('%d.%m.%Y %H:%M:%S')}"
        )

# ========== CALLBACK HANDLER ==========
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    uid = query.from_user.id

    try:
        if data == "show_tariffs":
            await query.edit_message_text("💰 Выберите тариф:", reply_markup=tariffs_keyboard())
        
        elif data == "deleted_media":
            await show_deleted_media(update, context)
        
        elif data.startswith("view_media_"):
            await view_media(update, context)
        
        elif data == "back_to_menu":
            await query.edit_message_text("📋 Главное меню:", reply_markup=main_menu())
        
        elif data == "faq":
            await query.edit_message_text("❓ FAQ\n\nВыберите вопрос:", reply_markup=faq_keyboard())
        
        elif data == "check_sub":
            has_sub, plan, expiry = await has_subscription(uid)
            if has_sub:
                await query.edit_message_text(
                    f"✅ **Активная подписка**\n\n"
                    f"📌 План: {plan}\n"
                    f"⏰ Действует до: {expiry.astimezone(TIMEZONE).strftime('%d.%m.%Y %H:%M:%S')}",
                    reply_markup=main_menu(),
                    parse_mode="Markdown"
                )
            else:
                await query.edit_message_text("❌ Нет активной подписки\n\nИспользуйте /start для покупки", reply_markup=main_menu())
        
        elif data == "trial":
            has_sub, _, _ = await has_subscription(uid)
            if has_sub:
                await query.edit_message_text("⚠️ У вас уже есть активная подписка!", reply_markup=main_menu())
                return
            expiry = datetime.now(pytz.UTC) + timedelta(hours=2)
            subscriptions[uid] = {"expiry": expiry, "plan": "trial"}
            save_data()
            await query.edit_message_text(
                f"🎁 **Пробный доступ активирован!**\n\n"
                f"⏰ Действует до: {expiry.astimezone(TIMEZONE).strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                f"📌 Теперь подключите бота в Telegram для бизнеса:\n"
                f"1. Настройки → Telegram для бизнеса\n"
                f"2. Чат-боты → Добавить\n"
                f"3. Введите @{(await context.bot.get_me()).username}",
                reply_markup=main_menu(),
                parse_mode="Markdown"
            )
        
        elif data.startswith("faq_"):
            texts = {
                "connect": "🔌 **Подключение бота:**\n\n1. Настройки Telegram\n2. Telegram для бизнеса\n3. Чат-боты\n4. Добавить бота\n5. Введите @username бота",
                "delete": "❌ **Отслеживание удалений:**\n\nБот автоматически кэширует все сообщения и медиа. При удалении вы получите точную копию.",
                "save": "💾 **Сохранение медиа:**\n\nПросто ответьте на любое сообщение с медиа — бот сохранит его автоматически!",
                "payment": "💳 **Способы оплаты:**\n\n• ⭐ Telegram Stars\n• 🪙 CryptoBot (USDT)\n• 💳 Platega.io (карты РФ, СБП)",
            }
            topic = data.replace("faq_", "")
            await query.edit_message_text(texts.get(topic, "Ответ не найден"), reply_markup=faq_keyboard())
        
        elif data.startswith("tariff_"):
            plan = data.replace("tariff_", "")
            await query.edit_message_text(
                f"💰 **Тариф {plan.replace('_', ' ')}**\n\n"
                f"⭐ Stars: {PRICES[plan]['stars']}\n"
                f"🪙 USDT: {PRICES[plan]['crypto_usdt']}\n"
                f"💳 Карты/СБП: {PRICES[plan]['rub']}₽\n\n"
                f"Выберите способ оплаты:",
                reply_markup=payment_methods_keyboard(plan),
                parse_mode="Markdown"
            )
        
        elif data.startswith("pay_"):
            parts = data.split("_")
            method = parts[1]
            plan = "_".join(parts[2:])
            
            if method == "stars":
                payment_id = secrets.token_hex(8)
                pending_payments[payment_id] = {"user_id": uid, "plan": plan, "method": "stars", "created_at": datetime.now().isoformat()}
                save_data()
                await context.bot.send_invoice(
                    chat_id=uid,
                    title=f"Подписка {plan.replace('_', ' ')}",
                    description=f"Доступ на {PLANS[plan].days} дней",
                    payload=f"sub:{plan}:{uid}:{payment_id}",
                    provider_token="",
                    currency="XTR",
                    prices=[{"label": "Подписка", "amount": PRICES[plan]["stars"]}],
                    start_parameter="sub",
                )
                await query.edit_message_text("⭐ Счёт на оплату Stars отправлен!\n\nОплатите в диалоге с ботом.")
            
            elif method == "crypto":
                amount = PRICES[plan]["crypto_usdt"]
                url, payment_id = await create_crypto_invoice(amount, "USDT", uid, plan)
                if url:
                    await query.edit_message_text(
                        f"🪙 **Оплата через CryptoBot**\n\n"
                        f"Сумма: {amount} USDT\n"
                        f"[Оплатить]({url})\n\n"
                        f"ID: `{payment_id}`",
                        parse_mode="Markdown"
                    )
                else:
                    await query.edit_message_text("❌ Ошибка создания счёта в CryptoBot")
            
            elif method == "platega":
                amount = PRICES[plan]["rub"]
                url, payment_id = await create_platega_payment(amount, uid, plan)
                if url:
                    await query.edit_message_text(
                        f"💳 **Оплата через Platega.io**\n\n"
                        f"Сумма: {amount} ₽\n"
                        f"[Оплатить]({url})\n\n"
                        f"ID: `{payment_id}`",
                        parse_mode="Markdown"
                    )
                else:
                    await query.edit_message_text("❌ Ошибка создания счёта в Platega.io")
    
    except Exception as e:
        print(f"[CALLBACK] Error: {e}")
        await query.edit_message_text("❌ Произошла ошибка. Попробуйте ещё раз.", reply_markup=main_menu())

# ========== ПЛАТЕЖИ STARS ==========
async def pre_checkout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.pre_checkout_query.answer(ok=True)

async def successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    payload = update.message.successful_payment.invoice_payload
    parts = payload.split(":")
    if len(parts) == 4:
        plan = parts[1]
        uid = int(parts[2])
        payment_id = parts[3]
        activate_subscription(uid, plan)
        if payment_id in pending_payments:
            del pending_payments[payment_id]
            save_data()
        await update.message.reply_text(
            f"✅ **Подписка активирована через Stars!**\n\n"
            f"📌 План: {plan.replace('_', ' ')}\n"
            f"⏰ Действует до: {subscriptions[uid]['expiry'].astimezone(TIMEZONE).strftime('%d.%m.%Y %H:%M:%S')}"
        )

# ========== ОСНОВНАЯ ФУНКЦИЯ ==========
def main():
    load_data()
    os.makedirs("media", exist_ok=True)
    
    app = Application.builder().token(BOT_TOKEN).build()
    
    # Команды
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("faq", cmd_faq))
    app.add_handler(CommandHandler("check_payment", cmd_check_payment))
    
    # Платежи Stars
    app.add_handler(PreCheckoutQueryHandler(pre_checkout))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))
    
    # Кнопки
    app.add_handler(CallbackQueryHandler(handle_callback))
    
    # Универсальный обработчик бизнес-событий
    app.add_handler(MessageHandler(filters.ALL, handle_all_updates))
    
    # Автосохранение медиа при ответе
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, auto_save_media))
    
    # JobQueue для фоновых задач
    job_queue = app.job_queue
    if job_queue:
        job_queue.run_repeating(check_expiring_subscriptions, interval=60, first=10)
        job_queue.run_repeating(check_payments_job, interval=30, first=5)
        print("[JOB] Scheduled background tasks")
    else:
        print("[WARNING] JobQueue not available, install: pip install python-telegram-bot[job-queue]")
    
    print("=" * 60)
    print("✅ БОТ ЗАПУЩЕН!")
    print("=" * 60)
    print("📌 ФУНКЦИОНАЛ:")
    print("   1. Полная проверка платежей (Stars, CryptoBot, Platega.io)")
    print("   2. Уведомления об окончании подписки (за час, 30 мин, 10 мин)")
    print("   3. Сохранение всех типов медиа")
    print("   4. Сохранение одноразовых фото и видео с таймером")
    print("   5. Просмотр удалённых медиа в меню")
    print("   6. Отслеживание изменённых и удалённых сообщений")
    print("=" * 60)
    
    app.run_polling()

if __name__ == "__main__":
    main()in()
