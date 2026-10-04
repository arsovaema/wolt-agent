# Wolt weekly meal planner

A multi-user weekly meal-planning application built with FastAPI, PostgreSQL, pgvector, OpenAI models, and a Chrome/Edge cart-helper extension.

The application retrieves fresh and affordable meals from a local Wolt restaurant catalog, asks an AI model to construct a varied seven-day plan, validates the result with deterministic Python constraints, and helps the user prepare one day’s meals in their Wolt cart.

Nutrition values and checkout fees are estimates. The user must confirm restaurant availability, delivery coverage, final prices, fees, delivery time, and payment directly on Wolt.

## Main features

- Seven-day meal plans with three dishes per day.
- All three dishes for a day come from the same restaurant.
- Each day uses a different restaurant.
- Hybrid meal retrieval using pgvector, PostgreSQL full-text search, nutrition, affordability, category diversity, and randomized exploration.
- AI-generated plans followed by deterministic validation and targeted repair.
- Per-user accounts, profiles, budgets, plans, and planning jobs.
- Background workers for scraping, nutrition estimation, embeddings, planning, and price refreshes.
- Menu freshness, price, and availability checks.
- Chrome/Edge extension that prepares meals in the Wolt cart without placing or paying for an order.
- PostgreSQL-backed queues, sessions, caches, and job recovery.

## How meal planning works

The final plan is produced in two main stages.

### 1. Candidate retrieval

The backend first selects restaurants that are:

- Active.
- Marked as available.
- Located in the user’s configured city.
- Eligible for the current planning mode.

The normal planning mode currently uses city-level restaurant eligibility. It does not claim to verify the user’s exact delivery address or future opening hours.

For each eligible restaurant, the system retrieves fresh menu items that:

- Are currently marked as available.
- Were observed within the previous 72 hours.
- Have a positive price.
- Fit within the user’s food allowance.
- Have valid nutrition estimates.
- Are not classified as known non-meals.

The retrieval process combines several independent branches:

- pgvector semantic similarity.
- PostgreSQL full-text matching.
- Nutrition proximity.
- Affordable options.
- Category distribution.
- Randomized exploration.
- A penalty for meals used in recent plans.

Semantic and lexical results are combined using reciprocal-rank fusion. The additional branches ensure that affordable and nutritionally useful meals remain available even when they are not strong semantic matches.

The pool contains up to 18 meals from each of up to 20 restaurants, for a maximum of 360 candidates.

If embedding generation is temporarily unavailable, retrieval can continue using the text, nutrition, affordability, and exploration branches.

Retrieval does not choose the winning daily combinations. It produces a bounded, restaurant-balanced candidate pool for the planner.

### 2. AI selection and deterministic validation

The AI receives:

- The retrieved meal candidates.
- Prices and nutrition estimates.
- Existing food assessments.
- The user’s nutrition targets.
- The daily budget and fee allowance.
- Recent meal-plan history.
- Instructions about restaurant and meal variety.

The AI proposes the daily meal combinations. Python then validates the proposal.

Hard requirements include:

- Every selected ID must exist in the retrieved catalog.
- Every selected item must be an eligible meal.
- Every day must contain exactly three distinct dishes.
- All three dishes for a day must come from one restaurant.
- Seven different restaurants must be used across the week.
- The estimated daily total must fit the configured budget.
- Duplicate variants of the same dish are rejected.
- Three dishes with the same known format are rejected.

Nutrition tolerances are planning goals rather than unconditional hard requirements:

- Calories and protein: ±15%.
- Carbohydrates and fat: ±20%.

Before planning, the backend checks which restaurants have at least one retrieved three-item combination that fits these tolerances. If a restaurant has no fitting combination, an affordable AI-selected day from that restaurant may remain visible as `poor_fit`, with the nutrient differences shown explicitly.

The application does not claim that `poor_fit` days satisfy the configured nutrition targets.

### Plan repairs

Valid days are preserved whenever possible.

When a day fails validation, the backend:

1. Enumerates affordable, valid three-item combinations.
2. Groups those combinations by restaurant.
3. Uses restaurant matching to reserve different restaurants for failed days.
4. Gives the AI prevalidated repair-option IDs.
5. Allows the AI to choose among those options.

The AI cannot invent a new price, restaurant, or meal ID during repair. Python maps the chosen option back to the original database records.

The planner makes at most three selection attempts, with a 40-second timeout for each selection call.

An additional AI dish comparison examines dish identity and format information after ordinary validation. Its assessments are used in subsequent validation and repair work. Cached food assessments are reused when the underlying menu evidence and assessment version have not changed.

The application does not silently replace an invalid plan with a numerically selected fallback week. Persistent constraint failures return `no_plan`; provider or connection failures return `refresh_failed`.

## Food and nutrition assessment

Nutrition workers estimate:

- Calories.
- Protein.
- Carbohydrates.
- Fat.
- Estimate ranges and confidence.
- Whether an item is a complete meal.
- Canonical dish identity.
- Dish format.
- Protein sources.
- Preparation style.

Food classification is model-based rather than a hard-coded food-name dictionary.

These values are estimates derived from menu evidence and, when available, item images. They are not verified nutrition labels and must not be treated as medical, dietary, or allergy guarantees.

Allergy-critical decisions should be confirmed with the restaurant and Wolt before ordering.

## Restaurant catalog

The included [restaurants.json](restaurants.json) file currently contains eleven Skopje restaurants. The catalog supports importing more restaurants and cities.

Restaurant records have stable database IDs and unique source URLs. Failed or empty scrapes preserve the previous successful snapshot rather than deleting the existing menu.

Removed items and items awaiting required recalculation are excluded from planning.

### Service-area support

The project contains a schema and resolver for verified service data, including:

- Restaurant coordinates.
- Delivery circles.
- Time zones.
- Weekly opening windows.
- Operator availability.
- Verification provenance.
- Verification expiration dates.

However, a real verified service-area dataset is not supplied with the project. Normal local planning therefore uses city-level eligibility and returns `city_only_unverified`.

City-level eligibility does not prove that a restaurant:

- Delivers to the user’s exact address.
- Is open at the requested time.
- Will remain available on a future plan date.

The user must confirm these details on Wolt.

The optional verified mode is available through `require_verified=True` for integrations that provide trusted service data.

## Architecture

The main components are:

- `main.py` – FastAPI application, routes, profiles, plan state, and startup migrations.
- `auth.py` – accounts, password hashing, sessions, request-origin checks, and user identity.
- `catalog.py` – restaurant catalog and scraping-job management.
- `ingestion.py` – nutrition-estimation jobs and result publication.
- `meal_search.py` – search documents, embeddings, hybrid search, and indexing.
- `weekly_retrieval.py` – restaurant-balanced candidate retrieval for AI planning.
- `ai_meal_planner.py` – AI plan generation, validation, and repair coordination.
- `meal_planning.py` – nutrition calculations, scoring helpers, and plan summaries.
- `meal_eligibility.py` – food-assessment models and eligibility logic.
- `dish_review.py` – independent dish-identity and format comparison.
- `meal_diversity.py` – diversity reporting and reuse analysis.
- `plan_history.py` – recent-plan history supplied to the planner.
- `plan_jobs.py` – durable weekly planning queue and worker.
- `service_area.py` – city-level and optional verified service resolution.
- `ordering.py` – read-only order preview and live-menu preflight.
- `wolt_scraper.py` – public Wolt menu scraping.
- `workers.py` – worker-process entry point.
- `browser-extension/` – Chrome/Edge Wolt cart helper.
- `frontend/` – planner interface and extension setup page.

## Authentication and users

The application supports multiple users with:

- Scrypt password hashing.
- Opaque database-backed sessions.
- Login and account-creation rate limits.
- Same-origin protection for state-changing requests.
- Per-user profiles.
- Per-user plans and background jobs.
- Per-user plan history and ordering previews.

Local development mode preserves the legacy profile as user ID `1` and accepts local loopback requests.

For remote deployment, set:

```env
WOLT_AUTH_REQUIRED=true
```

Remote deployments should use HTTPS and a correctly configured trusted reverse proxy. Worker processes should not be exposed directly to the internet.

## Background workers

The API process runs database migrations, but it does not execute ingestion or planning work itself.

Worker roles include:

- `scrape` – refresh restaurant menus.
- `nutrition` – classify meals and estimate nutrition.
- `embedding` – create and refresh search embeddings.
- `plans` – process weekly planning jobs.
- `prices` – periodically recheck existing menu prices and availability.
- `all` – run scraping, nutrition, embedding, planning, and cleanup roles together.

The `all` role does not include the hourly `prices` worker, so price refreshes should run separately.

Queues use leases, claim tokens, retries, and bounded concurrency. Interrupted work can be recovered after its lease expires.

## Local setup

### Requirements

- Python environment with the packages from `requirements.txt`.
- PostgreSQL.
- The pgvector PostgreSQL extension.
- A database role with permission to create or migrate the required tables.
- An OpenAI API key.

Create a `.env` file containing at least:

```env
DATABASE_URL=postgresql+asyncpg://USER:PASSWORD@HOST/DATABASE
OPENAI_API_KEY=YOUR_OPENAI_API_KEY
```

### Initialize a fresh database

```sh
.venv/bin/python search_admin.py init
.venv/bin/python search_admin.py import-restaurants --file restaurants.json
.venv/bin/python search_admin.py populate
```

`populate` queues restaurant refresh jobs. It does not wait for scraping, nutrition estimation, and embedding generation to finish.

### Start the application

Terminal 1:

```sh
.venv/bin/uvicorn main:app --reload --host 127.0.0.1 --port 8000
```

Terminal 2:

```sh
.venv/bin/python workers.py all --concurrency 2
```

Terminal 3:

```sh
.venv/bin/python workers.py prices --concurrency 1
```

Open:

```text
http://127.0.0.1:8000
```

On a fresh installation, allow the ingestion workers to populate the catalog before generating a plan. Scraping and AI-based estimation can take time and may make paid provider calls.

For production, run the API and worker commands under a process supervisor.

## Catalog administration

```sh
# Show imported restaurants
.venv/bin/python search_admin.py restaurants

# Import or update restaurant records
.venv/bin/python search_admin.py import-restaurants --file restaurants.json

# Queue all restaurants for refresh
.venv/bin/python search_admin.py populate

# Show indexing status
.venv/bin/python search_admin.py status

# Process currently pending embeddings
.venv/bin/python search_admin.py index

# Retry embedding documents that exhausted their attempts
.venv/bin/python search_admin.py retry-embeddings

# Import trusted service-area information
.venv/bin/python search_admin.py import-service --file verified-service.json
```

Restaurant imports contain:

- `name`
- `url`
- `city`
- Optional `is_active`

Existing restaurant URLs keep their database IDs. Changing a restaurant’s city clears its previous service verification.

Service imports must conform to [restaurant-service.schema.json](restaurant-service.schema.json). The schema is a data contract, not a supplied dataset of verified Wolt delivery coverage.

## API behavior

Important routes include:

- `GET /api/user/profile` – retrieve the current user’s profile.
- `POST /api/user/profile` – calculate and save targets, budget, and fee allowance.
- `POST /api/agent/weekly-plan` – enqueue a weekly planning job.
- `GET /api/agent/weekly-plan` – poll or retrieve the current weekly result.
- `POST /api/agent/fetch-meals` – legacy daily planning using the local catalog.
- `GET /api/agent/daily-plan` – retrieve the saved daily-plan state.
- `GET /api/agent/meal-queue` – retrieve a current ready daily queue.
- `POST /api/meals/search` – authenticated diagnostic hybrid meal search.
- `GET /api/agent/weekly-plan/{day_number}/order-preview` – revalidate and prepare one day for the extension handoff.

Repeated weekly-plan submissions are deduplicated while a job is queued or running. Profile changes invalidate an older result or an in-progress generation.

Saved plans are revalidated against:

- Current database prices.
- Menu-item availability.
- Item freshness.
- Active city-eligible restaurants.

Order preview additionally performs a read-only live-menu check for the exact meal names and prices before sending the handoff to the extension.

Exact delivery-address coverage, future opening hours, checkout fees, and delivery times remain Wolt-side checks.

## Budget behavior

The daily budget consists of:

- The food subtotal.
- A user-configured allowance reserved for delivery fees, service fees, and extras.

The application does not scrape or predict the final Wolt checkout fee. Selected catalog meals must fit within the remaining food allowance after the reserve is removed.

An explicitly configured fee allowance of `0` means that no money is reserved for fees. An omitted value preserves the previous allowance.

## Wolt cart helper

The [browser extension](browser-extension/README.md) prepares one planned day in the user’s existing Wolt browser session.

Before handing a day to the extension, the backend verifies:

- The plan generation is still current.
- The planned date and day still exist.
- The three saved database IDs are still valid.
- The saved names and prices still match the database.
- The meals still appear on the public Wolt menu at the expected prices.

The extension then:

1. Opens the correct Wolt restaurant.
2. Confirms that the restaurant page is visible.
3. Requires an empty cart.
4. Finds each meal using its exact visible name and expected MKD price.
5. Opens the item dialog.
6. Verifies the Add control and price.
7. Clicks Add once.
8. Confirms the cart count and total before continuing.
9. Opens the cart or checkout-review page after all meals are confirmed.

The helper never clicks Pay or Place order.

If an item requires options or extras, the helper pauses so the user can make the choices and click Wolt’s Add button. It continues only after it confirms the resulting cart update.

The helper stops and requests attention when it encounters conditions such as:

- A non-empty existing cart.
- An unavailable or ambiguous meal.
- A changed price.
- An ambiguous Add control.
- An unconfirmed cart update.
- A login, delivery-address, or restaurant-availability interruption.

The extension is experimental and depends on Wolt’s visible page structure, which can change.

Its planner-page bridge is currently configured for:

```text
http://127.0.0.1/*
http://localhost/*
```

If the planner is hosted on another origin, that exact HTTPS origin must be added to the extension’s content-script configuration before rebuilding and reloading the extension.

## Testing

Run the offline test suite:

```sh
.venv/bin/python -m unittest \
  test_food_evaluation \
  test_dish_review \
  test_ai_meal_planner \
  test_meal_planning \
  test_meal_diversity \
  test_meal_eligibility \
  test_plan_history \
  test_meal_search \
  test_infrastructure \
  test_workers \
  -q

.venv/bin/python -m unittest test_ordering -q
node test_frontend.cjs
node browser-extension/test_helper.cjs
```

Run PostgreSQL integration tests:

```sh
RUN_SEARCH_DB_TESTS=1 .venv/bin/python -m unittest \
  test_food_evaluation \
  test_dish_review \
  test_ai_meal_planner \
  test_meal_planning \
  test_meal_diversity \
  test_meal_eligibility \
  test_plan_history \
  test_meal_search \
  test_infrastructure \
  test_workers \
  -q
```

The older `test_openai.py` and `test_scraper.py` scripts can make live external calls and are not part of the offline suite.

## Scale benchmark

The repository contains an isolated retrieval benchmark:

```sh
.venv/bin/python benchmarks/scale.py \
  --rows 2000000 \
  --cities 100 \
  --concurrency 20 \
  --requests 100 \
  --quality-samples 20 \
  --http-users 20 \
  --output benchmarks/results-2m-dense.json
```

The existing [benchmark results](benchmarks/results-2m-dense.json) cover two million bootstrapped meals, concurrent retrieval, reference recall, budget and fit metrics, and authenticated planning-job submission and polling.

These measurements predate the current AI plan-selection workflow. See [benchmarks/README.md](benchmarks/README.md) for the benchmark’s scope and limitations.

## Diagnostic AI plan

To run an opt-in AI planning check without publishing a saved plan:

```sh
.venv/bin/python scripts/smoke_ai_plan.py --user-id 1
```

This command makes provider calls, may cache food assessments, and writes its diagnostic result to:

```text
/tmp/wolt-plan-smoke.json
```

To prefill food assessments for existing catalog items:

```sh
.venv/bin/python scripts/assess_catalog.py
```

## Current limitations

- Nutrition information is estimated rather than restaurant-verified.
- The system does not provide medical or allergy guarantees.
- Normal planning uses city-level eligibility rather than verified address-level delivery coverage.
- Final delivery fees and service fees are not scraped.
- Future delivery is not scheduled automatically.
- Restaurant pages and prices can change between planning and checkout.
- The browser extension depends on Wolt’s current visible HTML and accessibility structure.
- Payment always remains a manual user action.
- Full-text retrieval may have limited value when a planning query contains only broad nutrition goals, but it remains a fallback and an exact-evidence branch for future preference or ingredient queries.

## Safety and operational notes

- Keep database backups for deployed environments.
- Database migrations are additive and preserve existing profiles and menus.
- Do not expose worker processes directly to the internet.
- Confirm the final restaurant, dishes, quantities, prices, address, delivery time, fees, ingredients, and payment details on Wolt.
- Respect Wolt’s terms, robots policies, and applicable local rules when operating menu-ingestion workers.

## References

- [pgvector filtering and exact/approximate search](https://github.com/pgvector/pgvector#filtering)
- [OpenAI Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs)
- [Food-model experiment results](experiments/food_model/RESULTS.md)
