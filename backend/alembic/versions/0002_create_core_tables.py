"""Create the transactional core tables

Revision ID: 0002_core_tables
Revises: 0001_extensions
Create Date: 2026-08-21

The transactional heart of the schema, in `public`.

Includes the native ENUM types, all CHECK constraints, and every foreign key
with an explicit ON DELETE action. CHECKs are created with their tables
because a CHECK is part of a table's definition, not a separate object — a
row violating it must never be storable, including during this migration.

Two things are deliberately NOT here and get their own migrations:
  * the (doctor_id, appointment_date) index  -> 0005, so the EXPLAIN ANALYZE
    before/after comparison is reproducible against a real revision boundary
  * the doctor no-overlap EXCLUDE constraint -> 0004, which needs raw SQL
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0002_core_tables"
down_revision: str | None = "0001_extensions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "clinic",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("opens_at", sa.Time(), nullable=False),
        sa.Column("closes_at", sa.Time(), nullable=False),
        sa.Column("timezone", sa.String(length=64), nullable=False),
        sa.Column("address", sa.String(length=255), nullable=True),
        sa.Column("phone", sa.String(length=32), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("closes_at > opens_at", name=op.f("ck_clinic_closes_after_opens")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_clinic")),
        sa.UniqueConstraint("name", name=op.f("uq_clinic_name")),
    )
    op.create_table(
        "specialty",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("slug", sa.String(length=80), nullable=False),
        sa.Column("default_duration_minutes", sa.Integer(), nullable=False),
        sa.Column("description", sa.String(length=255), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "default_duration_minutes <= 480", name=op.f("ck_specialty_duration_within_working_day")
        ),
        sa.CheckConstraint(
            "default_duration_minutes > 0", name=op.f("ck_specialty_duration_positive")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_specialty")),
        sa.UniqueConstraint("name", name=op.f("uq_specialty_name")),
        sa.UniqueConstraint("slug", name=op.f("uq_specialty_slug")),
    )
    op.create_table(
        "user_account",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("email", sa.String(length=254), nullable=False),
        sa.Column("hashed_password", sa.String(length=255), nullable=False),
        sa.Column("role", sa.Enum("patient", "doctor", "admin", name="user_role"), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("full_name", sa.String(length=120), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_user_account")),
    )
    op.create_index(op.f("ix_user_account_email"), "user_account", ["email"], unique=True)
    op.create_table(
        "doctor",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("clinic_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("first_name", sa.String(length=80), nullable=False),
        sa.Column("last_name", sa.String(length=80), nullable=False),
        sa.Column("license_number", sa.String(length=40), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["clinic_id"],
            ["clinic.id"],
            name=op.f("fk_doctor_clinic_id_clinic"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["user_account.id"],
            name=op.f("fk_doctor_user_id_user_account"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_doctor")),
        sa.UniqueConstraint("license_number", name=op.f("uq_doctor_license_number")),
        sa.UniqueConstraint("user_id", name=op.f("uq_doctor_user_id")),
    )
    op.create_index(op.f("ix_doctor_clinic_id"), "doctor", ["clinic_id"], unique=False)
    op.create_table(
        "patient",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("first_name", sa.String(length=80), nullable=False),
        sa.Column("last_name", sa.String(length=80), nullable=False),
        sa.Column("date_of_birth", sa.Date(), nullable=False),
        sa.Column("phone", sa.String(length=32), nullable=True),
        sa.Column("email", sa.String(length=254), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "date_of_birth <= CURRENT_DATE", name=op.f("ck_patient_dob_not_in_future")
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["user_account.id"],
            name=op.f("fk_patient_user_id_user_account"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_patient")),
        sa.UniqueConstraint("user_id", name=op.f("uq_patient_user_id")),
    )
    op.create_index(op.f("ix_patient_email"), "patient", ["email"], unique=False)
    op.create_table(
        "room",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("clinic_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=60), nullable=False),
        sa.Column("capacity", sa.Integer(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("capacity > 0", name=op.f("ck_room_capacity_positive")),
        sa.ForeignKeyConstraint(
            ["clinic_id"], ["clinic.id"], name=op.f("fk_room_clinic_id_clinic"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_room")),
        sa.UniqueConstraint("clinic_id", "name", name="unique_room_name_per_clinic"),
    )
    op.create_index(op.f("ix_room_clinic_id"), "room", ["clinic_id"], unique=False)
    op.create_table(
        "appointment",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("clinic_id", sa.Integer(), nullable=False),
        sa.Column("doctor_id", sa.Integer(), nullable=False),
        sa.Column("patient_id", sa.Integer(), nullable=False),
        sa.Column("room_id", sa.Integer(), nullable=True),
        sa.Column("specialty_id", sa.Integer(), nullable=False),
        sa.Column("appointment_date", sa.Date(), nullable=False),
        sa.Column("start_time", sa.Time(), nullable=False),
        sa.Column("end_time", sa.Time(), nullable=False),
        sa.Column(
            "duration_minutes",
            sa.Integer(),
            sa.Computed("(EXTRACT(EPOCH FROM (end_time - start_time)) / 60)::int", persisted=True),
            nullable=False,
        ),
        sa.Column(
            "booked_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "status",
            sa.Enum("scheduled", "completed", "cancelled", "no_show", name="appointment_status"),
            nullable=False,
        ),
        sa.Column(
            "urgency", sa.Enum("routine", "urgent", "emergency", name="urgency"), nullable=False
        ),
        sa.Column("is_new_patient", sa.Boolean(), nullable=False),
        sa.Column("notes", sa.String(length=500), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "booked_at <= (appointment_date + start_time) AT TIME ZONE 'UTC'",
            name=op.f("ck_appointment_booked_before_start"),
        ),
        sa.CheckConstraint(
            "start_time >= TIME '06:00:00' AND end_time <= TIME '22:00:00'",
            name=op.f("ck_appointment_within_clinic_day_bounds"),
        ),
        sa.CheckConstraint("duration_minutes > 0", name=op.f("ck_appointment_duration_positive")),
        sa.CheckConstraint("end_time > start_time", name=op.f("ck_appointment_end_after_start")),
        sa.ForeignKeyConstraint(
            ["clinic_id"],
            ["clinic.id"],
            name=op.f("fk_appointment_clinic_id_clinic"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["doctor_id"],
            ["doctor.id"],
            name=op.f("fk_appointment_doctor_id_doctor"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["patient_id"],
            ["patient.id"],
            name=op.f("fk_appointment_patient_id_patient"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["room_id"], ["room.id"], name=op.f("fk_appointment_room_id_room"), ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["specialty_id"],
            ["specialty.id"],
            name=op.f("fk_appointment_specialty_id_specialty"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_appointment")),
    )
    op.create_index(op.f("ix_appointment_clinic_id"), "appointment", ["clinic_id"], unique=False)
    op.create_index(
        "ix_appointment_date_specialty",
        "appointment",
        ["appointment_date", "specialty_id"],
        unique=False,
    )
    op.create_index(op.f("ix_appointment_patient_id"), "appointment", ["patient_id"], unique=False)
    op.create_index(op.f("ix_appointment_room_id"), "appointment", ["room_id"], unique=False)
    op.create_table(
        "availability",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("doctor_id", sa.Integer(), nullable=False),
        sa.Column(
            "weekday",
            sa.Enum(
                "monday",
                "tuesday",
                "wednesday",
                "thursday",
                "friday",
                "saturday",
                "sunday",
                name="weekday",
            ),
            nullable=False,
        ),
        sa.Column("start_time", sa.Time(), nullable=False),
        sa.Column("end_time", sa.Time(), nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("effective_to", sa.Date(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "effective_to IS NULL OR effective_to >= effective_from",
            name=op.f("ck_availability_effective_range_valid"),
        ),
        sa.CheckConstraint("end_time > start_time", name=op.f("ck_availability_end_after_start")),
        sa.ForeignKeyConstraint(
            ["doctor_id"],
            ["doctor.id"],
            name=op.f("fk_availability_doctor_id_doctor"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_availability")),
    )
    op.create_index(
        "ix_availability_doctor_weekday", "availability", ["doctor_id", "weekday"], unique=False
    )
    op.create_table(
        "doctor_specialty",
        sa.Column("doctor_id", sa.Integer(), nullable=False),
        sa.Column("specialty_id", sa.Integer(), nullable=False),
        sa.Column("is_primary", sa.Boolean(), server_default="false", nullable=False),
        sa.ForeignKeyConstraint(
            ["doctor_id"],
            ["doctor.id"],
            name=op.f("fk_doctor_specialty_doctor_id_doctor"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["specialty_id"],
            ["specialty.id"],
            name=op.f("fk_doctor_specialty_specialty_id_specialty"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("doctor_id", "specialty_id", name=op.f("pk_doctor_specialty")),
    )
    op.create_index(
        "uq_doctor_specialty_one_primary",
        "doctor_specialty",
        ["doctor_id"],
        unique=True,
        postgresql_where="is_primary",
    )


def downgrade() -> None:
    op.drop_index(
        "uq_doctor_specialty_one_primary",
        table_name="doctor_specialty",
        postgresql_where="is_primary",
    )
    op.drop_table("doctor_specialty")
    op.drop_index("ix_availability_doctor_weekday", table_name="availability")
    op.drop_table("availability")
    op.drop_index(op.f("ix_appointment_room_id"), table_name="appointment")
    op.drop_index(op.f("ix_appointment_patient_id"), table_name="appointment")
    op.drop_index("ix_appointment_date_specialty", table_name="appointment")
    op.drop_index(op.f("ix_appointment_clinic_id"), table_name="appointment")
    op.drop_table("appointment")
    op.drop_index(op.f("ix_room_clinic_id"), table_name="room")
    op.drop_table("room")
    op.drop_index(op.f("ix_patient_email"), table_name="patient")
    op.drop_table("patient")
    op.drop_index(op.f("ix_doctor_clinic_id"), table_name="doctor")
    op.drop_table("doctor")
    op.drop_index(op.f("ix_user_account_email"), table_name="user_account")
    op.drop_table("user_account")
    op.drop_table("specialty")
    op.drop_table("clinic")
    # Dropping a table does not drop the ENUM types it used; they are
    # independent objects and would block a re-run of this migration.
    op.execute("DROP TYPE IF EXISTS appointment_status")
    op.execute("DROP TYPE IF EXISTS urgency")
    op.execute("DROP TYPE IF EXISTS weekday")
    op.execute("DROP TYPE IF EXISTS user_role")
