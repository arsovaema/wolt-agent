import food_evaluation
import ai_meal_planner
from meal_eligibility import FoodAssessment
import plan_history
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
import asyncio
import meal_search
import weekly_retrieval
import catalog
import auth
import plan_jobs
import service_area
import ingestion
import meal_planning
import ordering
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4
from zoneinfo import ZoneInfo
import os
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from openai import AsyncOpenAI
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import Boolean, JSON, Date, DateTime, Float, ForeignKey, Integer, String, delete, select, text
from sqlalchemy.ext.asyncio import (
    AsyncAttrs,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from meal_planning import summarize_plan

load_dotenv()

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://postgres:mysecretpassword@localhost:5432/postgres",
)

engine = create_async_engine(DATABASE_URL, echo=False)
async_session = async_sessionmaker(engine, expire_on_commit=False)

# Initialize OpenAI Client
openai_client = AsyncOpenAI()

# Calendar boundary for saved plans
PLANNER_TIMEZONE = ZoneInfo("Europe/Skopje")
ESTIMATOR_VERSION = "gpt-4o-mini-food-v2"


async def ensure_menu_item_columns(conn):
    """Add evaluator metadata to an existing installation without losing data.

    `create_all` creates new tables but does not add columns to an existing
    table, so this small idempotent migration keeps the rollout safe.
    """
    columns = {
        "image_url": "VARCHAR(1000)",
        "nutrition_ranges": "JSON NOT NULL DEFAULT '{}'",
        "nutrition_confidence": "VARCHAR(20) NOT NULL DEFAULT 'low'",
        "nutrition_source": "VARCHAR(80) NOT NULL DEFAULT 'legacy'",
        "estimator_version": "VARCHAR(80) NOT NULL DEFAULT 'legacy'",
        "source_fingerprint": "VARCHAR(64)",
        "food_assessment": "JSON NOT NULL DEFAULT '{}'",
    }
    dialect = conn.engine.dialect.name
    for name, definition in columns.items():
        if dialect == "postgresql":
            await conn.execute(text(
                f"ALTER TABLE menu_items ADD COLUMN IF NOT EXISTS {name} {definition}"
            ))
        elif dialect == "sqlite":
            existing = await conn.execute(text("PRAGMA table_info(menu_items)"))
            names = {row[1] for row in existing.fetchall()}
            if name not in names:
                await conn.execute(text(
                    f"ALTER TABLE menu_items ADD COLUMN {name} {definition}"
                ))


@asynccontextmanager
async def lifespan(app: FastAPI):
  async with engine.begin() as conn:
    await conn.run_sync(Base.metadata.create_all)
    await ensure_menu_item_columns(conn)
    await catalog.migrate(conn)
    await service_area.migrate(conn)
    await ingestion.migrate(conn)
    await auth.migrate(conn)
    await plan_jobs.migrate(conn)
    await meal_search.migrate(conn)
  print("Database tables created successfully!")

  # Ingestion runs in workers.py, independently of HTTP worker count.
  try:
    yield
  finally:
    await engine.dispose()



app = FastAPI(title="Wolt Nutrition Agent", lifespan=lifespan)
FRONTEND_DIR = Path(__file__).resolve().parent / "frontend"
app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")


class Base(AsyncAttrs, DeclarativeBase):
  pass


class UserProfile(Base):
    __tablename__ = "user_profiles"
    latitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    longitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    delivery_minute: Mapped[int] = mapped_column(Integer, default=780, server_default="780")
    city: Mapped[str] = mapped_column(String(100), default="Skopje", server_default="Skopje")

    id: Mapped[int] = mapped_column(primary_key=True)

    current_weight_kg: Mapped[float] = mapped_column(Float, nullable=False)
    target_weight_kg: Mapped[float] = mapped_column(Float, nullable=False)
    height_cm: Mapped[float] = mapped_column(Float, nullable=False)
    age: Mapped[int] = mapped_column(Integer, nullable=False)

    sex: Mapped[str] = mapped_column(String(20), nullable=False)
    activity_level: Mapped[str] = mapped_column(String(50), nullable=False)
    goal: Mapped[str] = mapped_column(String(50), nullable=False)
    daily_budget_mkd: Mapped[int] = mapped_column(
    Integer,
    nullable=False,
)
    daily_calorie_target: Mapped[int] = mapped_column(Integer, nullable=False)
    daily_protein_target: Mapped[float] = mapped_column(Float, nullable=False)
    daily_carbs_target: Mapped[float] = mapped_column(Float, nullable=False)
    daily_fat_target: Mapped[float] = mapped_column(Float, nullable=False)

    weights: Mapped[list["WeightLog"]] = relationship(back_populates="user")
    meals: Mapped[list["DailyMealQueue"]] = relationship(back_populates="user")


class DailyPlanState(Base):
    """New table: existing profile/menu/queue tables need no column migration."""
    __tablename__ = "daily_plan_states"

    user_id: Mapped[int] = mapped_column(ForeignKey("user_profiles.id"), primary_key=True)
    daily_fee_allowance_mkd: Mapped[int | None] = mapped_column(Integer, nullable=True)
    generation: Mapped[str] = mapped_column(String(36), nullable=False)
    plan_date: Mapped[date] = mapped_column(Date, nullable=False)
    result: Mapped[dict] = mapped_column(JSON, nullable=False)


class WeeklyPlanState(Base):
    __tablename__ = "weekly_plan_states"

    user_id: Mapped[int] = mapped_column(ForeignKey("user_profiles.id"), primary_key=True)
    generation: Mapped[str] = mapped_column(String(36), nullable=False)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    result: Mapped[dict] = mapped_column(JSON, nullable=False)


class WeightLog(Base):
  __tablename__ = "weight_logs"

  id: Mapped[int] = mapped_column(primary_key=True)
  user_id: Mapped[int] = mapped_column(ForeignKey("user_profiles.id"))
  weight_kg: Mapped[float] = mapped_column(Float, nullable=False)
  logged_date: Mapped[date] = mapped_column(Date, default=date.today)

  user: Mapped["UserProfile"] = relationship(back_populates="weights")


class DailyMealQueue(Base):
  __tablename__ = "daily_meal_queues"

  id: Mapped[int] = mapped_column(primary_key=True)
  user_id: Mapped[int] = mapped_column(ForeignKey("user_profiles.id"))
  meal_type: Mapped[str] = mapped_column(String(20), nullable=False)
  restaurant_name: Mapped[str] = mapped_column(String(150), nullable=False)
  item_name: Mapped[str] = mapped_column(String(150), nullable=False)
  estimated_calories: Mapped[int] = mapped_column(Integer, nullable=False)
  estimated_protein: Mapped[float] = mapped_column(Float, nullable=False)
  estimated_carbs: Mapped[float] = mapped_column(Float, nullable=False)
  estimated_fat: Mapped[float] = mapped_column(Float, nullable=False)
  suitability_score: Mapped[int] = mapped_column(Integer, nullable=False)
  reasoning: Mapped[str] = mapped_column(String(500), nullable=False)
  status: Mapped[str] = mapped_column(String(20), default="pending")
  created_at: Mapped[datetime] = mapped_column(
      DateTime, default=datetime.utcnow
  )
  price_mkd: Mapped[int] = mapped_column(
    Integer,
    nullable=False,
)

  user: Mapped["UserProfile"] = relationship(back_populates="meals")
class MenuItem(Base):
    __tablename__ = "menu_items"
    restaurant_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_available: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    id: Mapped[int] = mapped_column(primary_key=True)

    restaurant_name: Mapped[str] = mapped_column(
        String(150),
        nullable=False,
    )

    item_name: Mapped[str] = mapped_column(
        String(150),
        nullable=False,
    )

    category: Mapped[str] = mapped_column(
        String(100),
        nullable=True,
    )

    description: Mapped[str] = mapped_column(
        String(500),
        nullable=True,
    )

    estimated_calories: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )

    estimated_protein: Mapped[float] = mapped_column(
        Float,
        nullable=False,
    )

    estimated_carbs: Mapped[float] = mapped_column(
        Float,
        nullable=False,
    )

    estimated_fat: Mapped[float] = mapped_column(
        Float,
        nullable=False,
    )
    price_mkd: Mapped[int] = mapped_column(
    Integer,
    nullable=False,
    )

    image_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    food_assessment: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    nutrition_ranges: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    nutrition_confidence: Mapped[str] = mapped_column(String(20), default="low", nullable=False)
    nutrition_source: Mapped[str] = mapped_column(String(80), default="unknown", nullable=False)
    estimator_version: Mapped[str] = mapped_column(String(80), default="legacy", nullable=False)
    source_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)

    analyzed_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
    )


def planner_today():
    return datetime.now(PLANNER_TIMEZONE).date()


def plan_status(status, message):
    return {"status": status, "message": message, "meals": []}


async def locked_profile_and_state(db):
    # Serialize short state transitions across workers and profile updates.
    # No locks are held while making external requests or searching plans.
    result = await db.execute(
        select(UserProfile).where(UserProfile.id == auth.current_user_id())
        .with_for_update().execution_options(populate_existing=True)
    )
    profile = result.scalar_one_or_none()
    if profile is None:
        return None, None
    state = await db.get(DailyPlanState, profile.id, populate_existing=True)
    if state is None:
        state = DailyPlanState(
            user_id=profile.id, daily_fee_allowance_mkd=None,
            generation=str(uuid4()), plan_date=planner_today(),
            result=plan_status("refresh_required", "Generate a plan for the current profile."),
        )
        db.add(state)
    return profile, state


async def clear_pending_meals(db, user_id):
    await db.execute(delete(DailyMealQueue).where(
        DailyMealQueue.user_id == user_id, DailyMealQueue.status == "pending"
    ))


async def begin_plan_refresh(db):
    profile, state = await locked_profile_and_state(db)
    if profile is None:
        await db.rollback()
        return None, None, None, plan_status("profile_required", "Save a user profile first.")
    await clear_pending_meals(db, profile.id)
    state.generation = str(uuid4())
    state.plan_date = planner_today()
    state.result = plan_status("refreshing", "A new daily plan is being generated.")
    if state.daily_fee_allowance_mkd is None:
        state.result = plan_status("fees_required", "Set daily_fee_allowance_mkd on your profile to reserve money for delivery, service fees and required extras. Enter zero only when no allowance is needed.")
    snapshot = SimpleNamespace(**{column.key: getattr(profile, column.key) for column in UserProfile.__table__.columns})
    generation, fee, response = state.generation, state.daily_fee_allowance_mkd, dict(state.result)
    await db.commit()
    return snapshot, generation, fee, response


async def publish_plan_result(db, generation, result):
    profile, state = await locked_profile_and_state(db)
    if state is None or state.generation != generation:
        await db.rollback()
        return plan_status("superseded", "This refresh was superseded by a profile update or a newer refresh.")
    if state.plan_date != planner_today():
        result = plan_status("refresh_required", "The day changed during generation. Generate a plan for today.")
    await clear_pending_meals(db, profile.id)
    state.result = {**result, "plan_date": state.plan_date.isoformat()}
    # Poor-fit meals remain visible as alternatives, never as accepted recommendations.
    if result["status"] == "ready":
        for meal in result["meals"]:
            db.add(DailyMealQueue(
                user_id=profile.id, meal_type="meal",
                restaurant_name=meal["restaurant_name"], item_name=meal["item_name"],
                estimated_calories=meal["calories"], estimated_protein=meal["protein"],
                estimated_carbs=meal["carbs"], estimated_fat=meal["fat"],
                price_mkd=meal["price_mkd"], suitability_score=round(result["daily_plan_score"]),
                reasoning="Part of a daily plan matching the configured nutrition tolerances and estimated budget. The score describes the whole day.",
                status="pending",
            ))
    response = dict(state.result)
    await db.commit()
    return response


async def current_daily_plan(db):
    state = await db.get(DailyPlanState, auth.current_user_id())
    if state is None:
        return plan_status("refresh_required", "Generate a plan for the current profile; previous unversioned recommendations are not shown.")
    if state.plan_date != planner_today():
        return plan_status("refresh_required", "The previous plan has expired. Generate a plan for today.")
    if state.result.get('status') in ('ready','poor_fit') and not await cached_meals_valid(db,state.result.get('meals',[]),days=1):
        return plan_status('refresh_required','Menu prices, availability or delivery coverage changed. Generate a new day.')
    return state.result


async def cached_meals_valid(db, meals, days=7):
    bind = getattr(db, 'bind', None) or db.session.bind
    if bind.dialect.name != 'postgresql':
        return True
    if not meals or any('id' not in meal for meal in meals):
        return False
    profile = await db.get(UserProfile, auth.current_user_id())
    if profile is None:
        return False
    restaurants, _ = await service_area.eligible_restaurants(db, profile, days=days)
    rows = (await db.execute(text("""SELECT id,price_mkd FROM menu_items WHERE
        id=ANY(CAST(:ids AS integer[])) AND restaurant_id=ANY(CAST(:restaurants AS integer[]))
        AND is_available AND last_seen_at>=(now() AT TIME ZONE 'UTC')-interval '72 hours'"""),
        {'ids':list({meal['id'] for meal in meals}),'restaurants':restaurants})).all()
    prices = dict(rows)
    return all(prices.get(meal['id']) == meal['price_mkd'] for meal in meals)


def menu_item_to_candidate(item: MenuItem) -> dict:
    return {
        "item_name": item.item_name,
        "category": item.category,
        "description": item.description,
        "food_assessment": item.food_assessment or {},
        "restaurant_id": item.restaurant_id,
        "restaurant_name": item.restaurant_name,
        "calories": item.estimated_calories,
        "protein": item.estimated_protein,
        "carbs": item.estimated_carbs,
        "fat": item.estimated_fat,
        "price_mkd": item.price_mkd,
        "image_url": item.image_url,
        "nutrition_ranges": item.nutrition_ranges or {},
        "nutrition_confidence": item.nutrition_confidence or "low",
        "nutrition_source": item.nutrition_source or "unknown",
        "estimator_version": item.estimator_version or "legacy",
    }


async def weekly_candidates(db, profile, fee_allowance, previous_ids):
    bind = getattr(db, "bind", None) or db.session.bind
    if bind.dialect.name == "postgresql":
        return await weekly_retrieval.retrieve_candidates(db, openai_client, profile, fee_allowance, previous_ids)
    # Offline SQLite test installations have no pgvector or network dependency.
    rows = (await db.execute(select(MenuItem).order_by(MenuItem.id).limit(weekly_retrieval.CANDIDATE_LIMIT))).scalars().all()
    return [dict(menu_item_to_candidate(item), id=item.id) for item in rows], {"mode": "sqlite", "candidate_count": len(rows)}


async def publish_weekly_result(db, generation, start_date, result):
    profile, _ = await locked_profile_and_state(db)
    weekly = (await db.execute(select(WeeklyPlanState).where(WeeklyPlanState.user_id == auth.current_user_id())
        .execution_options(populate_existing=True))).scalar_one_or_none()
    if profile is None or weekly is None or weekly.generation != generation:
        await db.rollback()
        return plan_status("superseded", "Your profile or plan changed during generation. Generate a new week.")
    result = dict(result, selection_history=weekly.result.get("selection_history", []))
    weekly.start_date = start_date
    weekly.result = result
    await db.commit()
    return result


async def generate_weekly_plan(db: AsyncSession):
    """Retrieve candidates and select seven validated restaurant days."""
    profile, state = await locked_profile_and_state(db)
    if profile is None:
        await db.rollback()
        return plan_status("profile_required", "Save your profile to build your seven-day plan.")
    fee_allowance = state.daily_fee_allowance_mkd
    if fee_allowance is None:
        await db.rollback()
        return plan_status("fees_required", "Set a daily fee allowance in your profile.")
    if fee_allowance >= profile.daily_budget_mkd:
        await db.rollback()
        return plan_status("budget_invalid", f"Your daily fee allowance ({fee_allowance} MKD) must be less than your total daily budget ({profile.daily_budget_mkd} MKD). Update your profile to leave money for meals.")
    snapshot = SimpleNamespace(**{column.key: getattr(profile, column.key) for column in UserProfile.__table__.columns})
    weekly = await db.get(WeeklyPlanState, profile.id)
    previous = weekly.result if weekly else {}
    history = plan_history.prior_weeks(previous)
    _, _, previous_ids = plan_history.usage_from_history(history)
    previous_keys = {(m['restaurant_name'], m['item_name']) for day in (history[-1] if history else []) for m in day}
    generation, start_date = str(uuid4()), planner_today()
    if weekly is None:
        weekly = WeeklyPlanState(user_id=profile.id)
        db.add(weekly)
    weekly.generation = generation
    weekly.start_date = start_date
    weekly.result = dict(plan_status("refreshing", "Finding meals for your goal and building seven days."), selection_history=history)
    await db.commit()
    try:
        candidates, retrieval = await weekly_candidates(db, snapshot, fee_allowance, previous_ids)
        if len(candidates) < 3:
            return await publish_weekly_result(db, generation, start_date, {
                "status": "no_plan", "message": "Not enough fresh, affordable meals are available in the city catalog. Try a higher budget or refresh the menus.",
                "weekly_plan": [], "retrieval": retrieval})
        candidates, evaluation = await food_evaluation.assess_candidates(db, openai_client, candidates)
        try:
            selected_days, planning = await ai_meal_planner.create_plan(
                openai_client, candidates, snapshot, fee_allowance, history)
        except ai_meal_planner.PlanRequirementsError as exc:
            return await publish_weekly_result(db, generation, start_date, {
                "status": "no_plan", "message": "Could not build seven affordable days with three eligible meals per day from 7 different restaurants. Refresh menus or adjust the budget.",
                "weekly_plan": [], "retrieval": retrieval, "validation_errors": exc.errors})
        planning["food_evaluation"] = evaluation
        if not await cached_meals_valid(db, [meal for day in selected_days for meal in day]):
            return await publish_weekly_result(db, generation, start_date, {
                "status": "refresh_failed", "message": "Menu prices or availability changed while planning. Please generate again.", "weekly_plan": []})
        used_keys, days, restaurant_usage = set(), [], {}
        for offset, best_meals in enumerate(selected_days):
            day = summarize_plan(best_meals, snapshot, fee_allowance)
            day.update(day_number=offset + 1, date=(start_date + timedelta(days=offset)).isoformat())
            day["restaurant_name"] = best_meals[0]["restaurant_name"]
            days.append(day)
            restaurant_usage[meal_planning.restaurant_key(best_meals[0])] = 1
            used_keys.update((m["restaurant_name"], m["item_name"]) for m in best_meals)
        poor_days = sum(day["status"] == "poor_fit" for day in days)
        result = {
            "status": "poor_fit" if poor_days else "ready",
            "message": (f"Your seven-day plan is ready. {poor_days} days are affordable alternatives outside your nutrition targets."
                        if poor_days else "Your seven-day plan matches your nutrition targets and estimated budget."),
            "start_date": start_date.isoformat(), "expires_on": (start_date + timedelta(days=7)).isoformat(),
            "weekly_plan": days, "retrieval": retrieval, "planning": planning,
            "variety": {"unique_meals": len(used_keys), "scheduled_meals": 21,
                        "reused_meals": 21 - len(used_keys), "reused_from_previous_plan": len(used_keys & previous_keys), "history_weeks": len(history), "unique_restaurants": len(restaurant_usage), "minimum_restaurants": 7, "maximum_days_per_restaurant": 1, "goal": snapshot.goal},
        }
        result['single_restaurant_per_day'] = True
        return await publish_weekly_result(db, generation, start_date, result)
    except Exception:
        await db.rollback()
        meal_search.logger.exception("Weekly planning failed")
        return await publish_weekly_result(db, generation, start_date, {
            "status": "refresh_failed", "message": "We couldn't build your week. Please try again.", "weekly_plan": []})


async def current_weekly_plan(db):
    weekly = await db.get(WeeklyPlanState, auth.current_user_id())
    if weekly is None:
        return plan_status("refresh_required", "Generate your first seven-day plan.")
    # Only completed plans expire. In-progress/error states retain their meaning.
    if weekly.result.get("status") in {"ready", "poor_fit"} and planner_today() >= weekly.start_date + timedelta(days=7):
        return {**weekly.result, "status": "expired", "message": "Your seven-day plan has finished. Generate a new week."}
    if weekly.result.get('status') in ('ready','poor_fit'):
        saved_venues = {meal_planning.restaurant_key(meal)
                        for day in weekly.result.get('weekly_plan', []) for meal in day.get('meals', [])}
        if len(saved_venues) < meal_planning.MIN_WEEKLY_RESTAURANTS:
            return plan_status('refresh_required', 'Generate a new week: each day must use a different restaurant.')
        if any(len({('id', meal['restaurant_id']) if meal.get('restaurant_id') is not None
                    else ('name', meal.get('restaurant_name')) for meal in day.get('meals',[])}) > 1
               for day in weekly.result.get('weekly_plan',[])):
            return plan_status('refresh_required','Generate a new week: each day now uses one restaurant for a single cart.')
        days = max(1, (weekly.start_date + timedelta(days=7) - planner_today()).days)
        meals = [meal for day in weekly.result.get('weekly_plan',[]) for meal in day.get('meals',[])]
        if not await cached_meals_valid(db,meals,days=days):
            return plan_status('refresh_required','Menu prices, availability or delivery coverage changed. Generate a new week.')
    return weekly.result


async def get_db():
  async with async_session() as session:
    yield session


# Pydantic Schemas
class UserProfileCreate(BaseModel):
    latitude: float | None = Field(default=None, ge=-90, le=90, allow_inf_nan=False)
    longitude: float | None = Field(default=None, ge=-180, le=180, allow_inf_nan=False)
    delivery_minute: int = Field(default=780, ge=0, lt=1440)
    city: str = Field(default="Skopje", min_length=1, max_length=100)
    current_weight_kg: float = Field(gt=0, allow_inf_nan=False)
    target_weight_kg: float = Field(gt=0, allow_inf_nan=False)
    height_cm: float = Field(gt=0, allow_inf_nan=False)
    age: int = Field(gt=0)

    sex: str
    activity_level: str = "moderately active"
    goal: str
    daily_budget_mkd: int = Field(gt=0)
    daily_fee_allowance_mkd: int | None = Field(default=None, ge=0, description="Daily reserve for all delivery, service fees and required extras. Omit to preserve the existing allowance; null means unknown.")

    @field_validator('city')
    @classmethod
    def city_not_blank(cls, value):
        if not value.strip():
            raise ValueError('City cannot be blank')
        return value.strip()

    @model_validator(mode='after')
    def coordinates_pair(self):
        if (self.latitude is None) != (self.longitude is None):
            raise ValueError('Provide both delivery latitude and longitude')
        return self


class CalculatedNutritionalPlan(BaseModel):
    daily_calorie_target: int = Field(
        description="Recommended total daily calories"
    )

    daily_protein_target: float = Field(
        description="Recommended daily protein in grams"
    )

    daily_carbs_target: float = Field(
        description="Recommended daily carbohydrates in grams"
    )

    daily_fat_target: float = Field(
        description="Recommended daily fat in grams"
    )

    reasoning: str = Field(
        description="Reasoning behind the nutrition plan"
    )


class MacroNutrients(BaseModel):
  calories: int = Field(description="Estimated total calories for the dish")
  protein_grams: float = Field(description="Estimated grams of protein")
  carbs_grams: float = Field(description="Estimated grams of carbohydrates")
  fat_grams: float = Field(description="Estimated grams of fat")
  calories_low: int = Field(default=0, ge=0, description="Plausible lower calorie bound")
  calories_high: int = Field(default=0, ge=0, description="Plausible upper calorie bound")
  protein_low: float = Field(default=0, ge=0, description="Plausible lower protein bound")
  protein_high: float = Field(default=0, ge=0, description="Plausible upper protein bound")
  carbs_low: float = Field(default=0, ge=0, description="Plausible lower carbohydrate bound")
  carbs_high: float = Field(default=0, ge=0, description="Plausible upper carbohydrate bound")
  fat_low: float = Field(default=0, ge=0, description="Plausible lower fat bound")
  fat_high: float = Field(default=0, ge=0, description="Plausible upper fat bound")
  confidence: str = Field(default="low", description="high, medium, or low confidence")


def complete_nutrition_uncertainty(macros: MacroNutrients) -> MacroNutrients:
    """Give legacy/partial model responses explicit conservative ranges."""
    central = {
        "calories": macros.calories,
        "protein": macros.protein_grams,
        "carbs": macros.carbs_grams,
        "fat": macros.fat_grams,
    }
    ranges = {
        "calories": (macros.calories_low, macros.calories_high),
        "protein": (macros.protein_low, macros.protein_high),
        "carbs": (macros.carbs_low, macros.carbs_high),
        "fat": (macros.fat_low, macros.fat_high),
    }
    values = {}
    for key, (low, high) in ranges.items():
        if high == 0 and central[key] > 0:
            low, high = round(central[key] * 0.8, 2), round(central[key] * 1.2, 2)
        if low > central[key] or high < central[key] or low < 0:
            raise ValueError(f"Invalid uncertainty range for {key}")
        values[key] = (low, high)
    if macros.confidence not in {"high", "medium", "low"}:
        raise ValueError("Confidence must be high, medium, or low")
    return macros.model_copy(update={
        "calories_low": values["calories"][0], "calories_high": values["calories"][1],
        "protein_low": values["protein"][0], "protein_high": values["protein"][1],
        "carbs_low": values["carbs"][0], "carbs_high": values["carbs"][1],
        "fat_low": values["fat"][0], "fat_high": values["fat"][1],
    })


class AssessedNutrition(MacroNutrients):
    food_assessment: FoodAssessment


class EvaluatedMeal(BaseModel):
  item_name: str
  restaurant_name: str
  price_mkd: int
  nutrition_ranges: dict = Field(default_factory=dict)
  nutrition_confidence: str = "low"
  nutrition_source: str = "unknown"
  estimator_version: str = ESTIMATOR_VERSION
  macros: MacroNutrients
  suitability_score: int = Field(
      description="Whole-day nutrition matching score from 1 to 10; not an individual meal score"
  )
  reasoning: str = Field(
      description="Brief explanation of why this fits or violates macro goals"
  )


@app.get("/")
async def root():
  return FileResponse(FRONTEND_DIR / "index.html", headers={"Cache-Control": "no-store"})


def calculate_tdee(
    weight_kg: float,
    height_cm: float,
    age: int,
    sex: str,
    activity_level: str
) -> float:


    sex = sex.lower().strip()

    if sex == "male":
        bmr = (
            10 * weight_kg
            + 6.25 * height_cm
            - 5 * age
            + 5
        )

    elif sex == "female":
        bmr = (
            10 * weight_kg
            + 6.25 * height_cm
            - 5 * age
            - 161
        )

    else:
        raise ValueError("sex must be 'male' or 'female'")

    activity_factors = {
        "sedentary": 1.2,
        "lightly active": 1.375,
        "moderately active": 1.55,
        "very active": 1.725,
        "extra active": 1.9,
    }

    factor = activity_factors.get(
        activity_level.lower().strip()
    )

    if factor is None:
        raise ValueError(
            f"Unsupported activity level: {activity_level}"
        )

    return bmr * factor

@app.post("/api/user/profile")
async def create_or_update_profile(
    profile_data: UserProfileCreate,
    db: AsyncSession = Depends(get_db),
):
    # 1. Calculate a deterministic maintenance-calorie baseline.
    try:
        tdee = calculate_tdee(
            weight_kg=profile_data.current_weight_kg,
            height_cm=profile_data.height_cm,
            age=profile_data.age,
            sex=profile_data.sex,
            activity_level=profile_data.activity_level,
        )

        goal = profile_data.goal.lower().strip()

        if goal == "muscle gain":
            protein_min_per_kg = 1.6
            protein_max_per_kg = 2.2
        elif goal in ["fat loss", "weight loss"]:
            protein_min_per_kg = 1.6
            protein_max_per_kg = 2.4
        else:
            protein_min_per_kg = 1.2
            protein_max_per_kg = 2.2

        protein_min_g = (
            profile_data.current_weight_kg * protein_min_per_kg
        )
        protein_max_g = (
            profile_data.current_weight_kg * protein_max_per_kg
        )

    except ValueError as e:
        raise HTTPException(
            status_code=400,
            detail=str(e),
        )

    # 2. Ask AI to build a goal-specific nutrition plan.
    prompt = f"""
You are an expert sports nutrition assistant.

Create a nutrition plan for this user.

User information:
- Current weight: {profile_data.current_weight_kg} kg
- Target weight: {profile_data.target_weight_kg} kg
- Height: {profile_data.height_cm} cm
- Age: {profile_data.age}
- Sex: {profile_data.sex}
- Activity level: {profile_data.activity_level}
- Goal: {profile_data.goal}

The user's estimated maintenance calories (TDEE) are approximately:
{round(tdee)} kcal/day.

Using this TDEE as the baseline, determine an appropriate nutrition
plan for the user's specific goal.

Return:
- daily calorie target
- daily protein target in grams
- daily carbohydrate target in grams
- daily fat target in grams

Important:
- Protein provides 4 kcal per gram.
- Carbohydrates provide 4 kcal per gram.
- Fat provides 9 kcal per gram.
- The calories calculated from the macro targets should approximately
  match the daily calorie target.

For this specific user:
- The daily protein target MUST be between
  {protein_min_g:.1f} g and {protein_max_g:.1f} g.
- Do not return a protein value outside this range.

For muscle gain:
- Prefer a moderate calorie surplus.

For weight or fat loss:
- Prefer a moderate calorie deficit.

Briefly explain the reasoning behind the plan.
"""

    try:
        completion = await openai_client.beta.chat.completions.parse(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are an expert sports nutrition assistant "
                        "creating evidence-informed nutrition plans."
                    ),
                },
                {
                    "role": "user",
                    "content": prompt,
                },
            ],
            response_format=CalculatedNutritionalPlan,
        )

        plan = completion.choices[0].message.parsed

        if plan is None:
            raise ValueError("AI did not return a nutrition plan.")

        original_protein = plan.daily_protein_target

        # 3. Clamp protein to the allowed range first.
        if plan.daily_protein_target < protein_min_g:
            plan.daily_protein_target = round(protein_min_g, 1)
        elif plan.daily_protein_target > protein_max_g:
            plan.daily_protein_target = round(protein_max_g, 1)

        # Recalculate carbohydrates AFTER any protein correction so that
        # the macro calories still match the calorie target.
        remaining_calories = (
            plan.daily_calorie_target
            - plan.daily_protein_target * 4
            - plan.daily_fat_target * 9
        )

        if remaining_calories <= 0:
            raise ValueError(
                "Generated fat/protein targets leave no calories "
                "for carbohydrates."
            )

        plan.daily_carbs_target = round(
            remaining_calories / 4,
            1,
        )

        # Validate that macro calories approximately match the calorie target.
        macro_calories = (
            plan.daily_protein_target * 4
            + plan.daily_carbs_target * 4
            + plan.daily_fat_target * 9
        )

        difference = abs(
            macro_calories - plan.daily_calorie_target
        )

        if difference > 150:
            raise ValueError(
                "Generated nutrition plan is inconsistent. "
                f"Calories calculated from macros = {round(macro_calories)}, "
                f"daily calorie target = {plan.daily_calorie_target}"
            )

        # Validate that the calorie surplus/deficit matches the goal.
        calorie_difference = plan.daily_calorie_target - tdee

        if goal == "muscle gain":
            if calorie_difference < 100 or calorie_difference > 500:
                raise ValueError(
                    "Generated calorie target is inconsistent with the "
                    "muscle-gain goal. "
                    f"Surplus = {round(calorie_difference)} kcal"
                )

        elif goal in ["fat loss", "weight loss"]:
            if calorie_difference > -100 or calorie_difference < -800:
                raise ValueError(
                    "Generated calorie target is inconsistent with the "
                    "weight-loss goal. "
                    f"Deficit = {round(calorie_difference)} kcal"
                )

        # Keep the explanation consistent with any automatic protein correction.
        if original_protein != plan.daily_protein_target:
            plan.reasoning += (
                f" The protein target was adjusted by the application "
                f"from {original_protein:.1f} g to "
                f"{plan.daily_protein_target:.1f} g to remain within "
                f"the configured range for this goal. "
                f"Carbohydrates were recalculated to keep the total "
                f"calories aligned with the daily target."
            )

    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"AI nutritional calculation failed: {str(e)}",
        )

    # 4. Save or update profile in PostgreSQL.
    try:
        db_profile, state = await locked_profile_and_state(db)

        if not db_profile:
          db_profile = UserProfile(
          id=auth.current_user_id(),
          city=profile_data.city.strip(),
          latitude=profile_data.latitude, longitude=profile_data.longitude,
          delivery_minute=profile_data.delivery_minute,
          current_weight_kg=profile_data.current_weight_kg,
          target_weight_kg=profile_data.target_weight_kg,
          height_cm=profile_data.height_cm,
          age=profile_data.age,
          sex=profile_data.sex,
          activity_level=profile_data.activity_level,
          goal=profile_data.goal,

          daily_calorie_target=plan.daily_calorie_target,
          daily_protein_target=plan.daily_protein_target,
          daily_carbs_target=plan.daily_carbs_target,
          daily_fat_target=plan.daily_fat_target,

          daily_budget_mkd=profile_data.daily_budget_mkd,)
          db.add(db_profile)
        else:
            db_profile.city = profile_data.city.strip()
            db_profile.latitude = profile_data.latitude
            db_profile.longitude = profile_data.longitude
            db_profile.delivery_minute = profile_data.delivery_minute
            db_profile.current_weight_kg = profile_data.current_weight_kg
            db_profile.target_weight_kg = profile_data.target_weight_kg
            db_profile.height_cm = profile_data.height_cm
            db_profile.age = profile_data.age
            db_profile.sex = profile_data.sex
            db_profile.activity_level = profile_data.activity_level
            db_profile.goal = profile_data.goal
            db_profile.daily_calorie_target = plan.daily_calorie_target
            db_profile.daily_protein_target = plan.daily_protein_target
            db_profile.daily_carbs_target = plan.daily_carbs_target
            db_profile.daily_fat_target = plan.daily_fat_target
            db_profile.daily_budget_mkd = profile_data.daily_budget_mkd

        if state is None:
            await db.flush()
            state = DailyPlanState(user_id=db_profile.id)
            db.add(state)
        if "daily_fee_allowance_mkd" in profile_data.model_fields_set:
            state.daily_fee_allowance_mkd = profile_data.daily_fee_allowance_mkd
        state.generation = str(uuid4())
        state.plan_date = planner_today()
        state.result = plan_status("refresh_required", "The profile changed. Generate a new plan for the updated goals and budget.")
        await clear_pending_meals(db, db_profile.id)
        weekly_state = await db.get(WeeklyPlanState, db_profile.id)
        if weekly_state is not None:
            weekly_state.result = plan_status("refresh_required", "The profile changed. Generate a new seven-day plan.")
            weekly_state.start_date = planner_today() - timedelta(days=7)
            weekly_state.generation = str(uuid4())
        await db.commit()
        await db.refresh(db_profile)

        return {
            "message": "Profile saved successfully!",
            "estimated_tdee": round(tdee),
            "nutritional_plan": plan,
            "profile": {
                "id": db_profile.id,
                "city": db_profile.city,
                "latitude": db_profile.latitude, "longitude": db_profile.longitude,
                "delivery_minute": db_profile.delivery_minute,
                "current_weight_kg": db_profile.current_weight_kg,
                "target_weight_kg": db_profile.target_weight_kg,
                "height_cm": db_profile.height_cm,
                "age": db_profile.age,
                "sex": db_profile.sex,
                "activity_level": db_profile.activity_level,
                "goal": db_profile.goal,
                "daily_budget_mkd": db_profile.daily_budget_mkd,
                "daily_fee_allowance_mkd": state.daily_fee_allowance_mkd,
                "daily_calorie_target": db_profile.daily_calorie_target,
                "daily_protein_target": db_profile.daily_protein_target,
                "daily_carbs_target": db_profile.daily_carbs_target,
                "daily_fat_target": db_profile.daily_fat_target,
            },
        }

    except Exception as e:
        await db.rollback()

        print("DATABASE ERROR TYPE:", type(e).__name__)
        print("DATABASE ERROR:", repr(e))

        raise HTTPException(
            status_code=500,
            detail=f"Database error: {str(e)}",
        )
@app.post("/api/agent/fetch-meals")
async def fetch_meals_now(db: AsyncSession = Depends(get_db)):
    profile, generation, fee, response = await begin_plan_refresh(db)
    if profile is None or fee is None:
        return response
    if fee >= profile.daily_budget_mkd:
        return await publish_plan_result(db,generation,plan_status('budget_invalid','Set a daily fee allowance lower than your total budget.'))
    try:
        candidates, _ = await weekly_candidates(db, profile, fee, [])
        candidates, evaluation = await food_evaluation.assess_candidates(db, openai_client, candidates)
        try:
            selected, planning = await ai_meal_planner.create_plan(openai_client, candidates, profile, fee, day_count=1)
            planning["food_evaluation"] = evaluation
            chosen = selected[0]
            if not await cached_meals_valid(db, chosen, days=1):
                raise RuntimeError("Catalog changed during planning")
            result = dict(summarize_plan(chosen, profile, fee), planning=planning)
        except ai_meal_planner.PlanRequirementsError as exc:
            chosen = []
            result = dict(plan_status("no_plan", "No day meeting budget and nutrition requirements could be validated."), validation_errors=exc.errors)
        if not chosen:
            result["budget"] = {
                "daily_limit_mkd": profile.daily_budget_mkd,
                "daily_fee_allowance_mkd": fee,
                "available_for_food_mkd": profile.daily_budget_mkd - fee,
                "is_estimate": True,
            }
    except Exception:
        await db.rollback()
        meal_search.logger.exception("Daily planning failed")
        result = plan_status("refresh_failed", "We couldn't build your day. Please try again.")
    return await publish_plan_result(db, generation, result)


@app.post("/api/agent/weekly-plan")
async def create_weekly_plan(db: AsyncSession = Depends(get_db)):
    profile, state = await locked_profile_and_state(db)
    if profile is None:
        await db.rollback()
        return plan_status("profile_required", "Save your profile first.")
    fee = state.daily_fee_allowance_mkd
    if fee is None or fee >= profile.daily_budget_mkd:
        await db.rollback()
        return plan_status("budget_invalid", "Set a daily fee allowance lower than your total budget.")
    uid = profile.id
    await db.commit()
    return await plan_jobs.enqueue(db, uid)


@app.get("/api/agent/weekly-plan")
async def get_weekly_plan(db: AsyncSession = Depends(get_db)):
    pending = await plan_jobs.status(db, auth.current_user_id())
    return pending or await current_weekly_plan(db)


@app.get("/api/agent/daily-plan")
async def get_daily_plan(db: AsyncSession = Depends(get_db)):
    """Status, alternatives, totals and estimated budget for the current day."""
    return await current_daily_plan(db)


@app.get("/api/agent/meal-queue", response_model=list[EvaluatedMeal])
async def get_meal_queue(db: AsyncSession = Depends(get_db)):
    """Compatibility list: only current, acceptable recommendations are returned."""
    plan = await current_daily_plan(db)
    if plan["status"] != "ready":
        return []
    return [EvaluatedMeal(
        item_name=meal["item_name"], restaurant_name=meal["restaurant_name"],
        price_mkd=meal["price_mkd"],
        nutrition_ranges=meal.get("nutrition_ranges", {}),
        nutrition_confidence=meal.get("nutrition_confidence", "low"),
        nutrition_source=meal.get("nutrition_source", "unknown"),
        estimator_version=meal.get("estimator_version", ESTIMATOR_VERSION),
        macros=MacroNutrients(
            calories=meal["calories"], protein_grams=meal["protein"],
            carbs_grams=meal["carbs"], fat_grams=meal["fat"],
            calories_low=meal.get("nutrition_ranges", {}).get("calories", {}).get("low", 0),
            calories_high=meal.get("nutrition_ranges", {}).get("calories", {}).get("high", 0),
            protein_low=meal.get("nutrition_ranges", {}).get("protein", {}).get("low", 0),
            protein_high=meal.get("nutrition_ranges", {}).get("protein", {}).get("high", 0),
            carbs_low=meal.get("nutrition_ranges", {}).get("carbs", {}).get("low", 0),
            carbs_high=meal.get("nutrition_ranges", {}).get("carbs", {}).get("high", 0),
            fat_low=meal.get("nutrition_ranges", {}).get("fat", {}).get("low", 0),
            fat_high=meal.get("nutrition_ranges", {}).get("fat", {}).get("high", 0),
        ),
        suitability_score=round(plan["daily_plan_score"]),
        reasoning="Selected as part of today's daily plan. This score describes the whole day; see /api/agent/daily-plan for totals and target differences.",
    ) for meal in plan["meals"]]


meal_search.register_routes(app, get_db, lambda: openai_client)


@app.get("/api/user/profile")
async def get_user_profile(db: AsyncSession = Depends(get_db)):
    profile = await db.get(UserProfile, auth.current_user_id())
    if profile is None:
        return {"profile": None}
    state = await db.get(DailyPlanState, auth.current_user_id())
    data = {column.key: getattr(profile, column.key) for column in UserProfile.__table__.columns}
    data["daily_fee_allowance_mkd"] = state.daily_fee_allowance_mkd if state else None
    return {"profile": data}


auth.install(app, async_session)

ordering.register_routes(app, get_db)
