# Phase 2 — Core Application

> **Goal:** authentication, three-role authorization, CRUD, per-role dashboards
> and a calendar, background jobs, and — the graded item — a booking path that is
> correct under concurrent writes, with a test that reproduces the race *without*
> the fix and passes *with* it.

---

## Table of contents

1. [Where the session lives](#1-where-the-session-lives)
2. [Password hashing, and a broken dependency](#2-password-hashing-and-a-broken-dependency)
3. [Refresh tokens and reuse detection](#3-refresh-tokens-and-reuse-detection)
4. [CSRF](#4-csrf)
5. [Authorization: roles are not enough](#5-authorization-roles-are-not-enough)
6. [Two races, two fixes](#6-two-races-two-fixes)
7. [Writing a concurrency test that means something](#7-writing-a-concurrency-test-that-means-something)
8. [Background jobs: auditing the trigger](#8-background-jobs-auditing-the-trigger)
9. [Server components and the cookie problem](#9-server-components-and-the-cookie-problem)
10. [Design tokens](#10-design-tokens)
11. [A bug the tests could not catch](#11-a-bug-the-tests-could-not-catch)
12. [Results](#12-results)

---

## 1. Where the session lives

### Concept: the two options, and what each exposes you to

| | `localStorage` + `Authorization: Bearer` | httpOnly cookie |
|---|---|---|
| Readable by page JavaScript | **yes** | no |
| Sent automatically by the browser | no | **yes** |
| Main threat | XSS steals the token | CSRF forges requests |
| Works in a server component | no | yes |

Neither is free — you are choosing which attack to defend against explicitly.

The decision here is **httpOnly cookies**, for three reasons:

1. **XSS is the more damaging failure.** A stolen bearer token can be exfiltrated
   and replayed from anywhere, for as long as it lives. CSRF, by contrast, only
   lets an attacker cause requests from the victim's own browser, and it has a
   complete, well-understood defence (see §4).
2. **Server components cannot read `localStorage`.** With a bearer token, every
   authenticated page must be a client component, which throws away the main
   architectural benefit of the App Router.
3. There is nothing for JavaScript to lose. The client never holds the token, so
   there is no code path that could leak it.

The API accepts the cookie **only** — there is deliberately no bearer fallback:

```python
def _read_access_token(request: Request) -> str:
    token = request.cookies.get(settings.access_cookie_name)
```

A fallback would reintroduce exactly the exposure the cookie exists to remove,
and would bypass the CSRF check, which only guards the cookie path.
`test_bearer_header_is_not_accepted` pins it.

### Concept: `SameSite`

`SameSite` tells the browser when to attach a cookie to cross-site requests.

- `lax` on the **access** cookie — sent on top-level navigations, not on
  cross-site POSTs. `strict` would drop the cookie when a user follows a link in
  from outside, logging them out for no security gain.
- `strict` on the **refresh** cookie, plus `Path=/api/v1/auth/refresh` — it is
  only ever needed at one endpoint, so it is not attached to the hundreds of
  other requests the app makes. Fewer requests carrying it means fewer places it
  can leak.

---

## 2. Password hashing, and a broken dependency

### The dependency problem

Phase 0's lockfile pinned `passlib[bcrypt]`, the conventional choice. It does not
work:

```
AttributeError: module 'bcrypt' has no attribute '__about__'
ValueError: password cannot be longer than 72 bytes
```

passlib is effectively unmaintained and reads `bcrypt.__about__.__version__`
during backend detection — an attribute removed in bcrypt 4.1. Against the
bcrypt 5.0 in our lockfile it fails outright.

Two ways forward: pin `bcrypt<4.1` to keep the wrapper alive, or drop the wrapper
and call `bcrypt` directly. The second is better — it *removes* a dependency
rather than freezing one, and stays on the maintained library. passlib's API here
was three functions.

### Concept: bcrypt's 72-byte limit

bcrypt only considers the **first 72 bytes** of a password. Historically
implementations truncated silently, which is the dangerous behaviour: a
100-character passphrase would be authenticated by its first 72 bytes, and two
distinct long passwords could collide. bcrypt 5 raises instead of truncating,
which is safer but means long passwords now fail outright.

The standard fix, used here:

```python
def _prepare(password: str) -> bytes:
    digest = hashlib.sha256(password.encode("utf-8")).digest()
    return base64.b64encode(digest)      # always exactly 44 bytes
```

**Why base64 rather than the raw digest.** A raw SHA-256 digest can contain a NUL
byte, and bcrypt treats NUL as a string terminator — everything after it would be
discarded, dramatically weakening the hash. Base64 output contains no NUL.

`test_data_model`-style unit checks confirm a 200-character password both works
and stays distinct from a 201-character one.

### Concept: work factor

```python
BCRYPT_ROUNDS = 12   # ~250ms per hash
```

Deliberately slow. The whole point of a password hash is to make offline brute
force expensive. 12 rounds is slow enough to matter and fast enough not to become
a login-path denial-of-service.

### Concept: timing as an oracle

```python
if user is None:
    verify_password(password, _DUMMY_HASH)   # equalise timing
    raise AuthError("invalid credentials")
```

Without the dummy verify, an unknown email returns in microseconds while a wrong
password spends ~250ms in bcrypt. That difference is measurable over the network,
so an attacker could enumerate which addresses have accounts without ever
guessing a password. The responses are also byte-identical, which
`test_wrong_password_and_unknown_email_are_indistinguishable` asserts.

---

## 3. Refresh tokens and reuse detection

### Concept: why two tokens

A single long-lived token forces a bad trade: short expiry means users are
constantly logged out, long expiry means a stolen token is useful for weeks.

Splitting the difference:

- **access** — 30 minutes, sent on every request, never stored server-side.
- **refresh** — 7 days, sent only to `/auth/refresh`, tracked in the database.

Only refresh tokens are stored, and only as SHA-256 fingerprints. **Not bcrypt**:
the input is a 256-bit random value, not a guessable human password, so there is
nothing for a slow hash to defend against, and it would put 250ms on every
refresh.

### Concept: rotation and the reuse signal

Every refresh redeems exactly once and issues a successor. If an
already-rotated token is presented, the token was captured — the legitimate
client has moved past it.

**The response is to revoke the entire family**, not just the replayed token:

```
login ──► R1 ──rotate──► R2 ──rotate──► R3        (family F)
             ▲
             └── attacker replays R1  ⇒  revoke R1, R2, R3
```

Revoking only R1 would be worse than useless: an attacker who stole R1 and
rotated it already holds R2, so the victim gets logged out and the attacker does
not. Killing the family ends both sessions and the real user simply signs in
again. `test_reusing_a_rotated_refresh_token_revokes_the_family` asserts that
even the *legitimate* client's current token stops working.

### Concept: type confusion

```python
if payload.get("typ") != expected_type:
    raise TokenError(...)
```

Without this check, a refresh token would be accepted wherever an access token
is — handing out a seven-day session from a credential intended for one endpoint.
Both tokens are signed by the same key, so the signature alone proves nothing
about *what kind* of token it is.

---

## 4. CSRF

### Concept: the attack

The browser attaches cookies to *any* request to your origin, including one
triggered by a form on someone else's site. The attacker cannot read the cookie
or the response, but the request still executes with the victim's session.

This is the cost of choosing cookies in §1, and it must be paid explicitly.

### Two layers

1. **`SameSite`**, already on the cookies. A modern browser will not attach a
   `lax` cookie to a cross-site POST at all. This is the primary defence.
2. **Origin checking**, in `app/core/csrf.py`, for unsafe methods. `SameSite`
   depends on the browser being current and correct; a server-side check does
   not.

### Concept: why not a double-submit token

The classic pattern puts a random value in both a cookie and a header, and
compares them. It requires the token to be **readable by JavaScript** — so it
cannot be httpOnly. And an XSS that can read the token can forge requests anyway,
so it defends against the attack you already stopped and not the one you have.

The `Origin` header cannot be set by page JavaScript at all. It is the stronger
signal, and it needs no token plumbing.

Requests with neither `Origin` nor `Referer` are allowed: curl, the test suite,
and server-to-server calls do not send them, and they are not subject to CSRF
because nothing is auto-attaching a cookie for them.

---

## 5. Authorization: roles are not enough

### Concept: dependencies, not `if` statements

```python
@router.get("/patients", dependencies=[Depends(require_admin)])
```

A check written inside a handler protects that handler only, and the next
endpoint someone adds starts unprotected **by default**. As a dependency, the
requirement is declarative, visible in the route signature, and shows up in the
OpenAPI schema.

### Concept: object-level authorization

The bug that role checks do not catch:

```
Alice and Bob are both `patient`.
GET /patients/{bob_id} as Alice → role check passes. Both are patients.
```

Any logged-in patient could walk the id space and read every other patient. So
patient access is checked against the *object*:

```python
own = await db.scalar(select(Patient.id).where(Patient.user_id == user.id))
if own != patient_id:
    raise HTTPException(404, "Not found")
```

**404, not 403.** A 403 confirms the record exists, which lets a caller probe for
valid ids. `test_patient_cannot_read_another_patients_record` covers it.

### Concept: scope the query, not the results

```python
stmt = await _scope_to_caller(stmt, user, db)
```

Filtering after loading would still have pulled other people's rows into memory,
and one forgotten filter leaks them. Pushing the restriction into the SQL means
the rows are never fetched.

### Concept: revocation must not be lazy

The role is embedded in the access token so most requests need no extra query —
but `get_current_user` re-reads the user from the database anyway. That is
deliberate: an admin demoting someone must take effect on the **next request**,
not whenever their token happens to expire. `test_role_change_takes_effect_immediately`
and `test_deactivated_mid_session_is_locked_out` pin both.

Note the status codes: a disabled account gets **403**, not 401. The credentials
are valid; the account is off. A 401 would prompt the client to try logging in
again, which cannot help.

---

## 6. Two races, two fixes

This is the graded part, and the interesting finding is that **the obvious race
is already solved and the real one is somewhere else**.

### Race 1 — same doctor, same slot

The textbook version: two patients book the last slot simultaneously.

```
T1: SELECT ... no conflict          T2: SELECT ... no conflict
T1: INSERT                          T2: INSERT
T1: COMMIT                          T2: COMMIT        ← doctor double-booked
```

Application-level checking cannot fix this. Between the SELECT and the INSERT,
another transaction can do the same. Being careful does not shrink the window;
the window is inherent to doing the check separately from the write.

But Phase 1's exclusion constraint already makes the corrupt state
**unrepresentable**:

```sql
EXCLUDE USING gist (doctor_id WITH =, tsrange(...) WITH &&)
WHERE (status <> 'cancelled')
```

It is evaluated by the index *during the write*, so there is no window at all —
regardless of isolation level or interleaving. The second INSERT fails.

So what the application owes here is not correctness, it is a decent **response**:

```python
if "excl_appointment_doctor_no_overlap" in str(exc.orig):
    raise SlotTakenError("that slot was just taken; please pick another time")
```

A 409, not a 500. Losing a race for a slot is normal behaviour in a shared
calendar, not a server error.

### Race 2 — room capacity

Here is a rule the database **cannot** express. "At most `capacity` overlapping
appointments in this room" is a property of a *set* of rows:

- A `CHECK` sees one row.
- An `EXCLUDE` compares rows pairwise — it can enforce capacity == 1, not
  capacity == N.

So the naive implementation is a genuine lost update:

```
capacity = 2, currently 1 booked

T1: SELECT count(*) → 1        T2: SELECT count(*) → 1
T1: 1 < 2, proceed             T2: 1 < 2, proceed
T1: INSERT, COMMIT             T2: INSERT, COMMIT     ← 3 in a 2-capacity room
```

**The fix: take a row lock on the room before counting.**

```python
await db.execute(select(Room).where(Room.id == room_id).with_for_update())
# ...then count, then insert
```

`SELECT ... FOR UPDATE` blocks the second transaction until the first commits, so
its count sees the committed insert.

Two things worth understanding about that lock:

- **It is not protecting the room row's data.** Nothing here modifies the room.
  It is being used as a **mutex keyed on the room id** — the row is just a
  convenient thing to lock on.
- **It is per-room, not global.** Bookings into different rooms take different
  locks and never block each other. `test_locks_on_different_rooms_do_not_serialise`
  guards against "fixing" the race with a coarse lock that would serialise the
  entire clinic.

### A third outcome, found by CI in Phase 4

Two concurrent bookings for the same slot do not always end with the exclusion
constraint rejecting one. Sometimes PostgreSQL **deadlocks**:

```
Process 213 waits for ShareLock on transaction 833; blocked by process 214.
Process 214 waits for ShareLock on transaction 832; blocked by process 213.
```

An `EXCLUDE` constraint does not reject the second writer immediately. The row
is inserted *speculatively* and the conflicting transaction is made to WAIT to
see whether the first commits or aborts. When both transactions claim the same
slot at the same instant, each waits on the other and the deadlock detector
aborts one.

Semantically this is identical to losing the race — two people wanted one slot
and one did not get it — so the booking service now maps SQLSTATE `40P01`
(deadlock_detected) and `40001` (serialization_failure) to the same
`SlotTakenError`, and therefore the same 409. Reporting a 500 would be wrong
twice: it is not a bug, and it tells the client to give up on something a retry
resolves.

It surfaced in CI rather than locally, because it needs the two inserts to land
close enough together and local timing happened to avoid it. The regression test
injects the error directly rather than waiting for the race, so the handler is
covered on every run instead of on lucky ones.

### Why not just use SERIALIZABLE?

It would work. `SERIALIZABLE` isolation makes PostgreSQL detect the dependency
cycle and abort one transaction with a 40001 serialization failure.

The cost is that it applies to **every** booking, and every one then needs retry
logic. A row lock scoped to the single resource that actually needs serialising
is more surgical. `advisory_lock_key` is kept in the service for Phase 4, where
the optimizer rewrites a whole day and a row lock cannot express "this entire
day".

---

## 7. Writing a concurrency test that means something

### Concept: the fixture must allow real concurrency

The rest of the suite runs each test in one transaction and rolls back. That is
perfect for constraint tests and **useless** here:

- Two sessions sharing one connection cannot contend for a lock.
- A rollback would hide the very commit whose visibility is under test.

So the race tests use a separate fixture yielding independent, genuinely
committing sessions on separate connections, and truncate afterwards. Without
this, every assertion in the file would be a tautology that passes for the wrong
reason.

### Concept: determinism

A race that reproduces "usually" is not evidence. `asyncio.Barrier` forces both
transactions to complete their capacity check before either inserts:

```python
_after_capacity_check=barrier.wait
```

This is a deliberate test seam in `book_appointment`, alongside
`_use_room_lock=False`. Both are keyword-only, underscored, and unreachable from
the API. The alternative — `sleep()` and hope — produces a test that fails
randomly in CI and teaches everyone to re-run it.

Note one asymmetry: the *locked* test uses **no** barrier. With the lock in
place, the second transaction blocks before reaching the hook, so waiting on a
barrier there would deadlock. It does not need one — the lock guarantees exactly
one winner however the two are scheduled.

### The tests

| Test | Asserts |
|---|---|
| `test_concurrent_booking_same_slot_only_one_wins` | one `booked`, one `slot_taken`, exactly one row survives |
| `test_back_to_back_concurrent_bookings_both_succeed` | adjacent slots are not a conflict (guards against over-locking) |
| **`test_room_capacity_race_WITHOUT_lock_reproduces_overbooking`** | **the bug: both commit, 2 in a capacity-1 room** |
| **`test_room_capacity_race_WITH_lock_holds_the_limit`** | **the fix: one `booked`, one `room_full`, 1 row** |
| `test_locks_on_different_rooms_do_not_serialise` | the lock is per-room, not global |

The third test asserts the bug **reproduces**. That is unusual and deliberate: it
is the "without the fix" half of the requirement, and it is what makes the fourth
test meaningful rather than merely passing.

---

## 8. Background jobs: auditing the trigger

The Celery task recomputes `analytics.doctor_utilization` from scratch and
compares it against what Phase 1's trigger maintained incrementally.

### Concept: why an incremental summary needs an audit

The trigger applies deltas, which is what makes it O(1) per write. It is also its
weakness: incremental state can drift, and **nothing in the normal write path
ever corrects it**. Real causes:

- `TRUNCATE` fires no row-level triggers at all
- a migration that touches `appointment` with triggers disabled
- a restore from a backup taken mid-transaction
- a bug in a future change to the trigger function

`test_trigger.py` proves correctness in CI. This proves it *in production*, on
real data, on a schedule.

### Concept: report, do not silently repair

```python
def reconcile_utilization(self, days: int = 30, dry_run: bool = True)
```

`dry_run=True` by default. Auto-correcting would hide the underlying bug, which
is the thing actually worth knowing about. Repair is available, but opt-in.

The recount uses a **FULL OUTER JOIN**, not a LEFT JOIN, so it also catches
summary rows that exist for a (doctor, date) with no appointments at all — drift
in the other direction, which a LEFT JOIN would silently miss.

---

## 9. Server components and the cookie problem

### Concept: the server has no cookie jar

This is the App Router auth trap. A server component runs in the Node process,
not the browser, so its `fetch` sends no cookies. To call the API as the
logged-in user it must read the incoming request's cookies and forward them:

```typescript
export async function serverFetch<T>(path: string, cookieHeader: string) {
  const res = await fetch(`${BASE_URL}${path}`, {
    headers: { cookie: cookieHeader },
  });
```

Client components use `apiFetch` with `credentials: "include"`, where the browser
handles it. Getting this wrong produces an app that works in development while
you happen to be on client-rendered routes, then 401s everywhere in production.

### Concept: the server is the authority on identity

`currentUser()` calls `/auth/me` rather than decoding the JWT in the frontend.
The cookie is httpOnly so the token is not readable there anyway — but the deeper
reason is that a client that decoded the token would be trusting a value the user
controls, and would miss a mid-session demotion or deactivation, both of which
the backend applies immediately (§5).

The cost is one API call per protected render. That is cheap next to rendering a
UI the API will then refuse to serve.

### Concept: route gating is UX, not security

`requireRole()` redirects the wrong role to their own dashboard. The API enforces
authorization independently and would refuse the data regardless. The redirect
exists so people land somewhere useful instead of on an empty shell.

### What is a client component, and why

Only four things:

| Component | Why it must be client-side |
|---|---|
| `Providers` | TanStack Query holds mutable cache state in context |
| `LoginForm` | form state and submit handler |
| `LogoutButton` | click handler |
| `BookingCalendar` | refetches slots on change, mutates on book |
| `Charts` | Recharts measures the DOM to size itself |

Everything else — the shell, navigation, all three dashboards, the week calendar,
every table — is a server component and ships no JavaScript.

Note `router.refresh()` after login and logout. Server components cache their
rendered output; without it the shell keeps showing the previous session state
until a hard reload.

### Concept: the calendar deliberately is not a time grid

The week view stacks appointments as a dense list per day rather than
absolutely-positioning blocks by time. Positioned blocks look impressive and read
badly: with 15-minute slots across an 11-hour day, most blocks become too thin to
label. The list is what someone scanning their week actually needs.

---

## 10. Design tokens

Tailwind v4 configures the theme in CSS rather than a JS config, so the design
system's palette becomes real utilities:

```css
@theme {
  --color-primary: #0d4a76;        /* → bg-primary, text-primary */
  --color-primary-container: #d1e4ff;
  --radius-card: 8px;              /* ROUND_EIGHT → rounded-card */
  --spacing-md: 16px;
}
```

Components reference named decisions (`bg-container-lowest`, `rounded-card`)
rather than hex values, so a palette change happens in one place.

Three choices worth stating:

- **Light-only, deliberately.** A clinical tool is used under bright ward
  lighting on shared workstations. A half-finished dark mode that inverts
  surfaces but not chart colours reads as broken; one committed theme beats two
  mediocre ones.
- **Tabular figures on every metric.** Proportional digits change width as values
  update, so a refreshing number visibly jitters — which reads as instability in
  a tool whose job is to look dependable.
- **`change_pct: null` renders as `—`, never `0%`.** Null means "no prior period
  to compare against". Showing a fabricated 0% is a quiet lie, and the API is
  careful to distinguish the two.

---

## 11. A bug the tests could not catch

98 tests passed. Then the first live booking returned **500** — and the row was
written anyway.

```
pydantic_core.ValidationError: 2 validation errors for AppointmentOut
  Error extracting attribute: MissingGreenlet: greenlet_spawn has not been
  called; can't call await_only() here. Was IO attempted in an unexpected place?
```

**Root cause.** `AppointmentOut` embeds `doctor`, `patient`, and `specialty`.
Pydantic reads those attributes while serialising the response — *after* the
handler has returned. On an async SQLAlchemy session, a lazy load at that point
tries to do IO outside the awaited context, which asyncpg cannot service.

**Why every existing suite missed it.** The structure of the test coverage,
not an oversight in any one test:

- the race tests call `booking_service` directly — no serialisation
- the auth tests check authorization — they never book successfully
- the constraint tests use the ORM — no response model

Nothing exercised a *successful* `POST /appointments` through the full stack.

**The fix.** Re-read the row with eager loads before serialising:

```python
async def _load_out(db: AsyncSession, appointment_id: int) -> AppointmentOut:
    row = await db.scalar(
        select(Appointment).options(*_APPOINTMENT_LOADS).where(Appointment.id == appointment_id)
    )
    return AppointmentOut.model_validate(row)
```

**The transferable lesson.** Service-layer tests and authorization tests are both
necessary, and neither covers response serialisation — that only happens when a
request goes through the whole stack. `test_booking_api.py` now does, and it
asserts the nested objects are *present*, not merely that the status is 201: the
bug occurred precisely while serialising them, so a status-only assertion would
be weaker than it looks.

This is the third time in this project that a green test suite gave false
confidence for a structural reason (Phase 0's dotenv path, Phase 0's clean
checkout, and now this). The common thread: **ask what a passing test does not
execute.**

---

## 12. Results

| Check | Result |
|---|---|
| Backend tests | **98 passed** (25 auth/RBAC, 12 booking API, 5 concurrency, 31 constraints/trigger, 25 unit) |
| ruff / black / mypy / purity | clean, 50 source files |
| `alembic check` | no drift (7 migrations) |
| Frontend `tsc --noEmit` / eslint / build | clean, all 9 routes dynamic |
| Cookie flags (live) | `HttpOnly; SameSite=lax` (access), `HttpOnly; SameSite=strict; Path=/api/v1/auth/refresh` (refresh) |
| Booking (live) | 201 with nested objects; duplicate → **409**; outside availability → 422; cross-origin POST → **403** |
| Role gating (live) | patient → `/admin` redirects to `/patient`; anonymous → `/login?next=/admin` |
| Reconciliation task | 13,117 cells checked, 0 drift; injected 2 corrupt rows → both detected → repaired → re-check clean |

### The concurrency result

```
test_concurrent_booking_same_slot_only_one_wins ......... PASSED
test_back_to_back_concurrent_bookings_both_succeed ...... PASSED
test_room_capacity_race_WITHOUT_lock_reproduces_over.... PASSED  ← the bug
test_room_capacity_race_WITH_lock_holds_the_limit ....... PASSED  ← the fix
test_locks_on_different_rooms_do_not_serialise .......... PASSED
```

Without the lock, two concurrent bookings both commit into a capacity-1 room.
With `SELECT ... FOR UPDATE`, one commits and the other receives `RoomFullError`
— and bookings into different rooms still run concurrently.

---

## What Phase 3 builds on this

The database has 36,817 appointments with deliberately learnable structure
(Phase 1) and an application that can read and write them safely (Phase 2).
Phase 3 adds the models:

1. Feature engineering — lead time, hour of week, patient history, specialty
2. A no-show classifier, with precision/recall/confusion matrix
3. Demand and duration models, with MAE/RMSE
4. An eval harness committed as a reproducible script, not screenshots
5. An inference service the optimizer and agent can call

One constraint carries over from this phase: a patient's *predicted* no-show
score must not be shown to the patient. It appears nowhere in the patient
dashboard, which shows only their own factual history. A prediction surfaced to
its subject is both a self-fulfilling nudge and a fairness problem.
