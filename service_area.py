"""Verified delivery circles and weekly opening windows, evaluated before retrieval."""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import text


class DeliveryArea(BaseModel):
    latitude: float = Field(ge=-90, le=90, allow_inf_nan=False)
    longitude: float = Field(ge=-180, le=180, allow_inf_nan=False)
    radius_km: float = Field(gt=0, le=100, allow_inf_nan=False)


class OpeningWindow(BaseModel):
    weekday: int = Field(ge=0, le=6, description='Monday=0')
    start_minute: int = Field(ge=0, lt=1440)
    end_minute: int = Field(gt=0, le=1440)

    @model_validator(mode='after')
    def ordered(self):
        if self.end_minute <= self.start_minute:
            raise ValueError('Split overnight hours into two weekday windows')
        return self


class RestaurantService(BaseModel):
    restaurant_id: int = Field(gt=0)
    latitude: float = Field(ge=-90, le=90, allow_inf_nan=False)
    longitude: float = Field(ge=-180, le=180, allow_inf_nan=False)
    timezone: str = 'Europe/Skopje'
    available: bool = True
    source: str = Field(min_length=1, max_length=1000)
    verified_until: datetime
    delivery_areas: list[DeliveryArea] = Field(min_length=1, max_length=100)
    opening_hours: list[OpeningWindow] = Field(min_length=1, max_length=100)

    @model_validator(mode='after')
    def valid(self):
        ZoneInfo(self.timezone)
        if self.verified_until.tzinfo is None:
            raise ValueError('verified_until must include a timezone')
        if self.verified_until <= datetime.now(timezone.utc):
            raise ValueError('Service verification has expired')
        return self


async def migrate(conn):
    if conn.dialect.name != 'postgresql':
        return
    await conn.execute(text('SELECT pg_advisory_xact_lock(78149322)'))
    if (await conn.execute(text("SELECT 1 FROM app_migrations WHERE version='service-v1'"))).scalar():
        return
    for definition in ('latitude DOUBLE PRECISION', 'longitude DOUBLE PRECISION',
                       "timezone TEXT NOT NULL DEFAULT 'Europe/Skopje'", 'available BOOLEAN NOT NULL DEFAULT true',
                       'service_source TEXT', 'service_verified_until TIMESTAMPTZ'):
        await conn.execute(text('ALTER TABLE restaurants ADD COLUMN IF NOT EXISTS ' + definition))
    await conn.execute(text('''CREATE TABLE restaurant_delivery_areas (
        id BIGSERIAL PRIMARY KEY, restaurant_id INTEGER NOT NULL REFERENCES restaurants(id) ON DELETE CASCADE,
        latitude DOUBLE PRECISION NOT NULL CHECK(latitude BETWEEN -90 AND 90),
        longitude DOUBLE PRECISION NOT NULL CHECK(longitude BETWEEN -180 AND 180),
        radius_km DOUBLE PRECISION NOT NULL CHECK(radius_km>0 AND radius_km<=100))'''))
    await conn.execute(text('CREATE INDEX delivery_restaurant ON restaurant_delivery_areas(restaurant_id)'))
    await conn.execute(text('''CREATE TABLE restaurant_opening_hours (
        restaurant_id INTEGER NOT NULL REFERENCES restaurants(id) ON DELETE CASCADE,
        weekday INTEGER NOT NULL CHECK(weekday BETWEEN 0 AND 6),
        start_minute INTEGER NOT NULL CHECK(start_minute>=0),
        end_minute INTEGER NOT NULL CHECK(end_minute<=1440 AND end_minute>start_minute),
        PRIMARY KEY(restaurant_id,weekday,start_minute))'''))
    for definition in ('latitude DOUBLE PRECISION', 'longitude DOUBLE PRECISION',
                       'delivery_minute INTEGER NOT NULL DEFAULT 780'):
        await conn.execute(text('ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS ' + definition))
    for nutrient in ('calories', 'protein'):
        await conn.execute(text(f'CREATE INDEX IF NOT EXISTS menu_local_{nutrient} ON menu_items(restaurant_id,estimated_{nutrient},id) WHERE is_available'))
    await conn.execute(text("INSERT INTO app_migrations(version) VALUES ('service-v1')"))


async def import_service(db, records):
    # Validate the complete file before any write. IDs come from the catalog.
    entries = [RestaurantService.model_validate(item) for item in records]
    for entry in entries:
        params = entry.model_dump(exclude={'delivery_areas', 'opening_hours'})
        found = (await db.execute(text('''UPDATE restaurants SET latitude=:latitude,longitude=:longitude,
            timezone=:timezone,available=:available,service_source=:source,service_verified_until=:verified_until
            WHERE id=:restaurant_id RETURNING id'''), params)).scalar()
        if found is None:
            raise ValueError(f'Unknown restaurant ID {entry.restaurant_id}')
        for table in ('restaurant_delivery_areas', 'restaurant_opening_hours'):
            await db.execute(text(f'DELETE FROM {table} WHERE restaurant_id=:id'), {'id':found})
        for area in entry.delivery_areas:
            await db.execute(text('''INSERT INTO restaurant_delivery_areas(restaurant_id,latitude,longitude,radius_km)
                VALUES (:restaurant_id,:latitude,:longitude,:radius_km)'''), area.model_dump() | {'restaurant_id':found})
        for hours in entry.opening_hours:
            await db.execute(text('''INSERT INTO restaurant_opening_hours VALUES
                (:restaurant_id,:weekday,:start_minute,:end_minute)'''), hours.model_dump() | {'restaurant_id':found})
    await db.commit()
    return len(entries)


async def eligible_restaurants(db, profile, days=7, now=None, require_verified=False):
    # City planning does not assert delivery coverage or opening hours.
    if not require_verified:
        rows = (await db.execute(text("""SELECT id FROM restaurants
            WHERE is_active AND available AND lower(city)=lower(:city) ORDER BY id"""),
            {'city': getattr(profile, 'city', None) or 'Skopje'})).scalars().all()
        return list(rows), 'city_only_unverified'

    if getattr(profile, 'latitude', None) is None or getattr(profile, 'longitude', None) is None:
        return [], 'location_required'
    now = now or datetime.now(timezone.utc)
    # Planning represents one delivery slot each day (three dishes may be ordered together).
    start = now.astimezone(ZoneInfo('Europe/Skopje')).date()
    minute = getattr(profile, 'delivery_minute', 780)
    timestamps = [datetime.combine(start + timedelta(days=d), datetime.min.time(),
                                  ZoneInfo('Europe/Skopje')) + timedelta(minutes=minute) for d in range(days)]
    rows = (await db.execute(text('''SELECT r.id FROM restaurants r
        WHERE r.is_active AND r.available AND lower(r.city)=lower(:city)
        AND r.service_verified_until >= :until
        AND EXISTS (SELECT 1 FROM restaurant_delivery_areas a WHERE a.restaurant_id=r.id
            AND 6371 * 2 * asin(sqrt(LEAST(1.0,
                power(sin(radians(a.latitude-:latitude)/2),2) + cos(radians(:latitude))*cos(radians(a.latitude))
                *power(sin(radians(a.longitude-:longitude)/2),2)))) <= a.radius_km)
        AND NOT EXISTS (SELECT 1 FROM unnest(CAST(:slots AS timestamptz[])) AS slots(slot)
            WHERE NOT EXISTS (SELECT 1 FROM restaurant_opening_hours h WHERE h.restaurant_id=r.id
                AND h.weekday=extract(isodow FROM slot AT TIME ZONE r.timezone)::integer-1
                AND h.start_minute<=extract(hour FROM slot AT TIME ZONE r.timezone)*60+extract(minute FROM slot AT TIME ZONE r.timezone)
                AND h.end_minute>extract(hour FROM slot AT TIME ZONE r.timezone)*60+extract(minute FROM slot AT TIME ZONE r.timezone)))
        ORDER BY r.id'''), {'city':profile.city, 'latitude':profile.latitude, 'longitude':profile.longitude,
                           'slots':timestamps, 'until':max(now, timestamps[-1])})).scalars().all()
    return list(rows), 'verified' if rows else 'no_verified_service'
