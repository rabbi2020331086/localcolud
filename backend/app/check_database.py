import asyncio
import sys

from app.infrastructure.database import close_database, initialize_database


async def check() -> None:
    try:
        await initialize_database()
    except Exception as exc:
        print(f"MongoDB connection failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
    finally:
        await close_database()
    print("MongoDB connection and indexes are ready.")


if __name__ == "__main__":
    asyncio.run(check())
