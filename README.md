# Wolt weekly meal planner

FastAPI/PostgreSQL meal planning for multiple users, with a verified restaurant service catalog, local vector and nutrition retrieval, and durable background jobs. Nutrition and checkout fees remain estimates. The nine initial restaurant URLs are Skopje venues; the catalog supports importing additional venues and cities.

## Current implementation

1. **Restaurant catalog:** stable database IDs, unique source URLs, city and coordinates, verified delivery circles, timezone-aware weekly opening windows, operator availability, and per-item freshness. Empty/failed scrapes preserve the last snapshot. Removed items and estimates pending recalculation are excluded.
2. **Local retrieval:** first restrict to active, available restaurants in the profile city (Skopje in the UI), then search fresh meals with indexed nutrition and vector retrieval. Delivery coverage and opening hours are confirmed on Wolt. The service resolver retains an explicit `require_verified=True` mode for integrations with verified data.
3. **AI planning:** restaurant-balanced hybrid retrieval combines pgvector semantic matches and full-text ranks with affordable, nutrition-relevant and category-spread options, supplying up to 360 choices. The model proposes the plan; code validates requirements and requests targeted repairs without rescoring valid choices. Food classification uses model judgments rather than name dictionaries.
4. **Separate ingestion:** scraping snapshots enqueue item-level nutrition jobs. Nutrition workers publish only if their claimed snapshot is still current; document triggers enqueue embedding work. Embeddings run in batches of 64. Queues use leases, claim tokens, retries and bounded worker concurrency.
5. **Multi-user infrastructure:** opaque database-backed sessions, password hashing, same-origin writes, login and per-user API limits, per-user profiles and plans, durable weekly jobs, shared versioned query-vector caching, and nutrition caching. Saved plans revalidate prices, freshness and delivery coverage before display. Profile changes invalidate ongoing results.
6. **Earlier retrieval evaluation (predates AI planning):** [benchmark runner](benchmarks/scale.py) and [measured results](benchmarks/results-2m-dense.json) cover two million bootstrapped meals, concurrent retrieval, exact-reference recall, budget/fit metrics, and 20 authenticated users submitting and polling weekly jobs. See [evaluation notes](benchmarks/README.md) for what these measurements do and do not establish.

**Verified service data is not yet available.** City-based planning is enabled without coordinates; it does not claim address-level delivery coverage or verified opening hours.

## Run locally

Use `.env` containing `DATABASE_URL` (SQLAlchemy asyncpg URL) and `OPENAI_API_KEY`. The dependency versions are in `requirements.txt`. PostgreSQL must have pgvector installed. The database role needs DDL permissions for initialization.

```sh
.venv/bin/python search_admin.py init
.venv/bin/uvicorn main:app --reload
# Separate terminal/process; necessary for weekly job completion:
.venv/bin/python workers.py plans --concurrency 2
# Keep existing estimated meals current (hourly public-menu checks):
.venv/bin/python workers.py prices --concurrency 1
```

Open `http://127.0.0.1:8000`. The city is Skopje; no coordinates are required. Save nutrition goals, budget and fee allowance. Plans contain three dishes per day. Confirm the delivery address and time on Wolt.

Local mode preserves the previous local profile as user ID 1 and accepts only loopback requests with a local Host. For remote use, set `WOLT_AUTH_REQUIRED=true`, run behind HTTPS, and use the sign-in/create-account form. Accounts start above the legacy profile ID and do not take ownership of it. Each account has its own profile, plan state, and jobs. Deployments behind a reverse proxy must configure trusted proxy handling so scheme and client identity are correct; application workers must not be directly exposed around the proxy.

The API process runs migrations but **does not run ingestion or planner workers**. Run the worker commands under a process supervisor in a deployment. Concurrency limits apply per worker process; total capacity is replicas multiplied by the configured concurrency. No Redis service is needed.

```sh
# These ingestion workers scrape Wolt / make paid provider calls as required:
.venv/bin/python workers.py scrape --concurrency 2
.venv/bin/python workers.py nutrition --concurrency 2
.venv/bin/python workers.py embedding
# For development, run every worker role in one separate process:
.venv/bin/python workers.py all --concurrency 2
```

Scraping checks for due venues every 15 minutes, refreshing at roughly 24-hour intervals. Scrape and nutrition jobs retry up to three times; interrupted claims are recovered after their lease expires. Embedding batches back off and stop after three failures; changing the document resets its retry state. Successful unchanged estimates are reused, including legacy cache entries. Expired sessions, query caches, old request counters and completed catalog jobs are periodically cleaned up by workers. Each loop is independently supervised with restart backoff capped at 30 seconds, so a cleanup or database failure does not stop unrelated roles. SIGTERM cancels workers cleanly; durable leases recover interrupted jobs.

## Catalog and verified coverage

```sh
.venv/bin/python search_admin.py restaurants
.venv/bin/python search_admin.py import-restaurants --file restaurants.json
.venv/bin/python search_admin.py import-service --file verified-service.json
.venv/bin/python search_admin.py populate
.venv/bin/python search_admin.py status
.venv/bin/python search_admin.py retry-embeddings
.venv/bin/python search_admin.py index
```

Restaurant imports contain `name`, `url`, `city`, and optional `is_active`. Existing URLs retain their IDs; changing a city clears verification. Service imports use those IDs and must conform to [restaurant-service.schema.json](restaurant-service.schema.json). Each record includes restaurant coordinates, timezone, availability, provenance (`source`), a timezone-qualified future `verified_until`, delivery circles, and opening windows. This is a data contract, not a supplied dataset of real coverage.

Opening windows use Monday=0 and minute offsets from midnight; split overnight windows across two weekdays. Circle coverage must itself be verified; a guessed radius around a venue is not sufficient. The importer validates the whole file and commits atomically. Multiple circles can represent separate service zones. Polygonal coverage and automatic provider service-area feeds are not currently implemented.

`populate` queues scrapes; it does not complete them synchronously. `index` processes pending embedding batches directly; `retry-embeddings` re-enables exhausted embedding documents. CLI commands are local operator tools, not public catalog-write endpoints.

## Planning and API behavior

- `GET/POST /api/user/profile`: retrieve or save the authenticated user's profile. Saving calculates targets through the provider, preserves an omitted fee allowance, and invalidates previous plans. Explicit `null` clears the allowance; explicit `0` reserves no fees.
- `POST /api/agent/weekly-plan`: enqueue a durable per-user job and return `refreshing`. Repeated submissions while pending deduplicate. Workers retry interruptions and superseded generations. `GET` polls the job and returns the saved result; the browser resumes polling after reload.
- `POST /api/agent/fetch-meals`: legacy daily planning, now using the bounded cached catalog instead of scraping. `GET /api/agent/daily-plan` returns its saved state. `GET /api/agent/meal-queue` exposes only a current `ready` day.
- `POST /api/meals/search`: authenticated diagnostic hybrid search restricted to the user's city. Structured price and nutrition filters are enforced in SQL. Model recommendations can cite only existing retrieved IDs and verbatim menu evidence.

The daily budget includes one user-specified reserve for all delivery/service fees and extras. Actual checkout charges are not scraped. Selected prices and nutrition come from database records; generated IDs never supply them. All days must fit the estimated budget. Nutrition tolerances are ±15% for calories/protein and ±20% for carbs/fat, product rules rather than a clinical evaluation.

The AI chooses both daily and weekly plans from a restaurant-balanced pool of up to 360 catalog items (18 per venue, up to 20 venues). Retrieval fuses pgvector similarity and full-text matches by reciprocal rank, then includes affordable, nutritionally relevant and category-spread options with randomized exploration; it does not preselect winning triples. `MEAL_PLANNER_MODEL` configures the planner (default `gpt-4o-mini`). The model receives recent plans, descriptions, food assessments, prices and macro targets.

Code validates catalog IDs, meal eligibility, three distinct dishes per day, one restaurant per day, seven different restaurants per week and the estimated budget as hard requirements. Nutrition tolerances are matching goals: before asking the AI, a feasibility check identifies venues with no fitting combination in the retrieved pool. AI-selected affordable days from those venues remain visible as `poor_fit`, with exact nutrient gaps. The app does not claim these plans meet the targets.

Valid AI days are preserved. Failed days receive repair options with checked prices and distinct restaurant slots; the AI chooses the replacement meal combination and evaluates its food semantics. Restaurant matching tries to preserve the original choices and excludes already validated days. Repairs cannot override catalog IDs/prices or change valid days. There are at most three selection calls with 40-second timeouts. Once ordinary constraints pass, each plan draft gets one compact independent dish comparison (20-second timeout) so inconsistent canonical labels cannot hide size/drink variants or repeated formats. Repairs omit already known food assessments and return only failed days. There is no silent numerically selected replacement plan. Persistent hard-requirement failures return `no_plan`, provider failures return `refresh_failed`. Weeks expire after seven Europe/Skopje calendar dates.

Food eligibility, canonical dish identity, format, protein sources and preparation are model judgments rather than keyword lists. New nutrition evaluations persist these in `food_assessment`. Legacy retrieved items are evaluated in independent batches without planning targets or budget, then cached with an assessment version. The planner receives those fixed judgments and cannot promote a side/dessert to a meal to complete the week. First-time evaluation adds latency; cached evaluations are reused until the menu evidence or assessment version changes. The additive column migration runs on API startup; restart the API before planner/ingestion workers after upgrading.

The existing image-plus-menu estimator and uncertainty bounds remain in use. The offline model comparison is in [experiments/food_model/RESULTS.md](experiments/food_model/RESULTS.md). Menu estimates and relevance are not independently verified nutrition labels or dietary/allergy guarantees.

## Verification

```sh
# Offline: no configured database or external network calls
.venv/bin/python -m unittest test_food_evaluation test_dish_review test_ai_meal_planner test_meal_planning test_meal_diversity test_meal_eligibility test_plan_history test_meal_search test_infrastructure test_workers -q
node test_frontend.cjs
# PostgreSQL tests use temporary schemas and mock every provider call:
RUN_SEARCH_DB_TESTS=1 .venv/bin/python -m unittest test_food_evaluation test_dish_review test_ai_meal_planner test_meal_planning test_meal_diversity test_meal_eligibility test_plan_history test_meal_search test_infrastructure test_workers -q
# Isolated scale benchmark, no paid API calls; needs cached seed embeddings:
.venv/bin/python benchmarks/scale.py --rows 2000000 --cities 100 --concurrency 20 --requests 100 --quality-samples 20 --http-users 20 --output benchmarks/results-2m-dense.json
```

Tests cover service validation, distance/hours/availability, migration idempotence, cached-plan invalidation, authentication/logout/origin checking, per-user isolation/limits, job recovery/deduplication, outdated nutrition results, embedding backoff, retrieval fallbacks, budget boundaries, weekly fit/variety and frontend restoration. The old `test_openai.py` and `test_scraper.py` scripts make live calls and are not included.

Database migrations are additive; they preserve existing profiles and menus. Keep backups for operational deployments. The benchmark's temporary data is removed after completion; the JSON report remains.

Retrieval implementation reference: [pgvector filtering and exact/approximate search](https://github.com/pgvector/pgvector#filtering).

## Prepare a day's Wolt cart (Chrome / Edge)

Click a day to review its meals in a confirmation dialog. The per-user `GET /api/agent/weekly-plan/{day_number}/order-preview` route validates the saved generation, date, prices, availability and coverage before handing off. Install the local [Wolt Cart Helper](browser-extension/README.md), or open `/static/order-setup.html` for the ZIP and setup instructions. Reload the planner after installation.

The extension prepares one restaurant at a time in your existing Wolt browser session and leaves review/payment to you. It stops on an existing cart, choices, price changes or ambiguous controls. Future plan dates do not schedule delivery. City-based planning does not require verified service data; confirm delivery availability on Wolt.

The English/MKD Wolt adapter is experimental: fixture-tested, not yet verified against a signed-in live checkout. Additional checks: `.venv/bin/python -m unittest test_ordering -q`, `node test_frontend.cjs`, and `node browser-extension/test_helper.cjs`. PostgreSQL integration tests also exercise per-user order previews and stale-plan rejection.

## One restaurant per day

Weekly plans require three distinct meals from the same restaurant each day and seven different restaurants. Valid AI choices are kept. Failed days are repaired using affordable options from distinct unused venues; the model chooses replacement meals. Nutrition gaps are explicitly labeled rather than making an otherwise valid week disappear. Each day is one helper handoff with all three meals.

The `prices` worker refreshes timestamps for unambiguously observed existing menu items even when their prices do not change, and retires missing dishes. It preserves nutrition estimates and does not add or estimate new dishes; use the full ingestion workers for those. Like the other worker commands, it must remain running or be managed by a process supervisor.

Daily diversity checks use AI-assessed identities and formats: duplicate dishes and three dishes of the same known format require repair. There is no food-name/protein dictionary. Recent weeks and assessed dish characteristics are supplied to the model for cross-day and cross-generation variety; reports expose exact reuse counts.

AI response schema reference: [OpenAI Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs).

For an opt-in live AI check against a profile and catalog without publishing a saved plan (food assessments are cached in the catalog):

```sh
.venv/bin/python scripts/smoke_ai_plan.py --user-id 1
```

This makes provider calls and writes the diagnostic plan to `/tmp/wolt-plan-smoke.json`.

To prefill the independent food-assessment cache for existing available items (uses the AI API):

```sh
.venv/bin/python scripts/assess_catalog.py
```
