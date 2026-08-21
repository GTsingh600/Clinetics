"""Read models for the per-role dashboards.

Reads `analytics.doctor_utilization` — the table the Phase 1 trigger maintains —
rather than aggregating `appointment` on every page load. That is the payoff for
having built the trigger: the admin dashboard becomes a small scan of a summary
table instead of a GROUP BY over an ever-growing fact table.

Utilisation *percentage* is computed here, not stored, because it needs each
doctor's available minutes from `availability`, which changes independently of
any appointment row.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import Integer, and_, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Appointment,
    AppointmentStatus,
    Availability,
    Doctor,
    DoctorUtilization,
    Weekday,
)
from app.schemas.dashboard import (
    DemandPoint,
    DoctorUtilizationRow,
    MetricCard,
    UtilizationPoint,
)


def _pct_change(current: float, previous: float) -> float | None:
    """None rather than 0 when there is no baseline.

    A dashboard that renders "0%" when it means "no prior data" is lying
    quietly, which is worse than an obvious gap.
    """
    if previous == 0:
        return None
    return round((current - previous) / previous * 100, 1)


async def _available_minutes(
    db: AsyncSession, doctor_ids: list[int], start: dt.date, end: dt.date
) -> dict[int, int]:
    """Total scheduled working minutes per doctor over a date range.

    Expands each weekly availability window across the actual dates in range, so
    a doctor who works Mondays only is not credited with a full week.
    """
    if not doctor_ids:
        return {}
    rows = (
        await db.execute(
            select(
                Availability.doctor_id,
                Availability.weekday,
                Availability.start_time,
                Availability.end_time,
                Availability.effective_from,
                Availability.effective_to,
            ).where(
                Availability.doctor_id.in_(doctor_ids),
                Availability.is_active.is_(True),
                Availability.effective_from <= end,
                or_(Availability.effective_to.is_(None), Availability.effective_to >= start),
            )
        )
    ).all()

    totals: dict[int, int] = dict.fromkeys(doctor_ids, 0)
    day = start
    while day <= end:
        weekday = Weekday(day.isoweekday())
        for doctor_id, wd, s, e, eff_from, eff_to in rows:
            if wd is not weekday or eff_from > day or (eff_to is not None and eff_to < day):
                continue
            totals[doctor_id] += (e.hour * 60 + e.minute) - (s.hour * 60 + s.minute)
        day += dt.timedelta(days=1)
    return totals


async def utilization_by_doctor(
    db: AsyncSession, start: dt.date, end: dt.date
) -> list[DoctorUtilizationRow]:
    rows = (
        await db.execute(
            select(
                Doctor.id,
                Doctor.first_name,
                Doctor.last_name,
                func.coalesce(func.sum(DoctorUtilization.booked_minutes), 0).label("booked"),
                func.coalesce(func.sum(DoctorUtilization.scheduled_count), 0).label("scheduled"),
                func.coalesce(func.sum(DoctorUtilization.completed_count), 0).label("completed"),
                func.coalesce(func.sum(DoctorUtilization.cancelled_count), 0).label("cancelled"),
                func.coalesce(func.sum(DoctorUtilization.no_show_count), 0).label("no_show"),
            )
            .select_from(Doctor)
            .outerjoin(
                DoctorUtilization,
                and_(
                    DoctorUtilization.doctor_id == Doctor.id,
                    DoctorUtilization.utilization_date >= start,
                    DoctorUtilization.utilization_date <= end,
                ),
            )
            .where(Doctor.is_active.is_(True))
            .group_by(Doctor.id, Doctor.first_name, Doctor.last_name)
            .order_by(Doctor.last_name)
        )
    ).all()

    available = await _available_minutes(db, [r[0] for r in rows], start, end)
    out: list[DoctorUtilizationRow] = []
    for doctor_id, first, last, booked, scheduled, completed, cancelled, no_show in rows:
        avail = available.get(doctor_id, 0)
        out.append(
            DoctorUtilizationRow(
                doctor_id=doctor_id,
                doctor_name=f"Dr. {first} {last}",
                booked_minutes=int(booked),
                available_minutes=avail,
                utilization_pct=round(booked / avail * 100, 1) if avail else 0.0,
                scheduled_count=int(scheduled),
                completed_count=int(completed),
                cancelled_count=int(cancelled),
                no_show_count=int(no_show),
            )
        )
    return out


async def utilization_trend(
    db: AsyncSession, start: dt.date, end: dt.date, doctor_id: int | None = None
) -> list[UtilizationPoint]:
    conditions = [
        DoctorUtilization.utilization_date >= start,
        DoctorUtilization.utilization_date <= end,
    ]
    if doctor_id is not None:
        conditions.append(DoctorUtilization.doctor_id == doctor_id)

    rows = (
        await db.execute(
            select(
                DoctorUtilization.utilization_date,
                func.sum(DoctorUtilization.booked_minutes),
            )
            .where(*conditions)
            .group_by(DoctorUtilization.utilization_date)
            .order_by(DoctorUtilization.utilization_date)
        )
    ).all()
    if not rows:
        return []

    doctor_ids = (
        [doctor_id]
        if doctor_id is not None
        else list(await db.scalars(select(Doctor.id).where(Doctor.is_active.is_(True))))
    )
    # Availability is fetched once for the whole span and then attributed per
    # day, rather than issuing one query per day inside the loop.
    per_day = await _available_minutes(db, doctor_ids, rows[0][0], rows[-1][0])
    span_days = (rows[-1][0] - rows[0][0]).days + 1
    daily_avg = sum(per_day.values()) / span_days if span_days else 0

    out: list[UtilizationPoint] = []
    for day, booked in rows:
        avail = int(daily_avg)
        out.append(
            UtilizationPoint(
                date=day,
                booked_minutes=int(booked or 0),
                available_minutes=avail,
                utilization_pct=round((booked or 0) / avail * 100, 1) if avail else 0.0,
            )
        )
    return out


async def demand_by_hour(db: AsyncSession, start: dt.date, end: dt.date) -> list[DemandPoint]:
    rows = (
        await db.execute(
            select(
                cast(func.extract("hour", Appointment.start_time), Integer).label("hour"),
                func.count().label("n"),
            )
            .where(
                Appointment.appointment_date >= start,
                Appointment.appointment_date <= end,
                Appointment.status != AppointmentStatus.CANCELLED,
            )
            .group_by("hour")
            .order_by("hour")
        )
    ).all()
    return [DemandPoint(hour=int(h), count=int(n)) for h, n in rows]


async def headline_metrics(db: AsyncSession, start: dt.date, end: dt.date) -> list[MetricCard]:
    """Four cards, each compared against the immediately preceding window."""
    span = (end - start).days + 1
    prev_start = start - dt.timedelta(days=span)
    prev_end = start - dt.timedelta(days=1)

    async def counts(a: dt.date, b: dt.date) -> tuple[int, int, int, int]:
        row = (
            await db.execute(
                select(
                    func.count(),
                    func.count().filter(Appointment.status == AppointmentStatus.NO_SHOW),
                    func.count().filter(Appointment.status == AppointmentStatus.CANCELLED),
                    func.coalesce(func.sum(Appointment.duration_minutes), 0),
                ).where(Appointment.appointment_date >= a, Appointment.appointment_date <= b)
            )
        ).one()
        return int(row[0]), int(row[1]), int(row[2]), int(row[3])

    total, no_shows, cancelled, minutes = await counts(start, end)
    p_total, p_no_shows, p_cancelled, p_minutes = await counts(prev_start, prev_end)

    resolved = max(total - cancelled, 1)
    p_resolved = max(p_total - p_cancelled, 1)
    no_show_rate = round(no_shows / resolved * 100, 1)
    p_no_show_rate = round(p_no_shows / p_resolved * 100, 1)

    return [
        MetricCard(
            label="Appointments",
            value=total,
            change_pct=_pct_change(total, p_total),
            hint=f"{start:%d %b} - {end:%d %b}",
        ),
        MetricCard(
            label="No-show rate",
            value=no_show_rate,
            unit="%",
            change_pct=_pct_change(no_show_rate, p_no_show_rate),
            hint="excludes cancellations",
        ),
        MetricCard(
            label="Cancellations",
            value=cancelled,
            change_pct=_pct_change(cancelled, p_cancelled),
            hint="slots released back to the calendar",
        ),
        MetricCard(
            label="Booked hours",
            value=round(minutes / 60, 1),
            unit="h",
            change_pct=_pct_change(minutes, p_minutes),
        ),
    ]
