"""Create the derived analytical tables

Revision ID: 0003_analytics
Revises: 0002_core_tables
Create Date: 2026-08-21

Forecast, Schedule, ScheduleEntry, and DoctorUtilization.

All four are derived: recomputable from the transactional core plus a trained
model or a solver run. They live in `analytics` rather than `public` so the
distinction between source-of-truth and generated output is enforced by the
database, not merely by naming.

Foreign keys still cross into `public`, so a forecast for a deleted specialty
cannot survive.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0003_analytics"
down_revision: str | None = "0002_core_tables"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "forecast",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("clinic_id", sa.Integer(), nullable=False),
        sa.Column("specialty_id", sa.Integer(), nullable=False),
        sa.Column("forecast_date", sa.Date(), nullable=False),
        sa.Column("hour_of_day", sa.SmallInteger(), nullable=False),
        sa.Column("predicted_demand", sa.Numeric(precision=8, scale=3), nullable=False),
        sa.Column("lower_bound", sa.Numeric(precision=8, scale=3), nullable=True),
        sa.Column("upper_bound", sa.Numeric(precision=8, scale=3), nullable=True),
        sa.Column("model_version", sa.String(length=60), nullable=False),
        sa.Column(
            "generated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("hour_of_day BETWEEN 0 AND 23", name=op.f("ck_forecast_hour_in_range")),
        sa.CheckConstraint("predicted_demand >= 0", name=op.f("ck_forecast_demand_non_negative")),
        sa.ForeignKeyConstraint(
            ["clinic_id"],
            ["clinic.id"],
            name=op.f("fk_forecast_clinic_id_clinic"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["specialty_id"],
            ["specialty.id"],
            name=op.f("fk_forecast_specialty_id_specialty"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_forecast")),
        sa.UniqueConstraint(
            "clinic_id",
            "specialty_id",
            "forecast_date",
            "hour_of_day",
            "model_version",
            name="unique_forecast_cell",
        ),
        schema="analytics",
    )
    op.create_index(
        "ix_forecast_lookup",
        "forecast",
        ["clinic_id", "specialty_id", "forecast_date"],
        unique=False,
        schema="analytics",
    )
    op.create_table(
        "schedule",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("clinic_id", sa.Integer(), nullable=False),
        sa.Column("schedule_date", sa.Date(), nullable=False),
        sa.Column("solver_status", sa.String(length=32), nullable=False),
        sa.Column("is_baseline", sa.Boolean(), nullable=False),
        sa.Column("objective_value", sa.Numeric(precision=12, scale=3), nullable=True),
        sa.Column("total_wait_minutes", sa.Integer(), nullable=False),
        sa.Column("total_idle_minutes", sa.Integer(), nullable=False),
        sa.Column("total_overtime_minutes", sa.Integer(), nullable=False),
        sa.Column("urgency_penalty", sa.Numeric(precision=12, scale=3), nullable=False),
        sa.Column(
            "weights", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False
        ),
        sa.Column("solve_time_ms", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("solve_time_ms >= 0", name=op.f("ck_schedule_solve_time_non_negative")),
        sa.CheckConstraint(
            "total_wait_minutes >= 0 AND total_idle_minutes >= 0 AND total_overtime_minutes >= 0",
            name=op.f("ck_schedule_objective_terms_non_negative"),
        ),
        sa.ForeignKeyConstraint(
            ["clinic_id"],
            ["clinic.id"],
            name=op.f("fk_schedule_clinic_id_clinic"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_schedule")),
        schema="analytics",
    )
    op.create_index(
        "ix_schedule_clinic_date",
        "schedule",
        ["clinic_id", "schedule_date"],
        unique=False,
        schema="analytics",
    )
    op.create_table(
        "doctor_utilization",
        sa.Column("doctor_id", sa.Integer(), nullable=False),
        sa.Column("utilization_date", sa.Date(), nullable=False),
        sa.Column("scheduled_count", sa.Integer(), nullable=False),
        sa.Column("completed_count", sa.Integer(), nullable=False),
        sa.Column("cancelled_count", sa.Integer(), nullable=False),
        sa.Column("no_show_count", sa.Integer(), nullable=False),
        sa.Column("booked_minutes", sa.Integer(), nullable=False),
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
            "booked_minutes >= 0", name=op.f("ck_doctor_utilization_booked_minutes_non_negative")
        ),
        sa.CheckConstraint(
            "scheduled_count >= 0 AND completed_count >= 0 AND cancelled_count >= 0 AND no_show_count >= 0",
            name=op.f("ck_doctor_utilization_counts_non_negative"),
        ),
        sa.ForeignKeyConstraint(
            ["doctor_id"],
            ["doctor.id"],
            name=op.f("fk_doctor_utilization_doctor_id_doctor"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "doctor_id", "utilization_date", name=op.f("pk_doctor_utilization")
        ),
        schema="analytics",
    )
    op.create_table(
        "schedule_entry",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("schedule_id", sa.Integer(), nullable=False),
        sa.Column("appointment_id", sa.Integer(), nullable=False),
        sa.Column("doctor_id", sa.Integer(), nullable=False),
        sa.Column("room_id", sa.Integer(), nullable=True),
        sa.Column("assigned_start", sa.Time(), nullable=False),
        sa.Column("assigned_end", sa.Time(), nullable=False),
        sa.Column("wait_minutes", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "assigned_end > assigned_start", name=op.f("ck_schedule_entry_end_after_start")
        ),
        sa.CheckConstraint("wait_minutes >= 0", name=op.f("ck_schedule_entry_wait_non_negative")),
        sa.ForeignKeyConstraint(
            ["appointment_id"],
            ["appointment.id"],
            name=op.f("fk_schedule_entry_appointment_id_appointment"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["doctor_id"],
            ["doctor.id"],
            name=op.f("fk_schedule_entry_doctor_id_doctor"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["room_id"],
            ["room.id"],
            name=op.f("fk_schedule_entry_room_id_room"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["schedule_id"],
            ["analytics.schedule.id"],
            name=op.f("fk_schedule_entry_schedule_id_schedule"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_schedule_entry")),
        sa.UniqueConstraint(
            "schedule_id", "appointment_id", name="unique_appointment_per_schedule"
        ),
        schema="analytics",
    )
    op.create_index(
        op.f("ix_schedule_entry_schedule_id"),
        "schedule_entry",
        ["schedule_id"],
        unique=False,
        schema="analytics",
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_schedule_entry_schedule_id"), table_name="schedule_entry", schema="analytics"
    )
    op.drop_table("schedule_entry", schema="analytics")
    op.drop_table("doctor_utilization", schema="analytics")
    op.drop_index("ix_schedule_clinic_date", table_name="schedule", schema="analytics")
    op.drop_table("schedule", schema="analytics")
    op.drop_index("ix_forecast_lookup", table_name="forecast", schema="analytics")
    op.drop_table("forecast", schema="analytics")
