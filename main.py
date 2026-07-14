import asyncio
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
from config import (
    BOT_TOKEN, ADMIN_IDS, QUESTION_COUNT_OPTIONS,
    ADMIN_PANEL_USERNAME, ADMIN_PANEL_PASSWORD, LOCAL_LLM_PATH,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ---------------- Conversation states (admin savol qo'shish) ----------------
ASK_CATEGORY, ASK_QUESTION, ASK_CORRECT, ASK_WRONG1, ASK_WRONG2, ASK_WRONG3 = range(6)
ADMIN_USERNAME, ADMIN_PASSWORD = range(6, 8)
EDIT_MENU, EDIT_WAIT_VALUE = range(8, 10)

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
                "id": r["id"],
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

    # Har bir savol bo'yicha statistika uchun urinishni yozib qo'yamiz.
    # Bu ishlamay qolsa ham (masalan vaqtinchalik baza muammosi) testning
    # o'zi davom etishi kerak, shuning uchun xatoni yutib yuboramiz.
    try:
        await db.log_attempt(q["id"], poll_answer.user.id, is_correct)
    except Exception:
        logger.exception("Urinishni yozishda xato (question_id=%s)", q.get("id"))

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
    context.user_data.pop("edit_qid", None)
    context.user_data.pop("edit_field", None)
    if added_count:
        await update.message.reply_text(
            f"❌ Bekor qilindi. Bu seansda {added_count} ta savol saqlangan edi."
        )
    else:
        await update.message.reply_text("❌ Bekor qilindi.")
    return ConversationHandler.END


# =====================================================================
#                  /editquestion — SAVOLNI TAHRIRLASH
# =====================================================================

EDIT_FIELD_LABELS = {
    "question_text": "Savol matni",
    "correct_answer": "To'g'ri javob",
    "wrong_answer1": "Noto'g'ri javob 1",
    "wrong_answer2": "Noto'g'ri javob 2",
    "wrong_answer3": "Noto'g'ri javob 3",
    "category": "Kategoriya",
}


def _edit_menu_text(q) -> str:
    return (
        f"✏️ <b>Savolni tahrirlash</b> (ID: {q['id']})\n\n"
        f"📁 Kategoriya: {q['category']}\n"
        f"❓ Savol: {q['question_text']}\n"
        f"✅ To'g'ri: {q['correct_answer']}\n"
        f"❌ Noto'g'ri 1: {q['wrong_answer1']}\n"
        f"❌ Noto'g'ri 2: {q['wrong_answer2']}\n"
        f"❌ Noto'g'ri 3: {q['wrong_answer3']}\n\n"
        "Qaysi maydonni o'zgartirmoqchisiz?"
    )


def _edit_menu_keyboard() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(label, callback_data=f"editf_{field}")]
            for field, label in EDIT_FIELD_LABELS.items()]
    rows.append([InlineKeyboardButton("✅ Tugatish", callback_data="editf_done")])
    return InlineKeyboardMarkup(rows)


async def editquestion_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Bu buyruq faqat adminlar uchun.")
        return ConversationHandler.END

    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text(
            "Foydalanish: <code>/editquestion ID</code>\n"
            "Masalan: <code>/editquestion 42</code>\n\n"
            "ID raqamini admin statistikasidagi savollar ro'yxatidan yoki "
            "bazadan (<code>SELECT id, question_text FROM questions ...</code>) topishingiz mumkin.",
            parse_mode=ParseMode.HTML,
        )
        return ConversationHandler.END

    qid = int(context.args[0])
    q = await db.get_question_by_id(qid)
    if not q:
        await update.message.reply_text(f"⚠️ ID={qid} bilan savol topilmadi.")
        return ConversationHandler.END

    context.user_data["edit_qid"] = qid
    await update.message.reply_text(
        _edit_menu_text(q), parse_mode=ParseMode.HTML, reply_markup=_edit_menu_keyboard()
    )
    return EDIT_MENU


async def editquestion_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    field = query.data.split("editf_", 1)[1]

    if field == "done":
        context.user_data.pop("edit_qid", None)
        context.user_data.pop("edit_field", None)
        await query.edit_message_text("✅ Tahrirlash tugatildi.")
        return ConversationHandler.END

    context.user_data["edit_field"] = field
    await query.edit_message_text(
        f"✏️ Yangi qiymatni yuboring — <b>{EDIT_FIELD_LABELS[field]}</b>:",
        parse_mode=ParseMode.HTML,
    )
    return EDIT_WAIT_VALUE


async def editquestion_receive_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    qid = context.user_data.get("edit_qid")
    field = context.user_data.get("edit_field")
    new_value = update.message.text.strip()

    if not qid or not field:
        await update.message.reply_text("⚠️ Sessiya topilmadi, qaytadan /editquestion bilan boshlang.")
        return ConversationHandler.END

    try:
        await db.update_question_field(qid, field, new_value)
    except Exception:
        logger.exception("Savolni yangilashda xato (id=%s, field=%s)", qid, field)
        await update.message.reply_text("⚠️ Yangilashda xato yuz berdi.")
        return ConversationHandler.END

    q = await db.get_question_by_id(qid)
    await update.message.reply_text("✅ Yangilandi!")
    await update.message.reply_text(
        _edit_menu_text(q), parse_mode=ParseMode.HTML, reply_markup=_edit_menu_keyboard()
    )
    return EDIT_MENU


# =====================================================================
#                  /delquestion — SAVOLNI O'CHIRISH
# =====================================================================

async def delquestion_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Bu buyruq faqat adminlar uchun.")
        return

    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text(
            "Foydalanish: <code>/delquestion ID</code>\n"
            "Masalan: <code>/delquestion 42</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    qid = int(context.args[0])
    q = await db.get_question_by_id(qid)
    if not q:
        await update.message.reply_text(f"⚠️ ID={qid} bilan savol topilmadi.")
        return

    qtext = q["question_text"]
    if len(qtext) > 200:
        qtext = qtext[:200] + "…"

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🗑 Ha, o'chirish", callback_data=f"delq_yes_{qid}"),
                InlineKeyboardButton("❌ Bekor qilish", callback_data="delq_no"),
            ]
        ]
    )
    await update.message.reply_text(
        f"⚠️ Quyidagi savolni butunlay o'chirmoqchimisiz? Bu amalni qaytarib bo'lmaydi.\n\n"
        f"[ID:{qid}, {q['category']}]\n{qtext}",
        reply_markup=keyboard,
    )


async def delquestion_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not is_admin(update.effective_user.id):
        await query.edit_message_text("⛔ Bu buyruq faqat adminlar uchun.")
        return

    if query.data == "delq_no":
        await query.edit_message_text("❌ Bekor qilindi.")
        return

    qid = int(query.data.split("_")[-1])
    try:
        ok = await db.delete_question(qid)
    except Exception:
        logger.exception("Savolni o'chirishda xato (id=%s)", qid)
        await query.edit_message_text("⚠️ O'chirishda xato yuz berdi.")
        return

    if ok:
        await query.edit_message_text(f"🗑 Savol (ID:{qid}) muvaffaqiyatli o'chirildi.")
    else:
        await query.edit_message_text(f"⚠️ Savol (ID:{qid}) topilmadi yoki allaqachon o'chirilgan.")


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


# =====================================================================
#              LOKAL AI (API kalitisiz, kompyuter/server ichida)
# =====================================================================

_local_llm = None
_local_llm_load_failed = False


def _get_local_llm():
    """Modelni faqat birinchi so'ralganda yuklaydi (sekin, shuning uchun
    dastur ishga tushishida emas, birinchi savolda yuklanadi)."""
    global _local_llm, _local_llm_load_failed
    if _local_llm is not None or _local_llm_load_failed:
        return _local_llm
    if not LOCAL_LLM_PATH:
        return None
    try:
        from llama_cpp import Llama
        logger.info("Lokal AI modeli yuklanmoqda: %s", LOCAL_LLM_PATH)
        _local_llm = Llama(
            model_path=LOCAL_LLM_PATH,
            n_ctx=2048,
            n_threads=4,
            verbose=False,
        )
        logger.info("Lokal AI modeli tayyor.")
    except Exception:
        logger.exception("Lokal AI modelini yuklashda xato.")
        _local_llm_load_failed = True
        _local_llm = None
    return _local_llm


LOCAL_LLM_SYSTEM_PROMPT = (
    "Siz o'zbek tilida javob beruvchi yordamchisiz. "
    "Javoblaringiz qisqa, aniq va tushunarli bo'lsin."
)


def _run_local_llm(user_text: str):
    llm = _get_local_llm()
    if llm is None:
        return None
    try:
        result = llm.create_chat_completion(
            messages=[
                {"role": "system", "content": LOCAL_LLM_SYSTEM_PROMPT},
                {"role": "user", "content": user_text},
            ],
            max_tokens=300,
            temperature=0.7,
        )
        return result["choices"][0]["message"]["content"].strip()
    except Exception:
        logger.exception("Lokal AI javob generatsiyasida xato.")
        return None


async def generate_local_ai_reply(user_text: str):
    """CPU-bog'liq (blocking) generatsiyani alohida thread'da ishga tushiradi,
    shunda bot boshqa foydalanuvchilarga javob berishni to'xtatmaydi."""
    return await asyncio.to_thread(_run_local_llm, user_text)


GREETINGS = ["salom", "assalomu alaykum", "assalom", "hi", "hello", "salomlar", "vazalom"]
HOWAREYOU = ["qalaysan", "qanaqasan", "yaxshimisan", "qandaysan", "ahvoling qanday"]
HELP_TRIGGERS = [
    "bu bot nima qiladi", "bot nima qiladi", "nima qila olasan", "nima qilolasan",
    "nima qila oladi", "sen kimsan", "sen nimasan", "kimsan o'zing", "botni tanishtir",
    "qanday ishlaysan", "imkoniyatlaring",
]


def _matches_any(text: str, patterns) -> bool:
    return any(p in text for p in patterns)


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

    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("🔙 Suhbatni tugatish", callback_data="ai_chat_stop")]]
    )

    lower = user_text.lower()

    # Qisqa, oddiy salomlashish/hol-ahvol xabarlariga tabiiy javob beramiz
    # (uzunroq xabarlar haqiqiy test savoli bo'lishi mumkin, shuning uchun
    # bu tekshiruvni faqat qisqa xabarlarga qo'llaymiz)
    if len(user_text) <= 40:
        if _matches_any(lower, GREETINGS):
            await update.message.reply_text(
                "👋 Salom! Men test savollariga oid kichik yordamchiman.\n"
                "Menga test mavzusidagi biror savolni yozib ko'ring, masalan: "
                "\"Naryad necha kunga beriladi?\"",
                reply_markup=keyboard,
            )
            return
        if _matches_any(lower, HOWAREYOU):
            await update.message.reply_text(
                "😊 Yaxshi, rahmat! Menga test mavzusidan biror savol yozsangiz, "
                "bazadan javobini topib beraman.",
                reply_markup=keyboard,
            )
            return

    if _matches_any(lower, HELP_TRIGGERS):
        await update.message.reply_text(
            "🤖 Men shu botning kichik yordamchisiman (katta AI emasman).\n\n"
            "<b>Nima qila olaman:</b>\n"
            "• Test mavzulariga oid savollaringizga bazadagi savollar ichidan "
            "eng yaqin javobni topib beraman.\n"
            "  Masalan: <i>\"Naryad necha kunga beriladi?\"</i>\n\n"
            "<b>Nima qila olmayman:</b>\n"
            "• Test mavzusidan tashqari chuqur/umumiy savollarga to'liq javob berolmayman.\n\n"
            "Testni topshirish uchun asosiy menyudagi \"🚀 Testni boshlash\" tugmasidan foydalaning.",
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
        )
        return

    match = await db.find_similar_question(user_text, SIMILARITY_THRESHOLD)

    if match:
        reply = (
            f"📖 <b>Topilgan savol</b> ({match['category']}):\n"
            f"{match['question_text']}\n\n"
            f"✅ <b>Javob:</b> {match['correct_answer']}"
        )
        await update.message.reply_text(reply, parse_mode=ParseMode.HTML, reply_markup=keyboard)
        return

    # Bazadan aniq javob topilmadi — agar lokal AI model sozlangan bo'lsa, undan foydalanamiz
    if LOCAL_LLM_PATH:
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
        ai_reply = await generate_local_ai_reply(user_text)
        if ai_reply:
            await update.message.reply_text(
                f"🤖 {ai_reply}\n\n"
                "<i>⚠️ Bu javob bazadagi tayyor javob emas, kichik lokal AI model "
                "tomonidan generatsiya qilindi — noaniq bo'lishi mumkin.</i>",
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
            )
            return

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
            [
                [InlineKeyboardButton("👥 Foydalanuvchilar ro'yxati", callback_data="admin_users")],
                [InlineKeyboardButton("📈 Umumiy statistika", callback_data="admin_stats")],
            ]
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

    lines = [f"👥 Jami foydalanuvchilar: {total}\n"]
    for i, u in enumerate(users, start=1):
        name = u["full_name"] or "—"
        uname = f"@{u['username']}" if u["username"] else "—"
        joined = u["joined_at"].strftime("%Y-%m-%d %H:%M") if u["joined_at"] else "—"
        lines.append(f"{i}. {name} ({uname}) — ID: {u['telegram_id']} — {joined}")

    text = "\n".join(lines)
    # Telegram xabar chegarasi ~4096 belgi, shuning uchun bo'lib yuboramiz
    chunks = []
    chunk = ""
    for line in text.split("\n"):
        if len(chunk) + len(line) + 1 > 3800:
            chunks.append(chunk)
            chunk = ""
        chunk += line + "\n"
    if chunk:
        chunks.append(chunk)

    for c in chunks:
        try:
            await context.bot.send_message(chat_id=update.effective_chat.id, text=c)
        except Exception:
            logger.exception("Foydalanuvchilar ro'yxatini yuborishda xato.")
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="⚠️ Ro'yxatning bir qismini yuborishda xato yuz berdi (log'ga yozildi).",
            )

    if total > 100:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"ℹ️ Faqat oxirgi 100 tasi ko'rsatildi (jami {total} ta).",
        )


async def admin_stats_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not context.user_data.get("admin_authed"):
        await query.edit_message_text("⚠️ Avval /admin orqali kiring.")
        return

    stats = await db.get_overall_stats()
    hardest = await db.get_hardest_questions(limit=10, min_attempts=5)

    lines = [
        "📈 Umumiy statistika\n",
        f"👥 Foydalanuvchilar: {stats['users']}",
        f"❓ Bazadagi savollar: {stats['questions']}",
        f"📝 Topshirilgan testlar: {stats['quiz_attempts']}",
        f"✅ Javob berilgan savollar (jami): {stats['answers_logged']}",
        f"📊 O'rtacha natija: {stats['avg_percentage']}%\n",
    ]

    if hardest:
        lines.append("🔥 Eng ko'p xato qilingan savollar (kamida 5 marta so'ralgan):\n")
        for i, h in enumerate(hardest, start=1):
            qtext = h["question_text"]
            if len(qtext) > 90:
                qtext = qtext[:90] + "…"
            lines.append(
                f"{i}. [ID:{h['id']}] {qtext}\n"
                f"    ❌ {h['wrong_pct']}% xato ({h['wrong_count']}/{h['total_attempts']} urinish) — {h['category']}"
            )
    else:
        lines.append("ℹ️ Hali yetarli statistika yig'ilmagan (kamida 5 marta javob berilgan savol yo'q).")

    text = "\n".join(lines)
    chunks = []
    chunk = ""
    for line in text.split("\n"):
        if len(chunk) + len(line) + 1 > 3800:
            chunks.append(chunk)
            chunk = ""
        chunk += line + "\n"
    if chunk:
        chunks.append(chunk)

    for c in chunks:
        try:
            await context.bot.send_message(chat_id=update.effective_chat.id, text=c)
        except Exception:
            logger.exception("Statistikani yuborishda xato.")
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="⚠️ Hisobotning bir qismini yuborishda xato yuz berdi (log'ga yozildi).",
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

    edit_question_conv = ConversationHandler(
        entry_points=[CommandHandler("editquestion", editquestion_start)],
        states={
            EDIT_MENU: [CallbackQueryHandler(editquestion_menu_callback, pattern="^editf_")],
            EDIT_WAIT_VALUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, editquestion_receive_value)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("stats", stats))
    application.add_handler(add_question_conv)
    application.add_handler(admin_login_conv)
    application.add_handler(edit_question_conv)
    application.add_handler(CommandHandler("delquestion", delquestion_start))
    application.add_handler(CallbackQueryHandler(delquestion_confirm_callback, pattern="^delq_"))
    application.add_handler(CallbackQueryHandler(start_quiz_callback, pattern="^start_quiz$"))
    application.add_handler(CallbackQueryHandler(my_stats_callback, pattern="^my_stats$"))
    application.add_handler(CallbackQueryHandler(choose_category_callback, pattern="^cat_"))
    application.add_handler(CallbackQueryHandler(choose_count_callback, pattern="^count_"))
    application.add_handler(CallbackQueryHandler(ai_chat_start, pattern="^ai_chat$"))
    application.add_handler(CallbackQueryHandler(ai_chat_stop, pattern="^ai_chat_stop$"))
    application.add_handler(CallbackQueryHandler(admin_users_callback, pattern="^admin_users$"))
    application.add_handler(CallbackQueryHandler(admin_stats_callback, pattern="^admin_stats$"))
    application.add_handler(PollAnswerHandler(poll_answer_callback))
    # E'tibor bering: bu handler ENG OXIRIDA qo'shilishi kerak, aks holda
    # /addquestion suhbati ichidagi matn xabarlarini "yeb qo'yishi" mumkin.
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, ai_chat_message))

    logger.info("Bot ishga tushdi...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()