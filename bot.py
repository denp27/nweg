#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import json
import secrets
import asyncio
from datetime import datetime, timedelta
from collections import defaultdict
from typing import Dict, Optional, Tuple

import pytz
import aiohttp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    PreCheckoutQueryHandler, ContextTypes, filters
)

# ========== КОНФИГУРАЦИЯ ==========
BOT_TOKEN = "YOUR_BOT_TOKEN_HERE"  # ЗАМЕНИТЕ НА ВАШ ТОКЕН
TIMEZONE = pytz.timezone("Europe/Moscow")

# API ключи (оставьте пустыми, если не используете)
CRYPTOBOT_TOKEN = "YOUR_CRYPTOBOT_TOKEN"
CRYPTOBOT_API_URL = "https://pay.crypt.bot/api"

PLATEGA_API_KEY = "YOUR_PLATEGA_API_KEY"
PLATEGA_SHOP_ID = "YOUR_SHOP_ID"
PLATEGA_API_URL = "https://platega.io/api/v1"

SUBS_FILE = "subscriptions.json"
PENDING_FILE = "pending_payments.json"
CACHE_FILE = "message_cache.json"

subscriptions: Dict[int, datetime] = {}
pending_payments: Dict[str, dict] = {}
message_cache: Dict[int, Dict[int, dict]] = defaultdict(dict)

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
    global subscriptions, pending_payments, message_cache
    try:
        if os.path.exists(SUBS_FILE):
            with open(SUBS_FILE, "r") as f:
                data = json.load(f)
                subscriptions = {int(k): datetime.fromisoformat(v) for k, v in data.items()}
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

def save_data():
    with open(SUBS_FILE, "w") as f:
        data = {str(uid): exp.isoformat() for uid, exp in subscriptions.items()}
        json.dump(data, f, indent=2)
    with open(PENDING_FILE, "w") as f:
        json.dump(pending_payments, f, indent=2)
    with open(CACHE_FILE, "w") as f:
        cache_to_save = {str(uid): {str(mid): msg for mid, msg in msgs.items()} for uid, msgs in message_cache.items()}
        json.dump(cache_to_save, f, indent=2)

async def has_subscription(user_id: int) -> bool:
    if user_id not in subscriptions:
        return False
    return subscriptions[user_id] > datetime.now(pytz.UTC)

def activate_subscription(user_id: int, plan: str):
    expiry = datetime.now(pytz.UTC) + PLANS[plan]
    subscriptions[user_id] = expiry
    save_data()

# ========== ПЛАТЕЖИ (упрощённо) ==========
async def create_crypto_invoice(amount: float, asset: str, user_id: int, plan: str) -> Tuple[Optional[str], Optional[str]]:
    try:
        url = f"{CRYPTOBOT_API_URL}/createInvoice"
        headers = {"Crypto-Pay-API-Token": CRYPTOBOT_TOKEN}
        params = {"asset": asset, "amount": str(amount), "description": f"Subscription {plan}"}
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, params=params) as resp:
                data = await resp.json()
                if data.get("ok"):
                    invoice = data["result"]
                    invoice_id = str(invoice["invoice_id"])
                    payment_id = f"crypto_{invoice_id}"
                    pending_payments[payment_id] = {"user_id": user_id, "plan": plan, "method": "crypto", "invoice_id": invoice_id}
                    save_data()
                    return invoice["bot_invoice_url"], payment_id
    except:
        pass
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

async def create_platega_payment(amount: float, user_id: int, plan: str) -> Tuple[Optional[str], Optional[str]]:
    try:
        payment_id = f"platega_{secrets.token_hex(8)}"
        url = f"{PLATEGA_API_URL}/invoice/create"
        headers = {"API-Key": PLATEGA_API_KEY, "Content-Type": "application/json"}
        payload = {"shop_id": PLATEGA_SHOP_ID, "amount": amount, "currency": "RUB", "order_id": payment_id, "description": f"Subscription {plan}"}
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, headers=headers) as resp:
                data = await resp.json()
                if data.get("url"):
                    pending_payments[payment_id] = {"user_id": user_id, "plan": plan, "method": "platega"}
                    save_data()
                    return data["url"], payment_id
    except:
        pass
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

# ========== ФОНОВАЯ ПРОВЕРКА ПЛАТЕЖЕЙ ==========
async def payment_checker_job(app: Application):
    """Фоновая проверка платежей"""
    while True:
        try:
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
                
                if method == "crypto":
                    invoice_id = payment.get("invoice_id")
                    if invoice_id and await check_crypto_payment(invoice_id):
                        activate_subscription(user_id, payment["plan"])
                        to_remove.append(payment_id)
                        await app.bot.send_message(chat_id=user_id, text=f"✅ Подписка активирована через CryptoBot!\nПлан: {payment['plan']}")
                
                elif method == "platega":
                    if await check_platega_payment(payment_id):
                        activate_subscription(user_id, payment["plan"])
                        to_remove.append(payment_id)
                        await app.bot.send_message(chat_id=user_id, text=f"✅ Подписка активирована через Platega.io!\nПлан: {payment['plan']}")
            
            for payment_id in to_remove:
                if payment_id in pending_payments:
                    del pending_payments[payment_id]
            
            if to_remove:
                save_data()
        except Exception as e:
            print(f"[CHECKER] Error: {e}")
        
        await asyncio.sleep(30)

def start_background_checker(app: Application):
    asyncio.create_task(payment_checker_job(app))

# ========== КЛАВИАТУРЫ ==========
def main_menu():
    buttons = [
        [InlineKeyboardButton("💰 Купить подписку", callback_data="show_tariffs")],
        [InlineKeyboardButton("❓ FAQ", callback_data="faq")],
        [InlineKeyboardButton("🔍 Проверить подписку", callback_data="check_sub")],
        [InlineKeyboardButton("🎁 Пробный доступ (2 часа)", callback_data="trial")],
    ]
    return InlineKeyboardMarkup(buttons)

def tariffs_keyboard():
    buttons = [
        [InlineKeyboardButton("1 день - 5⭐ / 49₽ / 0.5 USDT", callback_data="tariff_1_day")],
        [InlineKeyboardButton("7 дней - 10⭐ / 199₽ / 2 USDT", callback_data="tariff_7_days")],
        [InlineKeyboardButton("1 месяц - 15⭐ / 399₽ / 5 USDT", callback_data="tariff_1_month")],
        [InlineKeyboardButton("🔙 Назад", callback_data="back_to_menu")],
    ]
    return InlineKeyboardMarkup(buttons)

def payment_methods_keyboard(plan: str):
    buttons = [
        [InlineKeyboardButton("⭐ Telegram Stars", callback_data=f"pay_stars_{plan}")],
        [InlineKeyboardButton("🪙 CryptoBot (USDT)", callback_data=f"pay_crypto_{plan}")],
        [InlineKeyboardButton("💳 Platega.io", callback_data=f"pay_platega_{plan}")],
        [InlineKeyboardButton("🔙 Назад", callback_data="show_tariffs")],
    ]
    return InlineKeyboardMarkup(buttons)

def faq_keyboard():
    buttons = [
        [InlineKeyboardButton("Как подключить бота?", callback_data="faq_connect")],
        [InlineKeyboardButton("Как работает удаление?", callback_data="faq_delete")],
        [InlineKeyboardButton("Как сохранить медиа?", callback_data="faq_save")],
        [InlineKeyboardButton("Способы оплаты", callback_data="faq_payment")],
        [InlineKeyboardButton("🔙 Назад", callback_data="back_to_menu")],
    ]
    return InlineKeyboardMarkup(buttons)

# ========== КОМАНДЫ ==========
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if await has_subscription(uid):
        until = subscriptions[uid].astimezone(TIMEZONE)
        await update.message.reply_text(
            f"✅ Активная подписка\n\nДействует до: {until.strftime('%d.%m.%Y %H:%M:%S')}\n\n"
            f"📌 Бот отслеживает все изменения в чатах\n"
            f"💾 Просто ответьте на любое медиа — оно сохранится автоматически!",
            reply_markup=main_menu()
        )
    else:
        await update.message.reply_text(
            "👋 Приватный бот для Business аккаунтов\n\n"
            "✅ Отслеживает удалённые и изменённые сообщения\n"
            "✅ Сохраняет медиа (ответьте на сообщение)\n\n"
            "Выберите действие:",
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
        if method == "crypto" and await check_crypto_payment(payment.get("invoice_id", "")):
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
    
    if not activated and user_payments:
        text = "💰 Ожидающие платежи:\n" + "\n".join([f"• {pid}: {p['method']} - {p['plan']}" for pid, p in user_payments.items()])
        await update.message.reply_text(text)

# ========== ОБРАБОТЧИКИ BUSINESS API ==========

async def handle_business_connection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик подключения бота к бизнес-аккаунту"""
    if not update.business_connection:
        return
    
    conn = update.business_connection
    user_id = conn.user.id
    chat_id = conn.user_chat_id
    
    # Проверяем права бота
    can_reply = getattr(conn, 'can_reply', False)
    
    print(f"[BUSINESS] Bot connected to user {user_id}")
    print(f"[BUSINESS] Can reply: {can_reply}")
    
    # Отправляем приветствие в чат, где подключили бота
    await context.bot.send_message(
        chat_id=chat_id,
        text="🤖 Бот успешно подключен к вашему бизнес-аккаунту!\n\n"
             "Теперь я буду отслеживать изменения и удаления сообщений.\n"
             "Чтобы сохранить медиа - просто ответьте на сообщение."
    )

async def handle_business_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Кэширование новых сообщений из бизнес-чатов"""
    if not update.business_message:
        return
    
    msg = update.business_message
    user_id = msg.from_user.id
    chat_id = msg.chat_id
    
    # Получаем информацию о подключении
    conn = await context.bot.get_business_connection(
        business_connection_id=msg.business_connection_id
    )
    business_owner_id = conn.user.id
    
    # Кэшируем сообщения ТОЛЬКО от владельца бизнес-аккаунта
    # (сообщения от клиентов мы не можем кэшировать этично)
    if user_id == business_owner_id:
        if await has_subscription(user_id):
            message_cache[user_id][msg.message_id] = {
                "text": msg.text or msg.caption or "",
                "date": msg.date.isoformat(),
                "chat_id": chat_id
            }
            save_data()
            print(f"[BUSINESS] Cached message {msg.message_id} from business owner")
    else:
        # Сообщение от клиента - уведомляем владельца
        if await has_subscription(business_owner_id):
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"📨 Новое сообщение от клиента:\n\n{msg.text[:200] if msg.text else '[медиа]'}"
            )

async def handle_edited_business_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка изменённых сообщений в бизнес-чатах"""
    if not update.edited_business_message:
        return
    
    edited = update.edited_business_message
    user_id = edited.from_user.id
    msg_id = edited.message_id
    new_text = edited.text or edited.caption or ""
    
    # Получаем информацию о подключении
    conn = await context.bot.get_business_connection(
        business_connection_id=edited.business_connection_id
    )
    business_owner_id = conn.user.id
    
    if user_id == business_owner_id and await has_subscription(user_id):
        if user_id in message_cache and msg_id in message_cache[user_id]:
            old_text = message_cache[user_id][msg_id]["text"]
            if old_text and old_text != new_text:
                report = (
                    f"✏️ Вы изменили сообщение\n\n"
                    f"❌ Было: {old_text[:200]}\n"
                    f"🔘 Стало: {new_text[:200]}\n"
                    f"🕐 Время: {datetime.now(TIMEZONE).strftime('%H:%M:%S')}"
                )
                await context.bot.send_message(chat_id=edited.chat_id, text=report)
                message_cache[user_id][msg_id]["text"] = new_text
                save_data()

async def handle_deleted_business_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка удалённых сообщений в бизнес-чатах"""
    if not update.deleted_business_messages:
        return
    
    deleted = update.deleted_business_messages
    business_connection_id = deleted.business_connection_id
    
    # Получаем информацию о подключении
    conn = await context.bot.get_business_connection(
        business_connection_id=business_connection_id
    )
    business_owner_id = conn.user.id
    
    if not await has_subscription(business_owner_id):
        return
    
    chat_id = deleted.chat.id
    
    for msg_id in deleted.message_ids:
        if business_owner_id in message_cache and msg_id in message_cache[business_owner_id]:
            cached = message_cache[business_owner_id][msg_id]
            report = (
                f"🗑 Вы удалили сообщение\n\n"
                f"📝 Текст: {cached['text'][:200] if cached['text'] else '[нет текста]'}\n"
                f"📅 Отправлено: {datetime.fromisoformat(cached['date']).astimezone(TIMEZONE).strftime('%d.%m.%Y %H:%M:%S')}\n"
                f"🗑 Удалено: {datetime.now(TIMEZONE).strftime('%d.%m.%Y %H:%M:%S')}"
            )
            await context.bot.send_message(chat_id=chat_id, text=report)
            print(f"[BUSINESS] Notified about deleted message {msg_id}")

# ========== АВТОСОХРАНЕНИЕ МЕДИА ==========
async def auto_save_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.reply_to_message:
        return
    
    user_id = update.effective_user.id
    if not await has_subscription(user_id):
        await update.message.reply_text("❌ Нет активной подписки для сохранения медиа")
        return
    
    reply = update.message.reply_to_message
    os.makedirs("media", exist_ok=True)
    
    saved = False
    if reply.photo:
        file = await reply.photo[-1].get_file()
        await file.download_to_drive(f"media/photo_{reply.message_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jpg")
        saved = True
    elif reply.video:
        file = await reply.video.get_file()
        await file.download_to_drive(f"media/video_{reply.message_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.mp4")
        saved = True
    elif reply.voice:
        file = await reply.voice.get_file()
        await file.download_to_drive(f"media/voice_{reply.message_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.ogg")
        saved = True
    elif reply.video_note:
        file = await reply.video_note.get_file()
        await file.download_to_drive(f"media/videonote_{reply.message_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.mp4")
        saved = True
    
    if saved:
        await update.message.reply_text(f"✅ Медиа сохранено!\nВремя: {datetime.now(TIMEZONE).strftime('%H:%M:%S')}")

# ========== CALLBACK HANDLER ==========
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    uid = query.from_user.id

    try:
        if data == "show_tariffs":
            await query.edit_message_text("💰 Выберите тариф:", reply_markup=tariffs_keyboard())
        elif data == "back_to_menu":
            await query.edit_message_text("📋 Главное меню:", reply_markup=main_menu())
        elif data == "faq":
            await query.edit_message_text("❓ FAQ - Выберите вопрос:", reply_markup=faq_keyboard())
        elif data == "check_sub":
            if await has_subscription(uid):
                until = subscriptions[uid].astimezone(TIMEZONE)
                await query.edit_message_text(f"✅ Подписка до {until.strftime('%d.%m.%Y %H:%M:%S')}", reply_markup=main_menu())
            else:
                await query.edit_message_text("❌ Нет активной подписки", reply_markup=main_menu())
        elif data == "trial":
            if await has_subscription(uid):
                await query.edit_message_text("У вас уже есть подписка!", reply_markup=main_menu())
                return
            expiry = datetime.now(pytz.UTC) + timedelta(hours=2)
            subscriptions[uid] = expiry
            save_data()
            await query.edit_message_text(
                f"🎁 Пробный доступ активирован!\n\nДействует до: {expiry.astimezone(TIMEZONE).strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                f"📌 Теперь подключите бота в Telegram для бизнеса:\n"
                f"1. Настройки → Telegram для бизнеса\n"
                f"2. Чат-боты → Добавить\n"
                f"3. Введите @{(await context.bot.get_me()).username}",
                reply_markup=main_menu()
            )
        elif data.startswith("faq_"):
            texts = {
                "connect": "🔌 Подключение бота:\n\n1. Настройки Telegram\n2. Telegram для бизнеса\n3. Чат-боты\n4. Добавить бота\n5. Введите @username бота\n\nПосле подключения бот сам пришлёт приветствие!",
                "delete": "❌ Отслеживание удалений:\n\nБот автоматически кэширует все ваши сообщения. При удалении вы получите точную копию с датами.",
                "save": "💾 Сохранение медиа:\n\nПросто ответьте на любое сообщение с фото/видео/голосовым/кружком — бот сохранит его автоматически!",
                "payment": "💳 Способы оплаты:\n\n• Telegram Stars (мгновенно)\n• CryptoBot (USDT)\n• Platega.io (карты/СБП)",
            }
            topic = data.replace("faq_", "")
            await query.edit_message_text(texts.get(topic, "Ответ не найден"), reply_markup=faq_keyboard())
        elif data.startswith("tariff_"):
            plan = data.replace("tariff_", "")
            await query.edit_message_text(f"💰 Тариф {plan.replace('_', ' ')}\n\nВыберите способ оплаты:", reply_markup=payment_methods_keyboard(plan))
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
                        f"🪙 Оплата через CryptoBot\n\n"
                        f"Сумма: {amount} USDT\n"
                        f"Ссылка: {url}\n\n"
                        f"ID: {payment_id}\n\n"
                        f"/check_payment {payment_id}"
                    )
                else:
                    await query.edit_message_text("❌ Ошибка создания счёта в CryptoBot")
            
            elif method == "platega":
                amount = PRICES[plan]["rub"]
                url, payment_id = await create_platega_payment(amount, uid, plan)
                if url:
                    await query.edit_message_text(
                        f"💳 Оплата через Platega.io\n\n"
                        f"Сумма: {amount} ₽\n"
                        f"Ссылка: {url}\n\n"
                        f"ID: {payment_id}\n\n"
                        f"/check_payment {payment_id}"
                    )
                else:
                    await query.edit_message_text("❌ Ошибка создания счёта в Platega.io")
    except Exception as e:
        print(f"[CALLBACK] Error: {e}")
        await query.edit_message_text("Произошла ошибка. Попробуйте ещё раз.")

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
        await update.message.reply_text(f"✅ Подписка активирована через Stars!\nПлан: {plan.replace('_', ' ')}")

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
    
    # ⭐⭐⭐ BUSINESS API ОБРАБОТЧИКИ (исправленные) ⭐⭐⭐
    # 1. Подключение бота к бизнес-аккаунту
    app.add_handler(MessageHandler(filters.BUSINESS_CONNECTION, handle_business_connection))
    
    # 2. Новые сообщения из бизнес-чатов
    app.add_handler(MessageHandler(filters.BUSINESS_MESSAGE, handle_business_message))
    
    # 3. Изменённые сообщения
    app.add_handler(MessageHandler(filters.EDITED_BUSINESS_MESSAGE, handle_edited_business_message))
    
    # 4. Удалённые сообщения
    app.add_handler(MessageHandler(filters.DELETED_BUSINESS_MESSAGES, handle_deleted_business_messages))
    
    # Автосохранение медиа
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, auto_save_media))
    
    # Фоновая проверка платежей
    start_background_checker(app)
    
    print("=" * 50)
    print("✅ БОТ ЗАПУЩЕН!")
    print("=" * 50)
    print("📌 КЛЮЧЕВЫЕ ИСПРАВЛЕНИЯ:")
    print("   1. Добавлен обработчик business_connection")
    print("   2. Исправлен обработчик deleted_business_messages")
    print("   3. Добавлено кэширование сообщений")
    print("   4. Фоновая проверка платежей")
    print("=" * 50)
    print("\n📌 КАК ПОДКЛЮЧИТЬ БОТА:")
    print("   1. Настройки Telegram → Telegram для бизнеса")
    print("   2. Чат-боты → Добавить")
    print(f"   3. Введите @{(app.bot.username if hasattr(app.bot, 'username') else BOT_TOKEN.split(':')[0])}")
    print("   4. Выберите чаты и разрешите 'Управление сообщениями'")
    print("=" * 50)
    
    app.run_polling()

if __name__ == "__main__":
    main()
