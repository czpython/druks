from sqlalchemy.orm import Session

from druks.accounts.constants import SYSTEM_ACCOUNT_ID
from druks.accounts.models import Account


def seed(engine) -> None:
    # Everything a fresh install needs beyond the schema. Idempotent; engine-
    # bound because it runs at the migrate step, where the scoped session
    # isn't bound.
    seed_system_account(engine)


def seed_system_account(engine) -> None:
    # Owns every run nobody asked for: crons, background work.
    with Session(engine) as session:
        if not session.get(Account, SYSTEM_ACCOUNT_ID):
            session.add(Account(id=SYSTEM_ACCOUNT_ID, username="system"))
            session.commit()
