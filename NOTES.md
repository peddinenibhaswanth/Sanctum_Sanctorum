# NOTES

**Live URL:** https://sanctum-sanctorum-tz1h.onrender.com
(Free-tier Render service — the first request after a period of inactivity can take 30–50 seconds to wake up.)

The database seeds itself automatically on first startup (`seed_if_empty`), so there's nothing extra needed to use it — the catalog, and members like "Ram" (id 5, Adept), are already there.

## What I finished

* **Books:** ISBN-13 checksum validation, duplicate-ISBN rejection (409), `PATCH /books/{id}`, search by title or author, price filters, sorting, and a pagination-total bug fix.
* **Members:** fixed the `tier_at_least` off-by-one, email normalization (strip + lowercase), duplicate-email rejection (409), `GET /members/{id}/stats`, and (as an optional extra) a `GET /members` endpoint with tier filtering and pagination, plus tests for it.
* **Orders:** validation for empty/duplicate line items, tier + bulk discount calculation, atomic order creation (stock reserved with a conditional `UPDATE`, all-or-nothing), and a fix so cancelling an order restores stock.
* **Loans:** the missing model columns (`due_at`, `returned_at`, `late_fee_cents`), status computed at read time, late-fee calculation, borrowing (with the full tier-limit/overdue/stock checks), returning, and listing a member's loans. This also satisfies the "handle concurrent orders for the last copy safely" optional extra — verified both with a manual two-device test (one book, two members racing to order it, one succeeded and one failed cleanly) and with a 40-trial threaded stress test in development.
* **Reports:** top-selling books by paid-order quantity.
* **Deployment:** fixed `db.py` so `check_same_thread` is only passed for SQLite (it breaks Postgres), added `psycopg` as an optional dependency group so the default SQLite install and test suite remain unchanged, and deployed to Render with a managed Postgres database.
* **Frontend bug fix:** found and fixed a dead "Return" button on the Loans tab — its `data-action="loan-return"` had no matching case in the click-handler switch, so clicking it silently did nothing.

All 207 tests pass locally on the default SQLite setup (`uv run pytest`).

## What I didn't do

* The concurrent-order/last-copy behavior is implemented and I verified it manually and with a one-off threaded script during development, but that stress test was never turned into a permanent, automated test inside `tests/`.
* No dedicated regression test for the `tier_at_least` boundary bug I found and fixed (a tier should count as "at or above" itself); it's covered indirectly by the order/loan tests that exercise tier access, but not directly.

## Architectural decisions and trade-offs

* **Stock changes use a conditional `UPDATE ... WHERE stock >= qty` instead of read-then-write.** I tested both under 8 concurrent requests racing for the last copy of a book: the naive read-check-write approach oversold the item in 22 of 40 trials, while the conditional update oversold it in 0 of 40. This covers both order stock reservation and loan borrowing.

* **Duplicate ISBN/email are caught via the database's unique constraint (`IntegrityError`), not just a pre-check query.** A pre-check alone still has a race window; the constraint is the race-safe source of truth, so the pre-check would be redundant.

* **Stock updates in `create_order`/`cancel_order` touch books in `book_id` order.** This establishes a consistent lock-acquisition order and avoids lock-ordering deadlocks on a real multi-connection database, such as two orders touching the same two books in opposite order.

* **Loan status is computed at read time from timestamps, never stored as a column.** A stored loan status could become stale as time passes without any database write — for example, an active loan can become overdue simply because its due time has passed. Computing the status when loan details are returned keeps it current. The same principle is used when filtering/counting overdue loans in member statistics. The trade-off is that the overdue condition exists once in the `loan_status` Python function and once as a SQLAlchemy condition inside `get_member_stats`; both use the same strict boundary (`due_at < now`) and are covered by tests.

* **Postgres driver as an optional dependency group** (`uv add "psycopg[binary]" --optional deploy`), not a default dependency, so the default SQLite install and test suite remain unchanged while deployment installs the extra with `uv sync --extra deploy`.

* **Single service for frontend + API.** The frontend already calls the API with relative URLs, so serving both from one FastAPI app (it mounts `frontend/` as static files) avoids CORS and a second URL to manage.

* **`GET /members` mirrors `list_books`'s pagination shape** (`items`/`total`/`limit`/`offset`) rather than inventing a new response shape, for consistency with the existing endpoint style.

## Known limitations

* Two truly simultaneous requests on the *same* order (pay + cancel at once) or the *same* loan (two returns at once) aren't guarded — the status check-then-write isn't atomic. The stock-race case is covered by the conditional `UPDATE`; this narrower case isn't required by the tests or spec, so I left it as a known gap.

* The `email` column is capped at `String(320)`; the validation regex doesn't enforce a length limit, so on Postgres an unusually long email would fail at the database layer rather than with a clean 422.

* Pending orders reserve stock indefinitely — no expiry releases it if an order is never paid or cancelled.

* There's no authentication or authorization layer distinguishing staff from members — anyone can call `POST /books` or `PATCH /books/{id}` to add or edit catalog entries, including restricted ones. The `restricted` flag only gates member-facing purchase/loan actions (orders and loans), not catalog management. This matches the spec as written, but wouldn't be acceptable in a real production system.

## AI usage

I used Claude (Anthropic) throughout — to read and explain the starter repo and spec, to discuss design options, and to draft the initial implementation for each function. I reviewed the generated code, tested the behavior myself, and adjusted the implementation where needed.

The implementation went through several rounds of review and validation, particularly around areas where the behavior depended on time, concurrency, or interaction between the frontend and backend:

* **Loan status:** I kept loan status derived from `due_at` and `returned_at` at read time rather than storing it as persistent state. This avoids a status becoming stale simply because time has passed without a database update — for example, an active loan can naturally become overdue without anyone modifying the row.

* **Concurrent stock handling:** the stock reservation logic was validated against a read-check-write approach and then implemented using a conditional database update. In development, the naive approach oversold the last copy in 22 of 40 trials, while the conditional update did not oversell it in those 40 trials.

* **Frontend behavior:** after testing the deployed application myself, I found that clicking "Return" on a loan did nothing. I traced the issue to a missing `case 'loan-return':` in the click-handler switch in `app.js`. The button's `data-action` therefore never reached the existing `returnLoan()` function. I added the missing case and verified the fix.

Overall, AI was used as a development and review aid, while the final implementation was validated against the specification through code review, automated tests, manual testing, and deployment testing.
