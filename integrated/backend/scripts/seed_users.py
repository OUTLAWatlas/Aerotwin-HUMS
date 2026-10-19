"""
Seed demo users for local development -- the FastAPI-side equivalent of the
old Node server's `server/seed.js`. Uses the same demo usernames/passwords
so anyone testing against either backend (or switching between them) can
use the same login form.

Drones/engines don't need seeding here -- they're already inserted by
app/db/schema.sql (see the "Seed data mirroring config/drone_configs.yaml"
section), which must be applied first.

Run once, after the schema is applied and PM_DATABASE_URL is reachable:
    python -m scripts.seed_users
"""
from __future__ import annotations

import asyncio

from sqlalchemy import select

from app.core.security import hash_password
from app.db.models import User
from app.db.session import get_sessionmaker

# role must match a key in config/roles.yaml -- "pilot" was added there
# specifically to match the AeroTwin-HUMS dashboard's role names.
SEED_USERS = [
    {"user_id": "admin", "password": "admin123", "role": "admin"},
    {"user_id": "engineer", "password": "engineer123", "role": "engineer"},
    {"user_id": "pilot", "password": "pilot123", "role": "pilot"},
    {"user_id": "viewer", "password": "viewer123", "role": "viewer"},
]


async def seed() -> None:
    Session = get_sessionmaker()
    async with Session() as session:
        for u in SEED_USERS:
            existing = await session.execute(select(User).where(User.user_id == u["user_id"]))
            row = existing.scalar_one_or_none()
            if row is not None:
                row.hashed_password = hash_password(u["password"])
                row.role = u["role"]
            else:
                session.add(User(
                    user_id=u["user_id"],
                    hashed_password=hash_password(u["password"]),
                    role=u["role"],
                ))
        await session.commit()

    print("Seeded demo users:")
    for u in SEED_USERS:
        print(f"  - {u['user_id']} / {u['password']}  ({u['role']})")


if __name__ == "__main__":
    asyncio.run(seed())
