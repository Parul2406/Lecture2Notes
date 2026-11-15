"""Seed demo accounts so a fresh database can be used straight away.

    python db/seed.py                 # create the demo accounts (skips ones that already exist)
    python db/seed.py --reset-passwords   # also set existing demo accounts back to the demo password

Creates the tables if needed (same as starting the app). Uses DATABASE_URL / .env like the app does.
Keep this file in sync when demo accounts or the data model change.
"""

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from app.database import Base, SessionLocal, add_missing_columns, engine  # noqa: E402
from app.models import Role, User  # noqa: E402
from app.security import hash_password  # noqa: E402

DEMO_PASSWORD = os.environ.get("SEED_PASSWORD", "test1234")

DEMO_USERS = [
    {"name": "Parul", "email": "parul@parul.com", "role": Role.TEACHER},
    {"name": "Parul", "email": "parul@student.com", "role": Role.STUDENT},
]


def seed(reset_passwords: bool = False) -> None:
    Base.metadata.create_all(engine)
    add_missing_columns()
    with SessionLocal() as db:
        for u in DEMO_USERS:
            user = db.scalar(select(User).where(User.email == u["email"]))
            if user is None:
                db.add(User(name=u["name"], email=u["email"], role=u["role"], password_hash=hash_password(DEMO_PASSWORD)))
                print(f"created  {u['role'].value:<8} {u['email']}")
            elif reset_passwords:
                user.password_hash = hash_password(DEMO_PASSWORD)
                print(f"reset    {u['role'].value:<8} {u['email']}")
            else:
                print(f"exists   {u['role'].value:<8} {u['email']}")
        db.commit()
    print(f"\nDemo password: {DEMO_PASSWORD}  (set SEED_PASSWORD to change it)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--reset-passwords", action="store_true", help="set existing demo accounts to the demo password")
    seed(parser.parse_args().reset_passwords)
