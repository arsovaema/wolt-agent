"""Plan validation/reporting plus offline scoring helpers for benchmarks.

Production plan selection is owned by ai_meal_planner, not these scorers.
"""
from itertools import combinations
from math import isfinite
from meal_eligibility import is_meal_candidate
from meal_diversity import combination_diversity

MIN_WEEKLY_RESTAURANTS = 7
MAX_DAYS_PER_RESTAURANT = 1

MACROS = ("calories", "protein", "carbs", "fat")
# Product matching tolerances, not a clinical assessment of a meal plan.
FIT_TOLERANCES = {"calories": 0.15, "protein": 0.15, "carbs": 0.20, "fat": 0.20}


def nutrition_targets(profile):
    targets = {name: getattr(profile, f"daily_{'calorie' if name == 'calories' else name}_target") for name in MACROS}
    if any(not isfinite(value) or value <= 0 for value in targets.values()):
        raise ValueError("Nutrition targets must be finite and positive.")
    return targets


def estimated_cost(meals, daily_fee_allowance_mkd):
    if daily_fee_allowance_mkd is None or daily_fee_allowance_mkd < 0:
        raise ValueError("A nonnegative ordering-fee estimate is required.")
    food = sum(meal["price_mkd"] for meal in meals)
    orders = len({meal["restaurant_name"] for meal in meals})
    fees = daily_fee_allowance_mkd if meals else 0
    return {"food_subtotal_mkd": food, "daily_fee_allowance_mkd": fees,
            "estimated_total_mkd": food + fees, "restaurant_count": orders}


def uncertainty_penalty(meals):
    """Penalize wide nutrition ranges while preserving central-target scoring."""
    relative_widths = []
    for meal in meals:
        ranges = meal.get("nutrition_ranges") or {}
        for key in MACROS:
            central = meal.get(key, 0)
            bounds = ranges.get(key) or {}
            low, high = bounds.get("low"), bounds.get("high")
            if central and low is not None and high is not None and high >= low:
                relative_widths.append((high - low) / (2 * central))
    if not relative_widths:
        return 0.0
    # A 100% total range width across nutrients costs up to one score point.
    return min(2.0, sum(relative_widths) / len(relative_widths))


def calculate_daily_plan_score(
    meals: list[dict],
    profile,
) -> float:
    total_calories = sum(
        meal["calories"]
        for meal in meals
    )

    total_protein = sum(
        meal["protein"]
        for meal in meals
    )

    total_carbs = sum(
        meal["carbs"]
        for meal in meals
    )

    total_fat = sum(
        meal["fat"]
        for meal in meals
    )

    calorie_difference = (
        abs(total_calories - profile.daily_calorie_target)
        / profile.daily_calorie_target
    )

    protein_difference = (
        abs(total_protein - profile.daily_protein_target)
        / profile.daily_protein_target
    )

    carbs_difference = (
        abs(total_carbs - profile.daily_carbs_target)
        / profile.daily_carbs_target
    )

    fat_difference = (
        abs(total_fat - profile.daily_fat_target)
        / profile.daily_fat_target
    )

    weighted_difference = (
        calorie_difference * 0.35
        + protein_difference * 0.35
        + carbs_difference * 0.15
        + fat_difference * 0.15
    )

    largest_macro_difference = max(
        calorie_difference,
        protein_difference,
        carbs_difference,
        fat_difference,
    )

    score = (
        10
        - weighted_difference * 7
        - largest_macro_difference * 3
    )

    score -= uncertainty_penalty(meals)

    # -----------------------------------
    # Extra penalties for large deviations
    # -----------------------------------

    # More than 15% away from calorie target
    if calorie_difference > 0.15:
        score -= 1.0

    # More than 15% away from protein target
    if protein_difference > 0.15:
        score -= 1.0

    # More than 20% away from carb target
    if carbs_difference > 0.20:
        score -= 0.5

    # More than 20% away from fat target
    if fat_difference > 0.20:
        score -= 1.0

    return max(
        1.0,
        min(10.0, round(score, 2)),
    )



def select_best_daily_meals(meals, profile, number_of_meals=3, *, daily_fee_allowance_mkd, meal_usage=None, used_combinations=None):
    """Enforce budget/eligibility/duplicate rules, then rank nutrition and variety.

    Exactly three meals is the current product contract. Enumeration avoids
    pruning complementary meals and keeps fee accounting correct across venues.
    """
    nutrition_targets(profile)
    estimated_cost([], daily_fee_allowance_mkd)
    if number_of_meals != 3:
        raise ValueError("The daily planner currently supports exactly three meals.")
    unique = {}
    for meal in meals:
        if not is_meal_candidate(meal['item_name'], meal.get('category'), meal.get('food_assessment')):
            continue
        price = meal["price_mkd"]
        if price is None or price <= 0 or price > profile.daily_budget_mkd:
            continue
        if any(not isfinite(meal[key]) or meal[key] < 0 for key in MACROS):
            continue
        if meal["calories"] <= 0:
            continue
        key = (meal["restaurant_name"], meal["item_name"])
        unique.setdefault(key, meal)
    best_plan, best_score, best_rank = [], -1.0, None
    targets = nutrition_targets(profile)
    for plan in combinations(unique.values(), number_of_meals):
        cost = sum(meal["price_mkd"] for meal in plan) + daily_fee_allowance_mkd
        if cost > profile.daily_budget_mkd:
            continue
        diversity = combination_diversity(plan)
        if diversity["obvious_duplicate"] or diversity["excessive_format_repetition"]:
            continue
        score = calculate_daily_plan_score(plan, profile)
        fits = all(abs(sum(meal[key] for meal in plan) - targets[key]) / targets[key]
                   <= FIT_TOLERANCES[key] + 1e-9 for key in MACROS)
        keys = frozenset((meal['restaurant_name'], meal['item_name']) for meal in plan)
        # Preserve nutrition tolerance and one-point quality bands. Within
        # comparable plans, prefer new combinations and less recently used meals.
        reuse = sum((meal_usage or {}).get(key, 0) for key in keys)
        adjusted = score - .9 * diversity['similarity_penalty']
        rank = (fits, int(score), keys not in (used_combinations or set()), -reuse, adjusted, -cost)
        if best_rank is None or rank > best_rank:
            best_plan, best_score, best_rank = list(plan), score, rank
    return best_plan, best_score


def summarize_plan(meals, profile, daily_fee_allowance_mkd):
    targets = nutrition_targets(profile)
    totals = {key: round(sum(meal[key] for meal in meals), 2) for key in MACROS}
    differences = {key: round(totals[key] - targets[key], 2) for key in MACROS}
    outside = [key for key in MACROS if abs(totals[key] - targets[key]) / targets[key] > FIT_TOLERANCES[key] + 1e-9]
    status = "poor_fit" if outside else "ready"
    costs = estimated_cost(meals, daily_fee_allowance_mkd)
    return {
        "status": status,
        "message": ("Affordable plan with nutrition trade-offs; outside the configured matching tolerances for " + ", ".join(outside) + "."
                    if outside else "Daily plan matches the configured nutrition tolerances and estimated budget."),
        "meals": meals,
        "daily_plan_score": calculate_daily_plan_score(meals, profile),
        "diversity": combination_diversity(meals),
        "nutrition": {"estimated_totals": totals, "targets": targets,
                      "differences_from_targets": differences, "outside_tolerance": outside,
                      "relative_matching_tolerances": FIT_TOLERANCES},
        "budget": {**costs, "daily_limit_mkd": profile.daily_budget_mkd,
                   "estimated_remaining_mkd": profile.daily_budget_mkd - costs["estimated_total_mkd"],
                   "is_estimate": True,
                   "assumption": "The user-entered daily allowance reserves money for all delivery, service fees and required extras. Actual fees are not scraped; verify the combined checkout totals."},
        "nutrition_is_estimated": True,
        "uncertainty": {
            "average_relative_range_width": round(
                uncertainty_penalty(meals), 3
            ),
            "ranking_effect": "Wider nutrition ranges reduce the plan score; central estimates still determine target matching.",
        },
    }


def restaurant_key(meal):
    return ('id', meal['restaurant_id']) if meal.get('restaurant_id') is not None else ('name', meal['restaurant_name'])


def select_weekly_restaurant_day(candidates, profile, fee_allowance, meal_usage, used_combinations, restaurant_usage, previous_restaurant=None):
    """One venue per day; prioritize nutrition fit before variety."""
    groups = {}
    for meal in candidates:
        groups.setdefault(restaurant_key(meal), []).append(meal)
    best, best_key, best_rank = [], None, None
    for key, pool in groups.items():
        if restaurant_usage.get(key, 0) >= MAX_DAYS_PER_RESTAURANT:
            continue
        if len(restaurant_usage) < MIN_WEEKLY_RESTAURANTS and key in restaurant_usage:
            continue
        if key == previous_restaurant:
            continue
        selected, score = select_best_daily_meals(pool, profile, daily_fee_allowance_mkd=fee_allowance,
            meal_usage=meal_usage, used_combinations=used_combinations)
        if not selected:
            continue
        combination = frozenset((meal['restaurant_name'], meal['item_name']) for meal in selected)
        summary = summarize_plan(selected, profile, fee_allowance)
        rank = (summary['status'] == 'ready', int(score), -restaurant_usage.get(key, 0), key != previous_restaurant,
                combination not in used_combinations,
                -sum(meal_usage.get(item, 0) for item in combination), score,
                -summary['budget']['estimated_total_mkd'])
        if best_rank is None or rank > best_rank:
            best, best_key, best_rank = selected, key, rank
    return best, best_key
