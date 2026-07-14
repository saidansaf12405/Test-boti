import asyncio
import os
from dotenv import load_dotenv
import asyncpg

load_dotenv()
url = os.getenv("DATABASE_URL")
print("URL host:", url.split("@")[-1] if url else None)


async def main():
    try:
        c = await asyncpg.connect(dsn=url, timeout=5)
        n = await c.fetchval("SELECT COUNT(*) FROM questions")
        cats = await c.fetch(
            "SELECT category, COUNT(*) c FROM questions GROUP BY category ORDER BY 1"
        )
        print("questions:", n)
        for r in cats:
            print(f"  {r['category']}: {r['c']}")
        await c.close()
    except Exception as e:
        print("ERROR:", type(e).__name__, e)


asyncio.run(main())
