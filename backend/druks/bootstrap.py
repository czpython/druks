from sqlalchemy import select

from druks.accounts.constants import SYSTEM_ACCOUNT_ID
from druks.accounts.models import Account
from druks.database import get_session
from druks.harnesses.registry import get_harnesses
from druks.user_settings.models import HarnessSettings


def seed(engine) -> None:
    # Everything a fresh install needs beyond the schema. Idempotent; engine-
    # bound because it runs at the migrate step, where the scoped session
    # isn't bound.
    seed_harnesses(engine)
    seed_system_account(engine)


def seed_harnesses(engine) -> None:
    # Give every registered harness a config row at its shipped defaults. A
    # harness added later is seeded on the deploy that adds it; existing rows
    # keep whatever the operator tuned.
    with get_session(engine) as session:
        existing = set(session.execute(select(HarnessSettings.name)).scalars())
        for harness in get_harnesses():
            if harness.name not in existing:
                session.add(
                    HarnessSettings(
                        name=harness.name,
                        model=harness.default_model,
                        effort=harness.default_effort,
                        timeout=harness.default_timeout,
                    )
                )
        session.commit()


def seed_system_account(engine) -> None:
    # Owns every run nobody asked for: crons, background work.
    with get_session(engine) as session:
        if not session.get(Account, SYSTEM_ACCOUNT_ID):
            session.add(Account(id=SYSTEM_ACCOUNT_ID, username="system"))
            session.commit()
