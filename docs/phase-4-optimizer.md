# Phase 4 — Optimizer

> **Goal:** a CP-SAT scheduler, a competent FCFS baseline, and a benchmark
> number that is real and reproducible.
>
> **The finding that shaped the phase:** the first honest benchmark returned
> **0% improvement**, and the reason was not the optimizer. Then, once it did
> improve, an out-of-sample simulation showed it was making patient waiting
> *worse*.

---

## Table of contents

1. [What the model decides](#1-what-the-model-decides)
2. [Constraints, and the ones that came for free](#2-constraints-and-the-ones-that-came-for-free)
3. [The objective, and every weight in it](#3-the-objective-and-every-weight-in-it)
4. [Overbooking, and the way around the constraint](#4-overbooking-and-the-way-around-the-constraint)
5. [A bug the tests caught](#5-a-bug-the-tests-caught)
6. [Zero percent, and why](#6-zero-percent-and-why)
7. [The load sweep](#7-the-load-sweep)
8. [The regression the simulator found](#8-the-regression-the-simulator-found)
9. [Two flaws in my own benchmark](#9-two-flaws-in-my-own-benchmark)
10. [Determinism](#10-determinism)
11. [Results](#11-results)

---

## 1. What the model decides

For each appointment: a **start time**, on a 15-minute grid, inside its booked
doctor's working windows.

The doctor is **fixed** — the optimizer re-times appointments, it does not
reassign them. That was a deliberate scope decision, and it has a consequence
worth stating plainly rather than discovering later: with doctors fixed, the
specialty-match rule stops being a decision the solver searches over and becomes
an invariant to *validate*. It is still checked, because bad data can violate
it, but the solver never has to satisfy it.

Time is **minutes from midnight, as integers**. CP-SAT works over integers, and
converting once at the boundary keeps every constraint readable arithmetic
rather than a datetime expression.

The grid is 15 minutes because that is what `Availability`, the Phase 2 slot
API, and the generator all use. An optimizer free to propose 09:07 would produce
schedules the booking path cannot express.

---

## 2. Constraints, and the ones that came for free

| Requirement | How it is modelled |
|---|---|
| No overlapping appointments per doctor | `AddNoOverlap` over that doctor's intervals |
| Within clinic operating hours | interval must fit inside a working window |
| Doctor specialty matches | validated up front (doctors are fixed) |
| Room capacity never exceeded | `AddCumulative` with the room's capacity |
| Urgent before non-urgent *where feasible* | soft — priced via the urgency multiplier |
| Duration fits the slot | interval size is the duration plus slack |
| Mandatory doctor breaks | **free** — see below |

### Concept: why `NoOverlap` and not pairwise disjunctions

The naive encoding is "for every pair, i before j or j before i": O(n²) reified
booleans that give the solver almost nothing to propagate with. `AddNoOverlap`
hands CP-SAT a global constraint with a dedicated scheduling propagator, which
reasons about the whole set at once. On a 67-appointment day that is the
difference between milliseconds and a time limit.

### Breaks are not a constraint

`Availability` stores a doctor's day as *separate windows either side of lunch*
— a Phase 1 decision made for a different reason. Requiring each appointment to
fit inside **one** window means it can never span the gap, so mandatory breaks
are enforced by the same constraint that enforces working hours.

No break modelling exists anywhere in this phase. That is what a good schema
buys you three phases later.

### Concept: `Cumulative` vs `NoOverlap` for rooms

A room with capacity 3 may hold three concurrent appointments. `NoOverlap`
cannot express that; `AddCumulative` treats the room as a renewable resource.
`test_a_room_may_hold_two_when_capacity_allows` exists specifically so a lazy
`NoOverlap` implementation cannot pass the capacity test for the wrong reason.

### Urgency is soft, deliberately

"Urgent patients scheduled before non-urgent **where feasible**." Enforcing that
as a hard constraint would make a day INFEASIBLE whenever an emergency is booked
for the afternoon — common, and not an error. It is priced instead, through the
urgency multiplier on delay.

---

## 3. The objective, and every weight in it

```
minimise  w1·delay + w2·idle + w3·overtime + w4·urgency + w5·overbooking
```

Every term is in **minutes**, which is what makes the weights answerable. Adding
"minutes" to "number of conflicts" produces a number whose units depend on the
weights; because everything is minutes, each weight is a plain question — *how
many patient-minutes of delay is one doctor-minute of idle worth?*

| | value | rationale |
|---|---|---|
| delay | 1.0 | the reference unit |
| idle | 0.8 | a doctor-minute is worth more to the clinic than a patient-minute is to one patient, but there are many patients per doctor; above parity the solver shunts a waiting room to close one gap |
| overtime | 3.0 | not merely more of the same — staff held back, cleaning and reception delayed, costs outside the schedule |
| urgency | multiplier | routine 1× / urgent 3× / emergency 8×, applied to *that patient's* delay: urgency changes how bad a delay is, not how bad the appointment is |
| overbooking | 60.0 | a fixed cost, not per minute — a discrete policy decision needing at least an hour of weighted gain to justify |

### Delay is one-sided, and never negative

Being seen *earlier* than requested is not a benefit the solver can bank. A
signed delay would let it "earn" credit by pulling someone forward, which nobody
asked for. See §5 for what happened when this was only half-implemented.

---

## 4. Overbooking, and the way around the constraint

The textbook use of a no-show model is to double-book slots. Phase 1's
`EXCLUDE` constraint makes two overlapping appointments for one doctor
**unrepresentable** in the `appointment` table, so the optimizer cannot simply do
it.

The way through is architectural, not a workaround:

> The constraint lives on the **transactional record**.
> `analytics.schedule_entry` — the **proposal** — has no such constraint, and
> Phase 1 separated the two deliberately.

So the optimizer may *propose* an overbooked plan. What it may not do is
silently commit two overlapping appointment rows. That is the correct behaviour
rather than a limitation being routed around: the constraint encodes a clinic
policy, and a scheduler should be able to **argue for** changing a policy while
remaining unable to **bypass** it. Applying such a plan is refused with an
explanation, not an `IntegrityError`:

```
this schedule overbooks 1 slot(s). Appointments cannot be written with
overlapping times: the database enforces one appointment per doctor per
interval. The proposal is stored in analytics.schedule for review, but applying
it requires a deliberate change to clinic overbooking policy.
```

Three guards keep a proposal defensible: off by default, capped per day, and
permitted only where **expected attendance across the shared slot stays ≤ 1.0** —
the clinic still expects to see one person. Double-booking two patients who will
probably both attend is not optimisation, it is deciding that someone waits.

### The other use of the no-show model: sequencing

A likely no-show mid-session leaves a gap nobody can fill; the same patient last
in a session just means the doctor finishes early. So expected idle is counted
only when work remains afterwards:

```
expected_idle_i = p_noshow_i × duration_i × (work remains after i)
```

This needs the probabilities to be **calibrated** — multiplying an uncalibrated
score by minutes gives a meaningless number. That is what Phase 3's Brier score
was checking.

---

## 5. A bug the tests caught

`test_no_appointment_is_moved_earlier_than_requested` failed on the first run.

The solver had scheduled a 14:00 request at **13:00**. The mechanism: delay is
one-sided, so pulling someone forward costs nothing, and the idle term rewards
compressing a doctor's span — so moving people earlier was free improvement.

It is not an optimisation. It is a patient arriving to find their slot already
gone. Moving someone earlier requires their consent, which the optimizer cannot
obtain, so it is now a hard constraint:

```python
model.add(start >= appointment.requested_start_minute)
```

Worth noting *how* it surfaced: not from reasoning about the objective, but from
a test asserting a property a patient would care about. The objective was
internally consistent and produced a lower cost. It was just wrong.

---

## 6. Zero percent, and why

The first honest benchmark on real data returned **0.00% improvement on every
term**. Optimizer and baseline produced byte-identical schedules.

The diagnosis took one query:

```
mean utilisation           17.1%   (busiest doctor 42%)
appointments per doctor     4.19
conflicting pairs at requested times   4 of 67
```

**There was no scheduling problem to solve.** At 17% utilisation with four
appointments per doctor per day, almost nothing competes for a slot, so both
schedulers place nearly everything at exactly the time it was requested. An
optimizer cannot improve a schedule that has no conflicts.

This is the sort of result that is tempting to engineer away. The alternative —
reporting a percentage extracted from a day with nothing to optimise — would be
reporting noise and calling it a benchmark.

The useful response was to change the question. Not *"what is the improvement?"*
but *"at what load does this start to matter?"*

---

## 7. The load sweep

Same appointments, shorter working days. Compressing hours raises utilisation
and creates the contention scheduling exists to resolve. It also corresponds to
something real: a half-day, a training afternoon, a clinic short-staffed by
illness.

Two things must be compressed together, which the first attempt got wrong.
Shortening only the windows left appointments requested at 17:00 with nowhere to
go — and since the model forbids moving anyone earlier, all but **one** day
became infeasible. A sweep computed from one surviving day is worthless. The
requested times are now remapped proportionally into the shortened window, which
preserves the *shape* of demand while squeezing it into less room.

The sweep is not an attempt to find a flattering number, and the as-is row is
reported alongside it at every point.

---

## 8. The regression the simulator found

The waiting-room simulator exists as out-of-sample evidence: the optimizer is
not trained on waiting time, so if an optimised schedule also reduced it, that
would be independent confirmation.

It did the opposite.

```
POLICY: no_slack
    util%   objective%   sim wait%
     11.0      +1.21       -6.11
     18.2      +8.09       -7.23
     24.1     +10.69       -5.73
     29.5     +13.02      -11.02
     35.0     +11.21      -21.65
```

The optimizer beats FCFS by up to **+13% on the objective** while making
simulated waiting-room time up to **22% worse**.

The mechanism is the idle term. Minimising a doctor's span pulls appointments
into a tight block; roughly half of consultations run longer than predicted, and
in a tight block every overrun cascades onto everyone behind. The objective
priced the clinic's idle time and did not price the queue that closing it
created.

That is a genuine mis-specification, and it is exactly the kind of thing that an
objective-only benchmark reports as success.

### Two responses, both measured

**Slack.** Reserve `duration × 1.15` so consecutive appointments cannot be
scheduled tight against a consultation that may overrun. The 15% comes from the
duration model's spread (lognormal, σ=0.25). The reserved margin shows up as
idle in the score, which is correct and deliberate: buffer *is* idle time,
bought on purpose.

**The idle weight.** A sweep isolated the cause directly:

| idle weight | objective vs FCFS | simulated wait vs FCFS |
|---|---|---|
| 0.8 | +0.43% | −1.11% |
| 0.3 | +0.55% | −1.11% |
| 0.1 | +0.62% | −1.11% |
| **0.0** | **+0.76%** | **+0.00%** |

The trade is real *and controllable*. So the objective is exposed as a named
policy rather than five tunable floats:

- `balanced` — prioritises clinician utilisation (the default weights)
- `patient_first` — drops the idle pressure, removing the waiting regression at
  the cost of leaving gaps in doctors' days

Which to use is a clinic decision, not a technical one. Both are honest; they
optimise different things, and the benchmark reports both.

---

## 9. Two flaws in my own benchmark

Worth recording because both produced plausible numbers.

**The baseline was running a different policy.** With slack added to the
optimizer only, the optimizer looked dramatically worse on delay (−88% at one
point) — of course it did, it was reserving buffer the baseline was not. That
comparison measured the *policy*, not the algorithm. Both schedulers now take
the same `SlackPolicy`, and every reported comparison holds it fixed.

**Sweep points computed from a handful of days.** Early sweep rows showed
"better on 1/1 days" and "2/3 days" — a mean over one or two surviving instances
presented in the same table as a mean over forty. Points with fewer than five
surviving days are now dropped rather than plotted.

Both flaws made the numbers look *more* interesting, which is the direction
benchmark errors usually go.

---

## 10. Determinism

`num_search_workers=1` and a fixed seed. Multi-threaded CP-SAT is faster and
**non-deterministic**: workers race, and the returned solution depends on
timing. For a number that is meant to be reproducible, a figure that changes
between runs is not a result. Single-threaded solving is genuinely slower, and
that is the right trade here.

The waiting-room simulation uses the same seed for both schedules being
compared, making it a **paired** comparison: both see identical sampled
durations and attendance, so a difference is the schedule rather than the luck
of the draw.

---

## 11. Results

45 weekdays, 2026-06-22 to 2026-08-21. Both schedulers under the same slack
policy at every point. Committed to `backend/reports/benchmark/benchmark.json`.

### As the clinic actually runs

| | value |
|---|---|
| Mean utilisation | **11.0%** |
| Appointments per day | 37.7 |
| Objective reduction | **+1.01%** |
| Days better / identical / worse | 13 / 27 / **0** |

Essentially no improvement, because there is essentially nothing to improve. The
optimizer is never *worse*, which is the most that can be claimed at this load.

### Under load (no_slack policy)

| utilisation | delay | idle | objective | simulated wait | days better |
|---|---|---|---|---|---|
| 11.0% | +0.36% | +0.18% | +1.21% | −6.11% | 14/45 |
| 18.2% | +0.28% | +0.63% | +8.09% | −7.23% | 25/43 |
| 24.1% | +1.12% | +2.02% | **+10.69%** | −5.73% | 27/41 |
| 29.5% | +3.50% | +5.45% | **+13.02%** | −11.02% | 24/30 |
| 35.0% | +0.01% | +12.37% | +11.21% | −21.65% | 12/13 |

**The honest headline: up to 13% objective reduction, but only above ~18%
utilisation, and at a measured cost in patient waiting time.**

### Solver performance

| | value |
|---|---|
| Median solve time | **22 ms** |
| p90 solve time | 33 ms |
| Largest instance | 67 appointments, 16 doctors |
| Status | OPTIMAL on every solved day |

Proving optimality in tens of milliseconds on realistic instances is the
strongest practical argument for CP-SAT here — a metaheuristic would return a
*good* schedule without ever telling you it was the best one.

### Verification

| Check | Result |
|---|---|
| Tests | **170 passed** (26 optimizer invariants) |
| ruff / black / mypy / purity | clean, 77 source files |
| Benchmark reproducibility | single-threaded, seeded; identical across runs |
| Live `/scheduling/optimize` | 67 appointments, OPTIMAL in 52 ms |
| Live `/scheduling/what-if` | mover 0.97 → 0.0 min, clinic mean 0.10 → 0.28 min |
| Overbooked proposal | produced, stored, and refused for apply with an explanation |

That what-if result is the design working: moving one patient earlier helps
*them* and costs everyone else, and the response says so rather than reporting
only the improvement that was asked about.

---

## What Phase 5 builds on this

The agent gets tools whose outputs are already grounded and comparable:

1. `optimize_schedule(date)` — returns a schedule, its baseline, and the
   measured difference
2. `simulate_scenario(...)` — `what_if_moved` already answers a single-patient
   question and reports the effect on everyone else
3. Every number carries provenance: solver status, weights, seed, solve time

One thing to carry forward: the agent must not present the objective improvement
as an unqualified win. The correct grounded answer includes the load it holds at
and the waiting-time cost — which is exactly the kind of caveat an LLM will drop
unless the tool output makes it structurally hard to.
