# bot.py
import os
import json
import requests
from bs4 import BeautifulSoup
from functools import wraps
from telegram import (
    Bot,
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ParseMode,
    InputMediaPhoto,
)
from telegram.ext import (
    Updater,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    Filters,
    Dispatcher,
)
import logging

# -------- configuration --------
# ضع التوكن في متغير بيئي TELEGRAM_TOKEN أو الصقه هنا (غير مستحسن للعموم)
TOKEN = os.getenv("TELEGRAM_TOKEN") or "8225851092:AAGeYxu-0-dhXKvF9_QFg703gH6PkeCLd5k"

# عنوان السيرفر الذي تريد إرسال الطلبات إليه
SERVER_BASE = "https://fourbe-0sfk.onrender.com"

# ملف HTML الذي رفعته — يجب أن يكون بنفس المجلد
HTML_FILE = "index.html"

# logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# -------- helpers: parse index.html --------
def parse_site_file(path=HTML_FILE):
    """
    يعيد dict: { sections: [{key,title}], payments: [{key,title,img}] }
    """
    data = {"sections": [], "payments": []}
    try:
        with open(path, "r", encoding="utf-8") as f:
            soup = BeautifulSoup(f.read(), "html.parser")
    except Exception as e:
        logger.error("Cannot open %s: %s", path, e)
        return data

    # sections: divs with class section-tile and data-section
    for tile in soup.select(".section-tile"):
        key = tile.get("data-section") or ""
        title_el = tile.select_one(".section-name")
        title = title_el.get_text(strip=True) if title_el else key
        if key:
            data["sections"].append({"key": key, "title": title})

    # payment methods: elements with class 'pd-method-tile' or 'pd-method-tilee'
    for btn in soup.select(".pd-method-tile, .pd-method-tilee"):
        method = btn.get("data-method") or ""
        # title is typically in the inner div or text
        div = btn.find("div")
        title = div.get_text(strip=True) if div else method
        img = None
        img_tag = btn.find("img")
        if img_tag:
            img = img_tag.get("src")
        if method:
            data["payments"].append({"key": method, "title": title, "img": img})
    return data

SITE_DATA = parse_site_file()

# -------- state per chat (simple ephemeral) --------
chat_state = {}
def set_state(chat_id, value):
    chat_state[chat_id] = value

def get_state(chat_id):
    return chat_state.get(chat_id, {})

def clear_state(chat_id):
    if chat_id in chat_state:
        del chat_state[chat_id]

# -------- sending to server --------
def post_to_server(payload):
    """
    يحاول إرسال payload إلى عدة نهايات شائعة على السيرفر:
    /api/orders ثم /orders
    يرجع dict نتيجة أو خطأ
    """
    urls = [
        f"{SERVER_BASE}/api/orders",
        f"{SERVER_BASE}/orders",
        f"{SERVER_BASE}/api/order",
    ]
    last_err = None
    headers = {"Content-Type": "application/json"}
    for url in urls:
        try:
            r = requests.post(url, json=payload, timeout=12, headers=headers)
            # قبول 200/201/202 كنجاح
            if r.status_code in (200, 201, 202):
                try:
                    return {"ok": True, "status_code": r.status_code, "response": r.json()}
                except Exception:
                    return {"ok": True, "status_code": r.status_code, "response_text": r.text}
            else:
                last_err = {"url": url, "status": r.status_code, "text": r.text}
        except Exception as e:
            last_err = {"url": url, "error": str(e)}
    return {"ok": False, "error": last_err}

# -------- telegram handlers --------
def restricted(func):
    @wraps(func)
    def wrapper(update: Update, context):
        return func(update, context)
    return wrapper

@restricted
def start(update: Update, context):
    chat_id = update.effective_chat.id
    text = "أهلاً! اختر أمر:\n/sections لعرض الأقسام\n/payments لطرق الشحن"
    update.message.reply_text(text)

@restricted
def cmd_sections(update: Update, context):
    chat_id = update.effective_chat.id
    sections = SITE_DATA.get("sections", [])
    if not sections:
        update.message.reply_text("لم أجد أقساماً في الملف (تأكد من وجود index.html في نفس المجلد).")
        return
    kb = []
    for s in sections:
        kb.append([InlineKeyboardButton(s["title"], callback_data=f"section:{s['key']}")])
    update.message.reply_text("اختر القسم:", reply_markup=InlineKeyboardMarkup(kb))

@restricted
def cmd_payments(update: Update, context):
    chat_id = update.effective_chat.id
    pm = SITE_DATA.get("payments", [])
    if not pm:
        update.message.reply_text("لم أجد طرق شحن في الملف.")
        return
    kb = []
    for p in pm:
        kb.append([InlineKeyboardButton(p["title"], callback_data=f"paymethod:{p['key']}")])
    update.message.reply_text("اختر طريقة الدفع:", reply_markup=InlineKeyboardMarkup(kb))

def callback_handler(update: Update, context):
    q = update.callback_query
    if not q:
        return
    data = q.data
    chat_id = q.message.chat_id
    q.answer()
    if data.startswith("section:"):
        key = data.split(":", 1)[1]
        # تهيئة حالة الطلب
        set_state(chat_id, {"stage": "awaiting_order_details", "section": key})
        q.message.reply_text(
            f"تم اختيار القسم `{key}`.\nأرسل تفاصيل الطلب كسطر واحد بهذا الشكل (بدون فاصلة):\n\n<item_name>|<id_field>|<phone>\n\nمثال:\nحزمة 100 لايك|user123|+9639xxxxxxx\n\nأو أرسل فقط اسم العنصر إذا لا توجد تفاصيل إضافية.",
            parse_mode=ParseMode.MARKDOWN,
        )
    elif data.startswith("paymethod:"):
        mkey = data.split(":", 1)[1]
        # ابحث عن البيانات
        pm = next((p for p in SITE_DATA.get("payments", []) if p["key"] == mkey), None)
        if not pm:
            q.message.reply_text("طريقة الدفع غير موجودة.")
            return
        text = f"طريقة الدفع: *{pm.get('title') or mkey}*\nرمز: `{mkey}`"
        if pm.get("img"):
            # نرسل الصورة مع الوصف
            try:
                context.bot.send_photo(chat_id=chat_id, photo=pm["img"], caption=text, parse_mode=ParseMode.MARKDOWN)
            except Exception:
                q.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)
        else:
            q.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)
        q.message.reply_text("إذا أتممت الدفع، أرسل تأكيد (نص) هنا أو ابقَ على اتصال مع الإدارة.")

def text_message_handler(update: Update, context):
    chat_id = update.effective_chat.id
    text = update.message.text.strip()
    st = get_state(chat_id)
    if st.get("stage") == "awaiting_order_details":
        # parse expected format: item|idField|phone
        parts = [p.strip() for p in text.split("|")]
        item = parts[0] if len(parts) >= 1 else ""
        idField = parts[1] if len(parts) >= 2 else ""
        phone = parts[2] if len(parts) >= 3 else ""
        payload = {
            "section": st.get("section"),
            "item": item,
            "idField": idField,
            "phone": phone,
            "chat_id": chat_id,
            "username": update.effective_user.username or "",
            "source": "telegram-bot",
        }
        update.message.reply_text("أرسل طلبك... سيتم التواصل عند الاستجابة من السيرفر.")
        res = post_to_server(payload)
        if res.get("ok"):
            update.message.reply_text("تم إرسال الطلب للسيرفر بنجاح.\nالرد من السيرفر:\n" + json.dumps(res.get("response") or res.get("response_text") or {}, ensure_ascii=False))
        else:
            update.message.reply_text("فشل إرسال الطلب. تفاصيل الخطأ:\n" + str(res.get("error")))
        clear_state(chat_id)
    else:
        # رسائل عادية
        update.message.reply_text("لم أفهم. استخدم /sections لعرض الأقسام أو /payments لطرق الشحن.")

def error_handler(update: Update, context):
    logger.exception("Update caused error: %s", context.error)

# -------- main --------
def main():
    up = Updater(TOKEN, use_context=True)
    dp = up.dispatcher

    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(CommandHandler("sections", cmd_sections))
    dp.add_handler(CommandHandler("payments", cmd_payments))

    dp.add_handler(CallbackQueryHandler(callback_handler))
    dp.add_handler(MessageHandler(Filters.text & (~Filters.command), text_message_handler))

    dp.add_error_handler(error_handler)
    logger.info("Starting bot...")
    up.start_polling()
    up.idle()

if __name__ == "__main__":
    main()
