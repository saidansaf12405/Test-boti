import logging
import random

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Poll, ReactionTypeEmoji
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    PollAnswerHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

import db
from config import BOT_TOKEN, ADMIN_IDS, QUESTION_COUNT_OPTIONS, ADMIN_PANEL_USERNAME, ADMIN_PANEL_PASSWORD

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ---------------- Conversation states (admin savol qo'shish) ----------------
ASK_CATEGORY, ASK_QUESTION, ASK_CORRECT, ASK_WRONG1, ASK_WRONG2, ASK_WRONG3 = range(6)
ADMIN_USERNAME, ADMIN_PASSWORD = range(6, 8)

ALL_CATEGORIES_MARKER = "__ALL__"

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


# =====================================================================
#                              START
# =====================================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await db.upsert_user(user.id, user.username, user.full_name)

    text = (
        f"👋 <b>Assalomu Alaykum, {user.first_name}!</b>\n\n"
        "🎓 <b>Test botiga xush kelibsiz!</b>\n\n"
        "Bu bot orqali bilimingizni sinab ko'rishingiz mumkin. "
        "Pastdagi tugmalardan birini tanlang 👇"
    )
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🚀 Testni boshlash", callback_data="start_quiz")],
            [InlineKeyboardButton("📊 Mening natijalarim", callback_data="my_stats")],
            [InlineKeyboardButton("🤖 AI bilan suhbat", callback_data="ai_chat")],
        ]
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)


async def start_quiz_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    total_q = await db.count_questions()
    if total_q == 0:
        await query.edit_message_text(
            "⚠️ Hozircha bazada savollar yo'q. Iltimos, keyinroq urinib ko'ring."
        )
        return

    categories = await db.get_categories()

    if not categories:
        await query.edit_message_text("⚠️ Hozircha bazada savollar yo'q.")
        return

    buttons = []
    for row in categories:
        label = f"📁 {row['category']} ({row['cnt']} ta)"
        buttons.append([InlineKeyboardButton(label, callback_data=f"cat_{row['category']}")])

    if len(categories) > 1:
        buttons.append(
            [InlineKeyboardButton(f"🎲 Barchasi ({total_q} ta)", callback_data=f"cat_{ALL_CATEGORIES_MARKER}")]
        )

    await query.edit_message_text(
        "📚 <b>Qaysi mavzu bo'yicha test topshirmoqchisiz?</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(buttons),
    )


# =====================================================================
#                              QUIZ FLOW
# =====================================================================

async def choose_category_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    category = query.data[len("cat_"):]
    selected_category = None if category == ALL_CATEGORIES_MARKER else category
    context.user_data["selected_category"] = selected_category

    available = await db.count_questions(selected_category)

    buttons = []
    row = []
    for n in QUESTION_COUNT_OPTIONS:
        row.append(InlineKeyboardButton(f"{n} ta", callback_data=f"count_{n}"))
        if len(row) == 3:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    category_label = selected_category if selected_category else "Barcha mavzular"
    await query.edit_message_text(
        f"📁 Mavzu: <b>{category_label}</b> ({available} ta savol mavjud)\n\n"
        "📊 <b>Nechta savoldan test topshirmoqchisiz?</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def choose_count_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    requested = int(query.data.split("_")[1])
    selected_category = context.user_data.get("selected_category")
    available = await db.count_questions(selected_category)
    n = min(requested, available)

    if n == 0:
        await query.edit_message_text("⚠️ Bu mavzuda savollar topilmadi.")
        return

    rows = await db.get_random_questions(n, selected_category)
    questions = []
    for r in rows:
        options = [
            r["correct_answer"],
            r["wrong_answer1"],
            r["wrong_answer2"],
            r["wrong_answer3"],
        ]
        random.shuffle(options)
        correct_index = options.index(r["correct_answer"])
        questions.append(
            {
                "text": r["question_text"],
                "options": options,
                "correct_index": correct_index,
            }
        )

    context.user_data["quiz"] = {
        "questions": questions,
        "index": 0,
        "correct_count": 0,
        "total": len(questions),
        "category": selected_category,
        "chat_id": update.effective_chat.id,
        "current_poll_id": None,
        "_last_user_id": update.effective_user.id,
    }

    category_label = selected_category if selected_category else "Barcha mavzular"
    await query.edit_message_text(
        f"🚀 <b>Test boshlandi!</b>\n📁 Mavzu: {category_label}\n📊 Savollar soni: {len(questions)}\n\n"
        "Har bir savolga javob beringiz bilan to'g'ri/noto'g'ri ekanligi darhol ko'rinadi 👇",
        parse_mode=ParseMode.HTML,
    )

    await send_quiz_poll(context, context.user_data["quiz"])


def _truncate(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _dedupe_options(options: list[str], limit: int = 100) -> list[str]:
    """Variantlarni limitgacha qisqartiradi va agar kesilgandan keyin ikkitasi
    bir xil chiqib qolsa, oxiriga (2), (3) kabi raqam qo'shib farqlantiradi
    (Telegram bir xil ikkita variantli poll'ni qabul qilmaydi)."""
    truncated = [_truncate(opt, limit) for opt in options]
    seen: dict[str, int] = {}
    result = []
    for opt in truncated:
        seen[opt] = seen.get(opt, 0) + 1
        if seen[opt] == 1:
            result.append(opt)
        else:
            suffix = f" ({seen[opt]})"
            result.append(opt[: limit - len(suffix)].rstrip() + suffix)
    return result


async def send_quiz_poll(context: ContextTypes.DEFAULT_TYPE, quiz: dict):
    idx = quiz["index"]
    total = quiz["total"]
    q = quiz["questions"][idx]

    question_text = f"Savol {idx + 1}/{total}: {q['text']}"
    # Telegram poll savoli 300 belgidan oshmasligi kerak
    question_text = _truncate(question_text, 300)

    # Telegram poll variantlari har biri 100 belgidan oshmasligi va
    # bir-biridan farqli bo'lishi kerak
    safe_options = _dedupe_options(q["options"], 100)

    try:
        message = await context.bot.send_poll(
            chat_id=quiz["chat_id"],
            question=question_text,
            options=safe_options,
            type=Poll.QUIZ,
            correct_option_id=q["correct_index"],
            is_anonymous=False,
        )
    except Exception:
        logger.exception(
            "Poll yuborishda xato (index=%s, chat=%s). Savol o'tkazib yuborildi.",
            idx, quiz["chat_id"],
        )
        await context.bot.send_message(
            chat_id=quiz["chat_id"],
            text="⚠️ Bu savolni ko'rsatib bo'lmadi (matn juda uzun), o'tkazib yuboryapmiz...",
        )
        quiz["index"] += 1
        if quiz["index"] < quiz["total"]:
            await send_quiz_poll(context, quiz)
        else:
            await finish_quiz(context, quiz, quiz.get("_last_user_id", 0))
        return

    quiz["current_poll_id"] = message.poll.id
    quiz["current_poll_message_id"] = message.message_id


async def poll_answer_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    poll_answer = update.poll_answer
    quiz = context.user_data.get("quiz")

    if not quiz or quiz.get("current_poll_id") != poll_answer.poll_id:
        return

    if not poll_answer.option_ids:
        return

    idx = quiz["index"]
    q = quiz["questions"][idx]
    chosen_index = poll_answer.option_ids[0]
    is_correct = chosen_index == q["correct_index"]
    quiz["_last_user_id"] = poll_answer.user.id

    if is_correct:
        quiz["correct_count"] += 1

    # 🎉 Poll xabariga reaksiya qo'yish (salyut effekti)
    try:
        await context.bot.set_message_reaction(
            chat_id=quiz["chat_id"],
            message_id=quiz["current_poll_message_id"],
            reaction=[ReactionTypeEmoji("🎉" if is_correct else "😢")],
        )
    except Exception:
        pass  # reaksiya qo'yib bo'lmasa ham davom etaveramiz

    quiz["index"] += 1

    if quiz["index"] < quiz["total"]:
        await send_quiz_poll(context, quiz)
    else:
        await finish_quiz(context, quiz, poll_answer.user.id)


async def finish_quiz(context: ContextTypes.DEFAULT_TYPE, quiz: dict, telegram_id: int):
    total = quiz["total"]
    correct = quiz["correct_count"]
    wrong = total - correct
    category = quiz.get("category")
    chat_id = quiz["chat_id"]
    percentage = round((correct / total) * 100, 2) if total else 0

    await db.save_result(telegram_id, total, correct, percentage, category)

    if percentage >= 90:
        emoji = "🏆"
        comment = "Ajoyib natija!"
    elif percentage >= 70:
        emoji = "🎉"
        comment = "Zo'r natija!"
    elif percentage >= 50:
        emoji = "👍"
        comment = "Yomon emas!"
    else:
        emoji = "💪"
        comment = "Ko'proq mashq qiling!"

    category_line = f"📁 Mavzu: <b>{category}</b>\n" if category else ""
    text = (
        f"{emoji} <b>Test yakunlandi!</b>\n\n"
        f"{category_line}"
        f"✅ To'g'ri: <b>{correct} ta</b>\n"
        f"❌ Noto'g'ri: <b>{wrong} ta</b>\n"
        f"📊 Natija: <b>{percentage}%</b>\n\n"
        f"{comment}"
    )
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🔁 Qayta boshlash", callback_data="start_quiz")],
            [InlineKeyboardButton("📊 Mening natijalarim", callback_data="my_stats")],
        ]
    )
    await context.bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )
    context.user_data.pop("quiz", None)


# =====================================================================
#                    FOYDALANUVCHI: Mening natijalarim
# =====================================================================

async def my_stats_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user = update.effective_user
    summary = await db.get_user_summary(user.id)
    history = await db.get_user_results(user.id, limit=10)

    attempts = summary["attempts"]

    if attempts == 0:
        text = (
            "📊 <b>Mening natijalarim</b>\n\n"
            "Siz hali birorta test topshirmagansiz.\n"
            "Boshlash uchun \"🚀 Testni boshlash\" tugmasini bosing."
        )
        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton("🚀 Testni boshlash", callback_data="start_quiz")]]
        )
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
        return

    avg_percentage = round(float(summary["avg_percentage"]), 2)
    best_percentage = round(float(summary["best_percentage"]), 2)

    text = (
        "📊 <b>Mening natijalarim</b>\n\n"
        f"📝 Jami topshirilgan testlar: <b>{attempts}</b>\n"
        f"📈 O'rtacha natija: <b>{avg_percentage}%</b>\n"
        f"🏆 Eng yaxshi natija: <b>{best_percentage}%</b>\n\n"
        "🕐 <b>Oxirgi urinishlar:</b>\n"
    )
    for r in history:
        cat = r["category"] or "Umumiy"
        date_str = r["created_at"].strftime("%d.%m.%Y %H:%M")
        total_q = r["total_questions"]
        corr = r["correct_count"]
        wrong = total_q - corr
        text += (
            f"• {cat}: ✅ {corr} ta / ❌ {wrong} ta — {r['percentage']}% ({date_str})\n"
        )

    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("🚀 Yangi test", callback_data="start_quiz")]]
    )
    await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)


# =====================================================================
#                    ADMIN: /addquestion (ConversationHandler)
# =====================================================================

async def addquestion_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Bu buyruq faqat adminlar uchun.")
        return ConversationHandler.END

    context.user_data["session_added"] = 0

    await update.message.reply_text(
        "📝 <b>Yangi savollar qo'shish</b>\n\n"
        "Bu savollar qaysi mavzuga oid? (masalan: <i>Matematika</i>)\n\n"
        "Bekor qilish uchun /cancel yozing.",
        parse_mode=ParseMode.HTML,
    )
    return ASK_CATEGORY


async def addquestion_get_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["current_category"] = update.message.text.strip()
    context.user_data["new_q"] = {}
    await update.message.reply_text(
        f"📁 Mavzu: <b>{context.user_data['current_category']}</b>\n\n"
        "Endi savol matnini yuboring (masalan: <i>2+2=?</i>)",
        parse_mode=ParseMode.HTML,
    )
    return ASK_QUESTION


async def addquestion_get_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_q"] = {"text": update.message.text}
    await update.message.reply_text("✅ To'g'ri javobni yuboring:")
    return ASK_CORRECT


async def addquestion_get_correct(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_q"]["correct"] = update.message.text
    await update.message.reply_text("1-noto'g'ri javobni yuboring:")
    return ASK_WRONG1


async def addquestion_get_wrong1(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_q"]["wrong1"] = update.message.text
    await update.message.reply_text("2-noto'g'ri javobni yuboring:")
    return ASK_WRONG2


async def addquestion_get_wrong2(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_q"]["wrong2"] = update.message.text
    await update.message.reply_text("3-noto'g'ri javobni yuboring:")
    return ASK_WRONG3


async def addquestion_get_wrong3(update: Update, context: ContextTypes.DEFAULT_TYPE):
    new_q = context.user_data.pop("new_q")
    new_q["wrong3"] = update.message.text
    category = context.user_data["current_category"]

    await db.add_question(
        new_q["text"], new_q["correct"], new_q["wrong1"], new_q["wrong2"], new_q["wrong3"],
        category, update.effective_user.id,
    )

    context.user_data["session_added"] = context.user_data.get("session_added", 0) + 1
    added_count = context.user_data["session_added"]

    text = (
        f"✅ <b>Savol qo'shildi!</b> (bu seansda: {added_count} ta)\n\n"
        f"📁 Mavzu: {category}\n"
        f"❓ {new_q['text']}\n"
        f"✅ To'g'ri: {new_q['correct']}\n"
        f"❌ {new_q['wrong1']}\n"
        f"❌ {new_q['wrong2']}\n"
        f"❌ {new_q['wrong3']}\n\n"
        f"➡️ <b>Keyingi savol matnini yuboring</b> (mavzu: {category})\n"
        "Tugatish uchun /done, bekor qilish uchun /cancel."
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)
    return ASK_QUESTION


async def addquestion_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    added_count = context.user_data.pop("session_added", 0)
    context.user_data.pop("new_q", None)
    context.user_data.pop("current_category", None)
    await update.message.reply_text(
        f"🏁 Tugatildi. Bu seansda jami <b>{added_count} ta</b> savol qo'shildi.",
        parse_mode=ParseMode.HTML,
    )
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    added_count = context.user_data.pop("session_added", 0)
    context.user_data.pop("new_q", None)
    context.user_data.pop("current_category", None)
    if added_count:
        await update.message.reply_text(
            f"❌ Bekor qilindi. Bu seansda {added_count} ta savol saqlangan edi."
        )
    else:
        await update.message.reply_text("❌ Bekor qilindi.")
    return ConversationHandler.END


# =====================================================================
#                    KICHIK YORDAMCHI (bazadan qidiradi)
# =====================================================================
# Bu haqiqiy AI emas — pullik API kalit talab qilmaydi. Foydalanuvchi
# yozgan matnga bazadagi savollar ichidan ENG O'XSHASHINI topib, uning
# javobini qaytaradi. Agar hech narsa yetarlicha o'xshamasa, halol tarzda
# "bilmayman" deydi.

SIMILARITY_THRESHOLD = 0.20  # 0..1 oralig'ida; kerak bo'lsa moslashtiring


async def ai_chat_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    context.user_data["mode"] = "ai_chat"

    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("🔙 Suhbatni tugatish", callback_data="ai_chat_stop")]]
    )
    await query.edit_message_text(
        "🤖 <b>Yordamchi bilan suhbat</b>\n\n"
        "Menga test mavzusiga oid savolingizni yozing — bazadan eng yaqin "
        "javobni topib beraman.\n\n"
        "⚠️ Men katta AI emasman, faqat bazadagi savollar asosida javob "
        "beraman. Murakkab yoki mavzudan tashqari savollarga to'liq javob "
        "berolmasligim mumkin.",
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )


async def ai_chat_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    context.user_data["mode"] = None

    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🚀 Testni boshlash", callback_data="start_quiz")],
            [InlineKeyboardButton("📊 Mening natijalarim", callback_data="my_stats")],
            [InlineKeyboardButton("🤖 AI bilan suhbat", callback_data="ai_chat")],
        ]
    )
    await query.edit_message_text(
        "✅ Suhbat tugatildi. Yana kerak bo'lsa, pastdagi tugmalardan foydalaning 👇",
        reply_markup=keyboard,
    )


async def ai_chat_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Faqat "AI bilan suhbat" rejimida bo'lgan foydalanuvchilarga javob beradi.
    # Boshqa holatlarda (masalan /addquestion jarayonida) bu handler ishlamaydi,
    # chunki ConversationHandler o'sha update'larni oldinroq o'zi ushlab qoladi.
    if context.user_data.get("mode") != "ai_chat":
        return

    user_text = update.message.text.strip()
    if len(user_text) < 3:
        await update.message.reply_text("✏️ Iltimos, savolingizni to'liqroq yozing.")
        return

    match = await db.find_similar_question(user_text, SIMILARITY_THRESHOLD)

    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("🔙 Suhbatni tugatish", callback_data="ai_chat_stop")]]
    )

    if match:
        reply = (
            f"📖 <b>Topilgan savol</b> ({match['category']}):\n"
            f"{match['question_text']}\n\n"
            f"✅ <b>Javob:</b> {match['correct_answer']}"
        )
        await update.message.reply_text(reply, parse_mode=ParseMode.HTML, reply_markup=keyboard)
    else:
        await update.message.reply_text(
            "🤔 Kechirasiz, bu savolga bazamda mos javob topa olmadim.\n"
            "Men kichik yordamchiman — faqat test mavzulariga oid savollarga "
            "javob bera olaman. Iltimos, savolni boshqacharoq yoki aniqroq "
            "so'rab ko'ring.",
            reply_markup=keyboard,
        )


# =====================================================================
#                      /admin — LOGIN VA FOYDALANUVCHILAR
# =====================================================================

async def admin_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not ADMIN_PANEL_PASSWORD:
        await update.message.reply_text(
            "⚠️ Admin panel hali sozlanmagan. .env fayliga ADMIN_PANEL_USERNAME "
            "va ADMIN_PANEL_PASSWORD qo'shing."
        )
        return ConversationHandler.END

    await update.message.reply_text("🔐 Admin panel\n\nUsername kiriting:")
    return ADMIN_USERNAME


async def admin_get_username(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["admin_username_input"] = update.message.text.strip()
    await update.message.reply_text("🔑 Parolni kiriting:")
    return ADMIN_PASSWORD


async def admin_get_password(update: Update, context: ContextTypes.DEFAULT_TYPE):
    username_ok = context.user_data.get("admin_username_input") == ADMIN_PANEL_USERNAME
    password_ok = update.message.text.strip() == ADMIN_PANEL_PASSWORD

    # Parolni chatdan darhol o'chiramiz (xavfsizlik uchun, iloji bo'lsa)
    try:
        await update.message.delete()
    except Exception:
        pass

    if username_ok and password_ok:
        context.user_data["admin_authed"] = True
        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton("👥 Foydalanuvchilar ro'yxati", callback_data="admin_users")]]
        )
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="✅ Kirish muvaffaqiyatli!",
            reply_markup=keyboard,
        )
    else:
        context.user_data["admin_authed"] = False
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="❌ Username yoki parol xato.",
        )

    context.user_data.pop("admin_username_input", None)
    return ConversationHandler.END


async def admin_users_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not context.user_data.get("admin_authed"):
        await query.edit_message_text("⚠️ Avval /admin orqali kiring.")
        return

    total = await db.count_users()
    users = await db.get_all_users(limit=100)

    if not users:
        await query.edit_message_text("Hozircha hech kim botdan foydalanmagan.")
        return

    lines = [f"👥 <b>Jami foydalanuvchilar:</b> {total}\n"]
    for i, u in enumerate(users, start=1):
        name = u["full_name"] or "—"
        uname = f"@{u['username']}" if u["username"] else "—"
        joined = u["joined_at"].strftime("%Y-%m-%d %H:%M") if u["joined_at"] else "—"
        lines.append(f"{i}. {name} ({uname}) — ID: <code>{u['telegram_id']}</code> — {joined}")

    text = "\n".join(lines)
    # Telegram xabar chegarasi ~4096 belgi, shuning uchun bo'lib yuboramiz
    chunk = ""
    for line in text.split("\n"):
        if len(chunk) + len(line) + 1 > 3800:
            await context.bot.send_message(chat_id=update.effective_chat.id, text=chunk, parse_mode=ParseMode.HTML)
            chunk = ""
        chunk += line + "\n"
    if chunk:
        await context.bot.send_message(chat_id=update.effective_chat.id, text=chunk, parse_mode=ParseMode.HTML)

    if total > 100:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"ℹ️ Faqat oxirgi 100 tasi ko'rsatildi (jami {total} ta).",
        )




async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Bu buyruq faqat adminlar uchun.")
        return

    users_count = await db.count_users()
    questions_count = await db.count_questions()
    results_count = await db.count_results()
    top = await db.get_top_results(10)
    categories = await db.get_categories()

    text = (
        "📊 <b>Statistika</b>\n\n"
        f"👥 Foydalanuvchilar: <b>{users_count}</b>\n"
        f"❓ Savollar bazasi: <b>{questions_count}</b>\n"
        f"📝 Topshirilgan testlar: <b>{results_count}</b>\n\n"
    )

    if categories:
        text += "📁 <b>Mavzular:</b>\n"
        for c in categories:
            text += f"• {c['category']}: {c['cnt']} ta savol\n"
        text += "\n"

    if top:
        text += "🏆 <b>Eng yaxshi natijalar:</b>\n"
        for i, r in enumerate(top, start=1):
            name = r["full_name"] or r["username"] or str(r["telegram_id"])
            wrong = r["total_questions"] - r["correct_count"]
            text += (
                f"{i}. {name} — ✅ {r['correct_count']} ta / ❌ {wrong} ta ({r['percentage']}%)\n"
            )

    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


# =====================================================================
#                              MAIN
# =====================================================================

async def post_init(application: Application):
    await db.init_db()
    logger.info("Ma'lumotlar bazasi tayyor.")


async def post_shutdown(application: Application):
    await db.close_db()


def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN topilmadi. .env faylini tekshiring.")

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    add_question_conv = ConversationHandler(
        entry_points=[CommandHandler("addquestion", addquestion_start)],
        states={
            ASK_CATEGORY: [MessageHandler(filters.TEXT & ~filters.COMMAND, addquestion_get_category)],
            ASK_QUESTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, addquestion_get_text)],
            ASK_CORRECT: [MessageHandler(filters.TEXT & ~filters.COMMAND, addquestion_get_correct)],
            ASK_WRONG1: [MessageHandler(filters.TEXT & ~filters.COMMAND, addquestion_get_wrong1)],
            ASK_WRONG2: [MessageHandler(filters.TEXT & ~filters.COMMAND, addquestion_get_wrong2)],
            ASK_WRONG3: [MessageHandler(filters.TEXT & ~filters.COMMAND, addquestion_get_wrong3)],
        },
        fallbacks=[CommandHandler("cancel", cancel), CommandHandler("done", addquestion_done)],
    )

    admin_login_conv = ConversationHandler(
        entry_points=[CommandHandler("admin", admin_start)],
        states={
            ADMIN_USERNAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_get_username)],
            ADMIN_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_get_password)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("stats", stats))
    application.add_handler(add_question_conv)
    application.add_handler(admin_login_conv)
    application.add_handler(CallbackQueryHandler(start_quiz_callback, pattern="^start_quiz$"))
    application.add_handler(CallbackQueryHandler(my_stats_callback, pattern="^my_stats$"))
    application.add_handler(CallbackQueryHandler(choose_category_callback, pattern="^cat_"))
    application.add_handler(CallbackQueryHandler(choose_count_callback, pattern="^count_"))
    application.add_handler(CallbackQueryHandler(ai_chat_start, pattern="^ai_chat$"))
    application.add_handler(CallbackQueryHandler(ai_chat_stop, pattern="^ai_chat_stop$"))
    application.add_handler(CallbackQueryHandler(admin_users_callback, pattern="^admin_users$"))
    application.add_handler(PollAnswerHandler(poll_answer_callback))
    # E'tibor bering: bu handler ENG OXIRIDA qo'shilishi kerak, aks holda
    # /addquestion suhbati ichidagi matn xabarlarini "yeb qo'yishi" mumkin.
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, ai_chat_message))

    logger.info("Bot ishga tushdi...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()