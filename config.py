import os
from dotenv import load_dotenv

load_dotenv()

# .env faylidan yoki muhit (environment) o'zgaruvchilaridan olinadi
BOT_TOKEN = os.getenv("BOT_TOKEN", "bilim_test_bot")
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:1234@localhost:5432/test_bot")

# /admin panelga kirish uchun login va parol (.env faylida belgilanadi)
ADMIN_PANEL_USERNAME = os.getenv("ADMIN_PANEL_USERNAME", "admin")
ADMIN_PANEL_PASSWORD = os.getenv("ADMIN_PANEL_PASSWORD", "661313")

# ============================================
# ADMIN TELEGRAM ID'LARI
# Bu yerga o'zingizning (va boshqa adminlarning)
# Telegram ID raqamingizni qo'shing.
# ID'ingizni bilish uchun @userinfobot ga /start yozing.
# ============================================
ADMIN_IDS = [
    7535530521  # <-- shu raqamni o'z Telegram ID'ingizga almashtiring
]

# Testda savollar sonini tanlash uchun variantlar
QUESTION_COUNT_OPTIONS = [5, 10, 15, 20, 40, 50, 100]