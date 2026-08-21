"""Create one login per role and link them to generated clinical records.

`generate_data.py` produces doctors and patients but no accounts, so after
seeding there is no way to actually sign in. This closes that gap: it creates an
admin, a doctor, and a patient, and links the latter two to the *busiest*
generated records so every dashboard has something real to show.

    uv run python scripts/create_demo_users.py

The password is printed rather than hidden, because these are demo accounts on
synthetic data and pretending otherwise would be theatre. They are still created
through the same hashing path as any other account.

Refuses to run against a database whose ENVIRONMENT is production — demo
credentials with a published password must never exist on a real deployment.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.config import settings
from app.core.security import hash_password
from app.models import Doctor, Patient, User, UserRole

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("create_demo_users")

DEMO_PASSWORD = "clinetics-demo-password"
ACCOUNTS = [
    ("admin@clinetics.example.com", UserRole.ADMIN, "Alex Admin"),
    ("doctor@clinetics.example.com", UserRole.DOCTOR, "Dana Doctor"),
    ("patient@clinetics.example.com", UserRole.PATIENT, "Pat Patient"),
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--password",
        default=DEMO_PASSWORD,
        help="Override the demo password (must be at least 12 characters)",
    )
    args = parser.parse_args()

    if settings.is_production:
        log.error("refusing to create demo accounts in a production environment")
        return 1
    if len(args.password) < 12:
        log.error("password must be at least 12 characters, matching the registration rule")
        return 1

    engine = create_engine(settings.database_url_sync, future=True)
    with Session(engine) as session:
        created: list[str] = []
        for email, role, full_name in ACCOUNTS:
            user = session.scalar(select(User).where(User.email == email))
            if user is None:
                user = User(
                    email=email,
                    hashed_password=hash_password(args.password),
                    full_name=full_name,
                    role=role,
                    is_active=True,
                )
                session.add(user)
                session.flush()
                created.append(email)
            else:
                # Idempotent: re-running resets the password rather than failing,
                # which is what you want when reseeding a demo.
                user.hashed_password = hash_password(args.password)
                user.role = role

            # Link to the record with the most appointments, so the dashboards
            # are populated rather than technically-correct-but-empty.
            if role is UserRole.DOCTOR:
                busiest = session.scalar(
                    text(
                        "SELECT doctor_id FROM appointment GROUP BY doctor_id "
                        "ORDER BY count(*) DESC LIMIT 1"
                    )
                )
                if busiest is not None:
                    session.execute(
                        text("UPDATE doctor SET user_id = NULL WHERE user_id = :u"),
                        {"u": user.id},
                    )
                    doctor = session.get(Doctor, busiest)
                    if doctor is not None:
                        doctor.user_id = user.id
            elif role is UserRole.PATIENT:
                busiest = session.scalar(
                    text(
                        "SELECT patient_id FROM appointment GROUP BY patient_id "
                        "ORDER BY count(*) DESC LIMIT 1"
                    )
                )
                if busiest is not None:
                    session.execute(
                        text("UPDATE patient SET user_id = NULL WHERE user_id = :u"),
                        {"u": user.id},
                    )
                    patient = session.get(Patient, busiest)
                    if patient is not None:
                        patient.user_id = user.id

        session.commit()

    print("\n" + "=" * 62)
    print("DEMO ACCOUNTS (synthetic data only)")
    print("=" * 62)
    for email, role, _ in ACCOUNTS:
        print(f"  {role.value:<8} {email}")
    print(f"\n  password: {args.password}")
    print("=" * 62)
    if created:
        log.info("created %d new account(s); existing ones had their password reset", len(created))
    else:
        log.info("all accounts already existed; passwords reset")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
