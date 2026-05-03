#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import json
import secrets
import hashlib
import hmac
from datetime import datetime, timedelta
from collections import defaultdict
from typing import Dict, Optional, Tuple
import asyncio

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

# API ключи для платежей
CRYPTOBOT_TOKEN = "YOUR_CRYPTOBOT_TOKEN"  # Получить у @CryptoBot
CRYPTOBOT_API_URL = "https://pay.crypt.bot/api"

PLATEGA_API_KEY = "YOUR_PLATEGA_API_KEY"
PLATEGA_SHOP_ID = "YOUR_SHOP_ID"
PLATEGA_API_URL = "https://platega.io/api/v1"
PLATEGA_SECRET_KEY = "YOUR_PLATEGA_SECRET_KEY"  # Секретный ключ для вебхуков

# Файлы для хранения данных
SUBS_FILE = "subscriptions.json"
PENDING_FILE = "pending_payments.json"
CACHE_FILE = "message_cache.json"

# Глобальные хранилища
subscriptions: Dict[int, datetime] = {}
pending_payments: Dict[str, dict] = {}
message_cache: Dict[int, Dict[int, dict]] = defaultdict(dict)

# Цены
PRICES = {
    "1_day": {"stars": 5, "rub": 49, "crypto_usdt": 0.5, "crypto_btc": 0.000008, "crypto_ton": 0.2},
    "7_days": {"stars": 10, "rub": 199, "crypto_usdt": 2, "crypto_btc": 0.00003, "crypto_ton": 0.8},
    "1_month": {"stars": 15, "rub": 399, "crypto_usdt": 5, "crypto_btc": 0.00008, "crypto_ton": 2},
}

PLANS = {
    "1_day": timedelta(days=1),
    "7_days": timedelta(days=7),
    "1_month": timedelta(days=30),
}

# Хранилище для созданных инвойсов CryptoBot
crypto_invoices: Dict[str, dict] = {}

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
        cache_to_save = {str(uid): {str(mid): msg for mid, msg in msgs.items()}
                         for uid, msgs in message_cache.items()}
        json.dump(cache_to_save, f, indent=2)

async def has_subscription(user_id: int) -> bool:
    if user_id not in subscriptions:
        return False
    return subscriptions[user_id] > datetime.now(pytz.UTC)

def activate_subscription(user_id: int, plan: str):
    expiry = datetime.now(pytz.UTC) + PLANS[plan]
    subscriptions[user_id] = expiry
    save_data()
    print(f"✅ Подписка активирована для {user_id}, план: {plan}, до: {expiry}")

# ========== CRYPTOBOT ПЛАТЕЖИ ==========
async def create_crypto_invoice(amount: float, asset: str, user_id: int, plan: str) -> Tuple[Optional[str], Optional[str]]:
    """Создаёт инвойс в CryptoBot и возвращает URL и invoice_id"""
    try:
        url = f"{CRYPTOBOT_API_URL}/createInvoice"
        headers = {"Crypto-Pay-API-Token": CRYPTOBOT_TOKEN}
        params = {
            "asset": asset,
            "amount": str(amount),
            "description": f"Подписка {plan} для пользователя {user_id}",
            "paid_btn_name": "callback",
            "paid_btn_url": f"https://t.me/{(await get_bot_username())}?start=crypto_check",
        }
        
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, params=params) as resp:
                data = await resp.json()
                if data.get("ok"):
                    invoice = data["result"]
                    invoice_id = str(invoice["invoice_id"])
                    
                    # Сохраняем информацию об инвойсе
                    crypto_invoices[invoice_id] = {
                        "user_id": user_id,
                        "plan": plan,
                        "amount": amount,
                        "asset": asset,
                        "status": "pending",
                        "created_at": datetime.now().isoformat()
                    }
                    
                    # Сохраняем в pending_payments
                    payment_id = f"crypto_{invoice_id}"
                    pending_payments[payment_id] = {
                        "user_id": user_id,
                        "plan": plan,
                        "method": f"crypto_{asset.lower()}",
                        "amount": amount,
                        "invoice_id": invoice_id,
                        "status": "pending"
                    }
                    save_data()
                    
                    return invoice["bot_invoice_url"], payment_id
    except Exception as e:
        print(f"Ошибка создания CryptoBot инвойса: {e}")
    
    return None, None

async def check_crypto_payment(invoice_id: str) -> Tuple[bool, Optional[str]]:
    """Проверяет статус платежа в CryptoBot"""
    try:
        url = f"{CRYPTOBOT_API_URL}/getInvoices"
        headers = {"Crypto-Pay-API-Token": CRYPTOBOT_TOKEN}
        params = {"invoice_ids": invoice_id}
        
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, params=params) as resp:
                data = await resp.json()
                if data.get("ok") and data["result"]["items"]:
                    invoice = data["result"]["items"][0]
                    status = invoice.get("status")
                    
                    if status == "paid":
                        return True, invoice.get("paid_amount")
                    elif status == "expired":
                        return False, None
    except Exception as e:
        print(f"Ошибка проверки CryptoBot: {e}")
    
    return False, None

async def process_crypto_payment(payment_id: str, user_id: int) -> bool:
    """Обрабатывает подтверждение крипто-платежа"""
    if payment_id not in pending_payments:
        return False
    
    payment = pending_payments[payment_id]
    if payment["user_id"] != user_id:
        return False
    
    invoice_id = payment.get("invoice_id")
    if not invoice_id:
        return False
    
    # Проверяем статус
    is_paid, amount = await check_crypto_payment(invoice_id)
    
    if is_paid:
        activate_subscription(user_id, payment["plan"])
        del pending_payments[payment_id]
        if invoice_id in crypto_invoices:
            crypto_invoices[invoice_id]["status"] = "paid"
        save_data()
        return True
    
    return False

# ========== PLATEGA.IO ПЛАТЕЖИ ==========
async def create_platega_payment(amount: float, user_id: int, plan: str) -> Tuple[Optional[str], Optional[str]]:
    """Создаёт платёж в Platega.io и возвращает URL и payment_id"""
    try:
        payment_id = f"platega_{secrets.token_hex(8)}"
        
        url = f"{PLATEGA_API_URL}/invoice/create"
        headers = {
            "API-Key": PLATEGA_API_KEY,
            "Content-Type": "application/json",
        }
        payload = {
            "shop_id": PLATEGA_SHOP_ID,
            "amount": amount,
            "currency": "RUB",
            "order_id": payment_id,
            "description": f"Подписка {plan} для пользователя {user_id}",
            "success_url": f"https://t.me/{(await get_bot_username())}?start=platega_success_{payment_id}",
            "fail_url": f"https://t.me/{(await get_bot_username())}",
            "webhook_url": f"https://your-domain.com/webhook/platega",  # Замените на ваш URL
        }
        
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, headers=headers) as resp:
                data = await resp.json()
                if data.get("url"):
                    # Сохраняем в pending_payments
                    pending_payments[payment_id] = {
                        "user_id": user_id,
                        "plan": plan,
                        "method": "platega",
                        "amount": amount,
                        "status": "pending",
                        "created_at": datetime.now().isoformat()
                    }
                    save_data()
                    return data["url"], payment_id
    except Exception as e:
        print(f"Ошибка создания Platega платежа: {e}")
    
    return None, None

async def check_platega_payment(payment_id: str) -> bool:
    """Проверяет статус платежа Platega.io"""
    try:
        url = f"{PLATEGA_API_URL}/invoice/info"
        headers = {"API-Key": PLATEGA_API_KEY}
        params = {"order_id": payment_id}
        
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, params=params) as resp:
                data = await resp.json()
                if data.get("status") == "paid":
                    return True
    except Exception as e:
        print(f"Ошибка проверки Platega: {e}")
    
    return False

async def verify_platega_webhook(data: dict, signature: str) -> bool:
    """Проверяет подпись вебхука Platega.io"""
    try:
        expected_signature = hmac.new(
            PLATEGA_SECRET_KEY.encode(),
            json.dumps(data, sort_keys=True).encode(),
            hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(signature, expected_signature)
    except:
        return False

# ========== TELEGRAM STARS ПЛАТЕЖИ ==========
async def process_stars_payment(payload: str) -> bool:
    """Обрабатывает оплату Telegram Stars"""
    try:
        parts = payload.split(":")
        if len(parts) == 4 and parts[0] == "sub":
            plan = parts[1]
            user_id = int(parts[2])
            payment_id = parts[3]
            
            # Проверяем, что платёж существует и не обработан
            if payment_id in pending_payments:
                activate_subscription(user_id, plan)
                del pending_payments[payment_id]
                save_data()
                return True
    except Exception as e:
        print(f"Ошибка обработки Stars платежа: {e}")
    
    return False

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
        [InlineKeyboardButton("📅 1 день — от 5⭐ / 49₽ / 0.5 USDT", callback_data="tariff_1_day")],
        [InlineKeyboardButton("📆 7 дней — от 10⭐ / 199₽ / 2 USDT", callback_data="tariff_7_days")],
        [InlineKeyboardButton("🗓 1 месяц — от 15⭐ / 399₽ / 5 USDT", callback_data="tariff_1_month")],
        [InlineKeyboardButton("🔙 Назад", callback_data="back_to_menu")],
    ]
    return InlineKeyboardMarkup(buttons)

def payment_methods_keyboard(plan: str):
    buttons = [
        [InlineKeyboardButton("⭐️ Telegram Stars", callback_data=f"pay_stars_{plan}")],
        [InlineKeyboardButton("🪙 CryptoBot (USDT)", callback_data=f"pay_crypto_usdt_{plan}")],
        [InlineKeyboardButton("₿ CryptoBot (BTC)", callback_data=f"pay_crypto_btc_{plan}")],
        [InlineKeyboardButton("💎 CryptoBot (TON)", callback_data=f"pay_crypto_ton_{plan}")],
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

# ========== КОМАНДЫ ==========
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    
    # Обработка deep link для подтверждения платежей
    if context.args:
        arg = context.args[0]
        
        # Обработка успешного платежа Platega
        if arg.startswith("platega_success_"):
            payment_id = arg.replace("platega_success_", "")
            if payment_id in pending_payments:
                payment = pending_payments[payment_id]
                if payment["user_id"] == uid:
                    # Проверяем статус платежа
                    if await check_platega_payment(payment_id):
                        activate_subscription(uid, payment["plan"])
                        del pending_payments[payment_id]
                        save_data()
                        await update.message.reply_text("✅ **Подписка успешно активирована через Platega.io!**", parse_mode="Markdown")
                    else:
                        await update.message.reply_text("⏳ Платёж ещё не подтверждён. Нажмите /check_payment для проверки.")
                    return
        
        # Обработка проверки CryptoBot
        if arg == "crypto_check":
            await update.message.reply_text("🔄 Используйте /check_payment для проверки статуса")
            return

    if await has_subscription(uid):
        until = subscriptions[uid].astimezone(TIMEZONE)
        await update.message.reply_text(
            f"✅ **Активная подписка**\n\nДействует до: {until.strftime('%d.%m.%Y %H:%M:%S')}\n\n"
            f"📌 Бот отслеживает все изменения в чатах\n"
            f"💾 Просто ответьте на любое медиа — оно сохранится автоматически!",
            reply_markup=main_menu(),
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(
            "👋 **Приватный бот для Business аккаунтов**\n\n"
            "✅ Отслеживает удалённые и изменённые сообщения\n"
            "✅ Автосохранение фото/видео/кружков/голосовых/GIF/стикеров\n\n"
            "Выберите действие:",
            reply_markup=main_menu(),
            parse_mode="Markdown"
        )

async def cmd_faq(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "❓ **Часто задаваемые вопросы**\n\nВыберите тему:",
        reply_markup=faq_keyboard(),
        parse_mode="Markdown"
    )

async def cmd_check_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Проверка статуса платежа по ID"""
    if not context.args:
        # Если ID не указан, показываем все ожидающие платежи
        user_id = update.effective_user.id
        user_payments = {pid: p for pid, p in pending_payments.items() if p["user_id"] == user_id}
        
        if not user_payments:
            await update.message.reply_text("❌ У вас нет ожидающих платежей")
            return
        
        text = "**💰 Ваши ожидающие платежи:**\n\n"
        for pid, payment in user_payments.items():
            text += f"• ID: `{pid}`\n  План: {payment['plan']}\n  Метод: {payment['method']}\n\n"
        text += "Для проверки конкретного платежа: `/check_payment <ID>`"
        await update.message.reply_text(text, parse_mode="Markdown")
        return
    
    payment_id = context.args[0]
    
    if payment_id not in pending_payments:
        await update.message.reply_text("❌ Платёж не найден")
        return
    
    payment = pending_payments[payment_id]
    user_id = update.effective_user.id
    
    if payment["user_id"] != user_id:
        await update.message.reply_text("❌ Это не ваш платёж")
        return
    
    # Проверяем статус в зависимости от метода
    method = payment["method"]
    status_msg = f"🔍 **Проверка платежа**\nID: `{payment_id}`\nМетод: {method}\nПлан: {payment['plan']}\n\n"
    
    if method == "stars":
        status_msg += "⭐️ Оплата через Telegram Stars\nСтатус: ожидает оплаты в Telegram"
        await update.message.reply_text(status_msg, parse_mode="Markdown")
    
    elif method.startswith("crypto"):
        invoice_id = payment.get("invoice_id")
        if invoice_id:
            is_paid, amount = await check_crypto_payment(invoice_id)
            if is_paid:
                activate_subscription(user_id, payment["plan"])
                del pending_payments[payment_id]
                save_data()
                await update.message.reply_text(f"✅ **Платёж подтверждён!**\nСумма: {amount} {payment.get('asset', 'USDT')}\nПодписка активирована!", parse_mode="Markdown")
            else:
                status_msg += f"🪙 CryptoBot ({payment.get('asset', 'USDT')})\nСтатус: ожидает оплаты\n\nПосле оплаты нажмите /check_payment {payment_id} снова"
                await update.message.reply_text(status_msg, parse_mode="Markdown")
        else:
            await update.message.reply_text("❌ Ошибка: не найден ID инвойса", parse_mode="Markdown")
    
    elif method == "platega":
        is_paid = await check_platega_payment(payment_id)
        if is_paid:
            activate_subscription(user_id, payment["plan"])
            del pending_payments[payment_id]
            save_data()
            await update.message.reply_text("✅ **Платёж подтверждён через Platega.io!**\nПодписка активирована!", parse_mode="Markdown")
        else:
            status_msg += f"💳 Platega.io\nСумма: {payment['amount']} ₽\nСтатус: ожидает оплаты\n\nСсылка на оплату была отправлена при создании"
            await update.message.reply_text(status_msg, parse_mode="Markdown")

# ========== КЭШ И ОТСЛЕЖИВАНИЕ ==========
async def handle_business_updates(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Единый обработчик для всех бизнес-событий"""
    
    # 1. Обработка нового бизнес-сообщения (кэширование)
    if update.business_message:
        msg = update.business_message
        user_id = msg.from_user.id
        
        if await has_subscription(user_id):
            cache_entry = {
                "text": msg.text or msg.caption or "",
                "date": msg.date.isoformat(),
                "message_id": msg.message_id,
                "chat_id": msg.chat_id,
            }
            message_cache[user_id][msg.message_id] = cache_entry
            
            if len(message_cache[user_id]) > 1000:
                oldest_key = min(message_cache[user_id].keys())
                del message_cache[user_id][oldest_key]
            save_data()
    
    # 2. Обработка изменённого бизнес-сообщения
    if update.edited_business_message:
        edited = update.edited_business_message
        user_id = edited.from_user.id
        
        if await has_subscription(user_id):
            msg_id = edited.message_id
            new_text = edited.text or edited.caption or ""
            
            if user_id in message_cache and msg_id in message_cache[user_id]:
                old = message_cache[user_id][msg_id]
                old_text = old["text"]
                
                if old_text and old_text != new_text:
                    report = (
                        f"✏️ **Собеседник изменил сообщение**\n\n"
                        f"❌ Было: {old_text[:500]}\n"
                        f"🔘 Стало: {new_text[:500]}\n\n"
                        f"📅 Получено: {datetime.fromisoformat(old['date']).astimezone(TIMEZONE).strftime('%d.%m.%Y %H:%M:%S')}\n"
                        f"✏️ Изменено: {datetime.now(TIMEZONE).strftime('%d.%m.%Y %H:%M:%S')}"
                    )
                    await context.bot.send_message(chat_id=edited.chat_id, text=report, parse_mode="Markdown")
                    old["text"] = new_text
                    save_data()
    
    # 3. Обработка удалённых бизнес-сообщений
    if update.deleted_business_messages:
        deleted = update.deleted_business_messages
        user_id = deleted.from_user.id
        
        if await has_subscription(user_id):
            for msg_id in deleted.message_ids:
                if user_id in message_cache and msg_id in message_cache[user_id]:
                    cached = message_cache[user_id][msg_id]
                    report = (
                        f"🗑 **Собеседник удалил сообщение**\n\n"
                        f"📝 Текст: {cached['text'][:500] if cached['text'] else '[нет текста]'}\n"
                        f"📅 Получено: {datetime.fromisoformat(cached['date']).astimezone(TIMEZONE).strftime('%d.%m.%Y %H:%M:%S')}\n"
                        f"🗑 Удалено: {datetime.now(TIMEZONE).strftime('%d.%m.%Y %H:%M:%S')}"
                    )
                    await context.bot.send_message(chat_id=deleted.chat_id, text=report, parse_mode="Markdown")

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
    file_path = None
    media_type = ""

    if reply.photo:
        file = await reply.photo[-1].get_file()
        file_path = f"media/photo_{reply.message_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jpg"
        await file.download_to_drive(file_path)
        saved = True
        media_type = "фото"
    elif reply.video:
        file = await reply.video.get_file()
        file_path = f"media/video_{reply.message_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.mp4"
        await file.download_to_drive(file_path)
        saved = True
        media_type = "видео"
    elif reply.voice:
        file = await reply.voice.get_file()
        file_path = f"media/voice_{reply.message_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.ogg"
        await file.download_to_drive(file_path)
        saved = True
        media_type = "голосовое"
    elif reply.video_note:
        file = await reply.video_note.get_file()
        file_path = f"media/videonote_{reply.message_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.mp4"
        await file.download_to_drive(file_path)
        saved = True
        media_type = "кружок"
    elif reply.animation:
        file = await reply.animation.get_file()
        file_path = f"media/gif_{reply.message_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.gif"
        await file.download_to_drive(file_path)
        saved = True
        media_type = "GIF"
    elif reply.sticker:
        file = await reply.sticker.get_file()
        file_path = f"media/sticker_{reply.message_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.webp"
        await file.download_to_drive(file_path)
        saved = True
        media_type = "стикер"

    if saved:
        report = (
            f"✅ **{media_type} сохранено!**\n\n"
            f"📅 Получено: {reply.date.astimezone(TIMEZONE).strftime('%d.%m.%Y %H:%M:%S')}\n"
            f"💾 Сохранено: {datetime.now(TIMEZONE).strftime('%d.%m.%Y %H:%M:%S')}\n"
            f"👤 От: @{reply.from_user.username or 'no_username'}\n"
            f"📁 Файл: {os.path.basename(file_path)}"
        )
        await update.message.reply_text(report, parse_mode="Markdown")

# ========== CALLBACK HANDLER ==========
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    uid = query.from_user.id

    if data == "show_tariffs":
        await query.edit_message_text("💰 **Выберите тариф:**", reply_markup=tariffs_keyboard(), parse_mode="Markdown")
    
    elif data == "back_to_menu":
        await query.edit_message_text("📋 **Главное меню:**", reply_markup=main_menu(), parse_mode="Markdown")
    
    elif data == "faq":
        await query.edit_message_text("❓ **FAQ**\n\nВыберите вопрос:", reply_markup=faq_keyboard(), parse_mode="Markdown")
    
    elif data == "check_sub":
        if await has_subscription(uid):
            until = subscriptions[uid].astimezone(TIMEZONE)
            await query.edit_message_text(f"✅ Подписка активна до {until.strftime('%d.%m.%Y %H:%M:%S')}", reply_markup=main_menu())
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
            f"🎁 **Пробный доступ активирован!**\nДействует до: {expiry.astimezone(TIMEZONE).strftime('%Y-%m-%d %H:%M:%S')}\n\n"
            f"Подключите бота через:\nTelegram для бизнеса → Чат-боты → Добавить\n\n"
            f"Юзернейм: @{(await context.bot.get_me()).username}",
            reply_markup=main_menu(),
            parse_mode="Markdown"
        )
    
    elif data.startswith("faq_"):
        topic = data.replace("faq_", "")
        faq_texts = {
            "connect": "🔌 **Как подключить бота:**\n\n1. Настройки Telegram\n2. Telegram для бизнеса\n3. Чат-боты\n4. Добавить\n5. Введите @username бота",
            "delete": "❌ **Отслеживание удалений:**\n\nБот кэширует все сообщения в чатах. При удалении вы получите точную копию с датами.",
            "save": "💾 **Сохранение медиа:**\n\nПросто ответьте на любое сообщение с фото/видео/кружком/голосовым/GIF/стикером — бот автоматически сохранит его!",
            "payment": "💳 **Способы оплаты:**\n\n• ⭐️ Telegram Stars (встроенная оплата)\n• 🪙 CryptoBot (USDT, BTC, TON)\n• 💳 Platega.io (карты РФ, СБП)\n\nПосле оплаты используйте /check_payment для подтверждения",
        }
        await query.edit_message_text(faq_texts.get(topic, "❓ Ответ не найден"), reply_markup=faq_keyboard(), parse_mode="Markdown")
    
    elif data.startswith("tariff_"):
        plan = data.replace("tariff_", "")
        price_text = (
            f"⭐️ Stars: {PRICES[plan]['stars']}\n"
            f"🪙 USDT: {PRICES[plan]['crypto_usdt']}\n"
            f"₿ BTC: {PRICES[plan]['crypto_btc']}\n"
            f"💎 TON: {PRICES[plan]['crypto_ton']}\n"
            f"💳 Карты/СБП: {PRICES[plan]['rub']}₽"
        )
        await query.edit_message_text(
            f"💰 **Тариф {plan.replace('_', ' ')}**\n\n{price_text}\n\nВыберите способ оплаты:",
            reply_markup=payment_methods_keyboard(plan),
            parse_mode="Markdown"
        )
    
    elif data.startswith("pay_"):
        parts = data.split("_")
        method = parts[1]
        
        if method == "crypto":
            asset = parts[2].upper()
            plan = "_".join(parts[3:])
            amount = PRICES[plan][f"crypto_{asset.lower()}"]
            
            # Создаём инвойс в CryptoBot
            invoice_url, payment_id = await create_crypto_invoice(amount, asset, uid, plan)
            
            if invoice_url:
                await query.edit_message_text(
                    f"🪙 **Оплата через CryptoBot ({asset})**\n\n"
                    f"Сумма: {amount} {asset}\n"
                    f"[Оплатить]({invoice_url})\n\n"
                    f"ID платежа: `{payment_id}`\n\n"
                    f"**После оплаты нажмите:**\n/check_payment {payment_id}",
                    parse_mode="Markdown"
                )
            else:
                await query.edit_message_text("❌ Ошибка создания счёта в CryptoBot. Попробуйте позже.")
        
        elif method == "stars":
            plan = "_".join(parts[2:])
            payment_id = secrets.token_hex(8)
            pending_payments[payment_id] = {
                "user_id": uid,
                "plan": plan,
                "method": "stars",
                "status": "pending",
                "created_at": datetime.now().isoformat()
            }
            save_data()
            
            await context.bot.send_invoice(
                chat_id=uid,
                title=f"Подписка {plan.replace('_', ' ')}",
                description=f"Доступ на {PLANS[plan].days if PLANS[plan].days else 24} дней",
                payload=f"sub:{plan}:{uid}:{payment_id}",
                provider_token="",
                currency="XTR",
                prices=[{"label": "Подписка", "amount": PRICES[plan]["stars"]}],
                start_parameter="sub",
            )
            await query.edit_message_text("⭐️ **Отправлен счёт на оплату звёздами!**\n\nОплатите в диалоге с ботом.")
        
        elif method == "platega":
            plan = "_".join(parts[2:])
            amount = PRICES[plan]["rub"]
            
            invoice_url, payment_id = await create_platega_payment(amount, uid, plan)
            
            if invoice_url:
                await query.edit_message_text(
                    f"💳 **Оплата через Platega.io**\n\n"
                    f"Сумма: {amount} ₽\n"
                    f"[Перейти к оплате]({invoice_url})\n\n"
                    f"ID платежа: `{payment_id}`\n\n"
                    f"**После оплаты нажмите:**\n/check_payment {payment_id}",
                    parse_mode="Markdown"
                )
            else:
                await query.edit_message_text("❌ Ошибка создания счёта в Platega.io. Попробуйте позже.")

# ========== ПЛАТЕЖИ STARS ==========
async def pre_checkout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Подтверждение предоплаты"""
    await update.pre_checkout_query.answer(ok=True)

async def successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Успешная оплата Stars"""
    payload = update.message.successful_payment.invoice_payload
    
    if await process_stars_payment(payload):
        await update.message.reply_text(
            "✅ **Подписка успешно активирована через Telegram Stars!**\n\n"
            "Теперь вы можете:\n"
            "• Отслеживать удалённые сообщения\n"
            "• Сохранять медиа ответом на сообщение\n"
            "• Пользоваться всеми функциями бота",
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text("❌ Ошибка активации подписки. Обратитесь в поддержку.")

# ========== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ==========
async def get_bot_username():
    """Получает username бота (заглушка, можно заменить на реальный)"""
    return BOT_TOKEN.split(":")[0]  # Временное решение

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

    # Callback кнопки
    app.add_handler(CallbackQueryHandler(handle_callback))

    # Business API обработчик (универсальный)
    app.add_handler(MessageHandler(filters.ALL, handle_business_updates))

    # Автосохранение медиа
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, auto_save_media))

    print("=" * 50)
    print("✅ БОТ ЗАПУЩЕН!")
    print("=" * 50)
    print("📌 Функционал:")
    print("   • Отслеживание удалённых/изменённых сообщений (Business API)")
    print("   • Автосохранение медиа (ответом на сообщение)")
    print("   • Полная проверка платежей:")
    print("     - ⭐️ Telegram Stars (автоматически)")
    print("     - 🪙 CryptoBot (USDT/BTC/TON)")
    print("     - 💳 Platega.io (карты/СБП)")
    print("   • Пробный доступ 2 часа")
    print("   • Команда /check_payment для проверки статуса")
    print("=" * 50)
    
    app.run_polling()

if __name__ == "__main__":
    main()
