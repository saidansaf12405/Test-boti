import asyncpg
from config import DATABASE_URL

pool: asyncpg.Pool | None = None

CREATE_TABLES_SQL = """
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    telegram_id BIGINT UNIQUE NOT NULL,
    username TEXT,
    full_name TEXT,
    joined_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS questions (
    id SERIAL PRIMARY KEY,
    question_text TEXT NOT NULL,
    correct_answer TEXT NOT NULL,
    wrong_answer1 TEXT NOT NULL,
    wrong_answer2 TEXT NOT NULL,
    wrong_answer3 TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT 'Umumiy',
    created_by BIGINT,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS results (
    id SERIAL PRIMARY KEY,
    telegram_id BIGINT NOT NULL,
    category TEXT,
    total_questions INT NOT NULL,
    correct_count INT NOT NULL,
    percentage NUMERIC(5,2) NOT NULL,
    created_at TIMESTAMP DEFAULT NOW()
);

-- Eski bazalarda ustunlar bo'lmasa, qo'shib qo'yamiz
ALTER TABLE questions ADD COLUMN IF NOT EXISTS category TEXT NOT NULL DEFAULT 'Umumiy';
ALTER TABLE questions ADD COLUMN IF NOT EXISTS wrong_answer3 TEXT NOT NULL DEFAULT '';
ALTER TABLE results ADD COLUMN IF NOT EXISTS category TEXT;

-- Foydalanuvchi yozgan matnga o'xshash savolni tez topish uchun indeks
CREATE INDEX IF NOT EXISTS idx_questions_trgm
    ON questions USING gin (question_text gin_trgm_ops);
"""


async def init_db():
    """Postgres poolni ochadi va jadvallarni (agar bo'lmasa) yaratadi."""
    global pool
    pool = await asyncpg.create_pool(dsn=DATABASE_URL, min_size=1, max_size=10)
    async with pool.acquire() as conn:
        await conn.execute(CREATE_TABLES_SQL)


async def close_db():
    if pool:
        await pool.close()


# ---------------- USERS ----------------

async def upsert_user(telegram_id: int, username: str | None, full_name: str | None):
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO users (telegram_id, username, full_name)
            VALUES ($1, $2, $3)
            ON CONFLICT (telegram_id)
            DO UPDATE SET username = EXCLUDED.username, full_name = EXCLUDED.full_name
            """,
            telegram_id, username, full_name,
        )


async def count_users() -> int:
    async with pool.acquire() as conn:
        return await conn.fetchval("SELECT COUNT(*) FROM users")


async def get_all_users(limit: int = 100, offset: int = 0):
    """Botdan foydalangan barcha foydalanuvchilar ro'yxati (eng yangisi birinchi)."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT telegram_id, username, full_name, joined_at
            FROM users
            ORDER BY joined_at DESC
            LIMIT $1 OFFSET $2
            """,
            limit, offset,
        )
        return rows


# ---------------- QUESTIONS ----------------

async def add_question(
    question_text: str,
    correct: str,
    wrong1: str,
    wrong2: str,
    wrong3: str,
    category: str,
    created_by: int,
):
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO questions
                (question_text, correct_answer, wrong_answer1, wrong_answer2, wrong_answer3, category, created_by)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            """,
            question_text, correct, wrong1, wrong2, wrong3, category, created_by,
        )


async def count_questions(category: str | None = None) -> int:
    async with pool.acquire() as conn:
        if category:
            return await conn.fetchval(
                "SELECT COUNT(*) FROM questions WHERE category = $1", category
            )
        return await conn.fetchval("SELECT COUNT(*) FROM questions")


async def get_categories():
    """Har bir mavzu va shu mavzudagi savollar sonini qaytaradi."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT category, COUNT(*) AS cnt
            FROM questions
            GROUP BY category
            ORDER BY category
            """
        )
        return rows


async def get_random_questions(limit: int, category: str | None = None):
    async with pool.acquire() as conn:
        if category:
            rows = await conn.fetch(
                """
                SELECT id, question_text, correct_answer, wrong_answer1, wrong_answer2, wrong_answer3
                FROM questions
                WHERE category = $1
                ORDER BY RANDOM()
                LIMIT $2
                """,
                category, limit,
            )
        else:
            rows = await conn.fetch(
                """
                SELECT id, question_text, correct_answer, wrong_answer1, wrong_answer2, wrong_answer3
                FROM questions
                ORDER BY RANDOM()
                LIMIT $1
                """,
                limit,
            )
        return rows


async def find_similar_question(text: str, threshold: float = 0.20):
    """Foydalanuvchi yozgan matnga bazadagi eng o'xshash savolni topadi.

    threshold - o'xshashlik darajasi (0..1). Qancha yuqori bo'lsa,
    shuncha aniq mos kelishi talab qilinadi. Past bo'lsa - ko'proq narsaga
    "javob topdim" deydi, lekin noto'g'ri javob berish xavfi oshadi.
    """
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT question_text, correct_answer, category,
                   similarity(question_text, $1) AS score
            FROM questions
            ORDER BY similarity(question_text, $1) DESC
            LIMIT 1
            """,
            text,
        )
        if row and row["score"] >= threshold:
            return row
        return None


# ---------------- RESULTS ----------------

async def save_result(telegram_id: int, total: int, correct: int, percentage: float, category: str | None = None):
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO results (telegram_id, category, total_questions, correct_count, percentage)
            VALUES ($1, $2, $3, $4, $5)
            """,
            telegram_id, category, total, correct, percentage,
        )


async def count_results() -> int:
    async with pool.acquire() as conn:
        return await conn.fetchval("SELECT COUNT(*) FROM results")


async def get_top_results(limit: int = 10):
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT r.telegram_id, u.full_name, u.username, r.total_questions,
                   r.correct_count, r.percentage, r.created_at
            FROM results r
            LEFT JOIN users u ON u.telegram_id = r.telegram_id
            ORDER BY r.percentage DESC, r.created_at DESC
            LIMIT $1
            """,
            limit,
        )
        return rows


async def get_user_results(telegram_id: int, limit: int = 10):
    """Foydalanuvchining oxirgi urinishlari."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT category, total_questions, correct_count, percentage, created_at
            FROM results
            WHERE telegram_id = $1
            ORDER BY created_at DESC
            LIMIT $2
            """,
            telegram_id, limit,
        )
        return rows


async def get_user_summary(telegram_id: int):
    """Foydalanuvchi bo'yicha umumlashtirilgan statistika: nechta test, o'rtacha va eng yaxshi foiz."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT COUNT(*) AS attempts,
                   COALESCE(AVG(percentage), 0) AS avg_percentage,
                   COALESCE(MAX(percentage), 0) AS best_percentage
            FROM results
            WHERE telegram_id = $1
            """,
            telegram_id,
        )
        return row