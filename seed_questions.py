"""
Savollarni bazaga qo'shuvchi skript.

Ishlatish:
    1) Shu faylni (seed_questions.py) va questions_part1.sql, questions_part2.sql
       fayllarini bot papkasiga (main.py, db.py, config.py bilan bir joyga) qo'ying.
    2) VS Code terminalida (bot uchun ishlatilayotgan virtual muhitda) shuni ishga tushiring:

           python seed_questions.py

    3) Tayyor bo'lgach konsolda har bir kategoriyada nechta savol borligi chiqadi.

Yangi qism fayllari (masalan questions_part3.sql) qo'shilganda,
pastdagi SQL_FILES ro'yxatiga nomini qo'shib qo'ying.
"""

import asyncio
import os

import asyncpg
from dotenv import load_dotenv

load_dotenv()
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:1234@localhost:5432/test_bot")

# Ishga tushiriladigan SQL fayllar, tartib bilan
SQL_FILES = [
    "questions_part1.sql",
    "questions_part2.sql",
    # "questions_part3.sql",
    # "questions_part4.sql",
]

# "Xavfsizlik texnikasi" kategoriyasini har 20 tadan qilib
# "Xavfsizlik texnikasi 1", "Xavfsizlik texnikasi 2" ... nomlarga bo'lib chiqadi
SPLIT_SQL = """
WITH numbered AS (
    SELECT id, ROW_NUMBER() OVER (ORDER BY id) AS rn
    FROM questions
    WHERE category = 'Xavfsizlik texnikasi'
)
UPDATE questions q
SET category = 'Xavfsizlik texnikasi ' || CEIL(n.rn::numeric / 20)
FROM numbered n
WHERE q.id = n.id;
"""

CREATE_TABLE_SQL = """
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
"""


async def main():
    print(f"🔌 Bazaga ulanmoqda: {DATABASE_URL.split('@')[-1]}")
    conn = await asyncpg.connect(dsn=DATABASE_URL)
    try:
        await conn.execute(CREATE_TABLE_SQL)

        for filename in SQL_FILES:
            path = os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)
            if not os.path.exists(path):
                print(f"⚠️  Fayl topilmadi, o'tkazib yuborildi: {filename}")
                continue

            sql = open(path, encoding="utf-8").read()
            print(f"➡️  Ishga tushirilyapti: {filename}")
            await conn.execute(sql)
            print(f"✅  Bajarildi: {filename}")

        print("➡️  'Xavfsizlik texnikasi' kategoriyasi guruhlarga bo'linmoqda...")
        await conn.execute(SPLIT_SQL)
        print("✅  Kategoriyalar bo'lindi.")

        rows = await conn.fetch(
            "SELECT category, COUNT(*) AS cnt FROM questions GROUP BY category ORDER BY category"
        )
        print("\n📊 Hozirgi holat:")
        for r in rows:
            print(f"   {r['category']}: {r['cnt']} ta savol")

    finally:
        await conn.close()
        print("\n🏁 Tayyor!")


if __name__ == "__main__":
    asyncio.run(main())