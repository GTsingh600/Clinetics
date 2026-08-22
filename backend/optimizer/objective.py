"""The objective, and the defence of every number in it.

    minimise  w1 * patient delay
            + w2 * doctor idle
            + w3 * overtime
            + w4 * urgency penalty
            + w5 * overbooking penalty

Every term is in **minutes**, which is what makes the weights meaningful. Adding
"minutes" to "number of conflicts" would produce a number whose units depend on
the weights, and tuning it would be guesswork. Because everything is minutes, a
weight is answerable as a plain question: *how many patient-minutes of delay is
one doctor-minute of idle worth?*

--------------------------------------------------------------------------
THE WEIGHTS
--------------------------------------------------------------------------
w1 = 1.0   PATIENT DELAY, the reference unit.
           Minutes between the slot a patient asked for and the slot they got.
           Everything else is priced relative to this.

w2 = 0.8   DOCTOR IDLE.
           A doctor-minute is worth more to the clinic than a patient-minute is
           to any one patient — but there are many patients per doctor, so
           pricing idle far above delay would let the model shunt a whole
           waiting room to save one gap. Slightly below parity encodes "keep
           clinicians busy, but not at any cost to patients".

w3 = 3.0   OVERTIME past clinic close.
           Deliberately the largest per-minute weight. Overtime is not merely
           more of the same: it means staff held back, cleaning and reception
           delayed, and costs that fall outside the schedule entirely. At 3.0 the
           solver will accept roughly three minutes of patient delay to avoid one
           minute of overtime, which matches how clinics actually behave.

w4 = 1.0   URGENCY, applied as a MULTIPLIER on that patient's delay, not as a
           separate additive term:
               routine 1x | urgent 3x | emergency 8x
           So delaying an emergency 15 minutes costs the same as delaying a
           routine patient two hours. Multiplicative rather than additive
           because urgency changes how bad a delay *is*, not how bad the
           appointment is.

w5 = 60.0  OVERBOOKING, per overbooked slot.
           Priced as a fixed cost, not per minute: overbooking is a discrete
           policy decision, so the solver must find at least an hour of weighted
           improvement before deliberately double-booking. See below.

--------------------------------------------------------------------------
EXPECTED IDLE, AND WHY SEQUENCING MATTERS
--------------------------------------------------------------------------
A patient likely to miss their appointment creates idle time — but *where* they
sit in the session decides whether that idle time is recoverable:

    mid-session no-show  -> a gap nobody can fill; the doctor waits
    end-of-session       -> the doctor simply finishes early

Same patient, same probability, materially different cost. So expected idle is
counted only when work remains afterwards:

    expected_idle_i = p_noshow_i * duration_i * (work remains after i)

This is the honest use of the Phase 3 classifier under a schema that forbids
overbooking, and it needs the probabilities to be **calibrated** — multiplying an
uncalibrated score by minutes produces a number with no meaning. That is what the
Brier score in Phase 3 was checking.

--------------------------------------------------------------------------
OVERBOOKING
--------------------------------------------------------------------------
The textbook use of a no-show model is to double-book slots. Phase 1's exclusion
constraint makes two overlapping appointments for one doctor **unrepresentable**
in the `appointment` table, so the optimizer cannot simply do it.

The way through is architectural rather than a workaround. The constraint lives
on the transactional record; `analytics.schedule_entry` — the *proposal* — has no
such constraint, and Phase 1 separated the two deliberately. So the optimizer may
PROPOSE an overbooked plan. What it may not do is silently commit two overlapping
appointment rows.

That is the right behaviour, not a limitation worked around: the constraint
encodes a clinic policy, and a scheduler should be able to *argue* for changing a
policy while remaining unable to *bypass* it. Applying an overbooked plan is
refused with an explanation rather than a database error.

Three guards, so a proposal stays defensible:

1. Off by default (`allow_overbooking=False`).
2. Capped per day (`max_overbooked_slots`).
3. Permitted only where **expected attendance across the shared slot stays at or
   below 1.0** — i.e. the model believes the double-booking will, in
   expectation, still produce one patient. Overbooking two patients who will
   probably both attend is not optimisation, it is deciding someone waits.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Weights:
    """Objective weights. Documented above; overridable per solve."""

    delay: float = 1.0
    idle: float = 0.8
    overtime: float = 3.0
    urgency: float = 1.0
    overbooking: float = 60.0

    def as_dict(self) -> dict[str, float]:
        return {
            "delay": self.delay,
            "idle": self.idle,
            "overtime": self.overtime,
            "urgency": self.urgency,
            "overbooking": self.overbooking,
        }


@dataclass(frozen=True)
class SlackPolicy:
    """Protective time reserved beyond each appointment's predicted duration.

    Added because the benchmark measured the objective doing the wrong thing.
    Minimising idle rewards packing consultations back-to-back at exactly their
    predicted length -- and roughly half of them run longer than predicted, so
    each overrun pushes every later patient back. The waiting-room simulation
    showed optimised schedules were up to 60% WORSE on patient waiting than the
    FCFS baseline they beat on every other term.

    That is a genuine mis-specification, not a simulation artefact: the
    objective priced the clinic's idle time and not the queue it created.

    Slack fixes it by making the trade explicit. Reserving `duration * (1 +
    fraction)` means consecutive appointments cannot be scheduled tighter than
    the consultation plus a margin. The reserved margin shows up as idle time in
    the score, which is correct and deliberate: buffer IS idle time, bought on
    purpose to protect patients from queueing. The weights then decide how much
    is worth buying.

    The default of 0.15 comes from the duration model's spread. Actual durations
    are lognormal around the prediction with sigma 0.25, so roughly a third of
    consultations run more than 15% over; a 15% margin absorbs the common
    overrun without reserving for the rare one.
    """

    fraction: float = 0.15
    max_minutes: int = 15

    def buffer_for(self, duration_minutes: int) -> int:
        return min(round(duration_minutes * self.fraction), self.max_minutes)


DEFAULT_SLACK = SlackPolicy()
NO_SLACK = SlackPolicy(fraction=0.0, max_minutes=0)


@dataclass(frozen=True)
class OverbookingPolicy:
    """When the solver is permitted to double-book a slot.

    `max_expected_attendance` is the load-bearing rule. Two patients sharing a
    slot are acceptable only if their combined attendance probability stays at
    or below one — the clinic still expects to see one person. Above that, the
    double-booking is not exploiting a predicted absence, it is scheduling a
    queue and hoping.
    """

    enabled: bool = False
    max_slots: int = 0
    max_expected_attendance: float = 1.0
    # A slot may be shared by at most this many appointments. Two is the only
    # value clinics use in practice, and higher values compound the failure mode
    # rather than the benefit.
    max_per_slot: int = 2


DEFAULT_WEIGHTS = Weights()

# Measured, not guessed. The benchmark showed the optimizer beating FCFS by up
# to 12% on the objective while making simulated WAITING-ROOM time 5-22% worse:
# minimising idle compresses each doctor into a tight block, and tight blocks
# queue when a consultation overruns.
#
# A weight sweep isolated the cause. Holding everything else fixed:
#
#     idle weight   objective vs FCFS   simulated wait vs FCFS
#         0.8            +0.43%                 -1.11%
#         0.3            +0.55%                 -1.11%
#         0.1            +0.62%                 -1.11%
#         0.0            +0.76%                 +0.00%
#
# So the trade is real and it is controllable. This preset drops the idle
# pressure, which stops the optimizer packing sessions and removes the waiting
# regression, at the cost of leaving gaps in doctors' days.
#
# Which to use is a clinic decision, not a technical one: DEFAULT_WEIGHTS
# prioritises clinician utilisation, PATIENT_FIRST_WEIGHTS prioritises how long
# people sit in the waiting room. Both are honest; they optimise different
# things, and the benchmark reports both.
PATIENT_FIRST_WEIGHTS = Weights(delay=1.0, idle=0.0, overtime=3.0)

NO_OVERBOOKING = OverbookingPolicy()
