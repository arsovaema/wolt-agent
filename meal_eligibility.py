"""Model-owned food judgments; ingestion only checks that an item has a name."""
from typing import Literal
from pydantic import BaseModel, Field


class FoodAssessment(BaseModel):
    is_meal: bool = Field(description="Can this entire item serve as a meal, rather than a standalone side dish, snack, dessert, beverage, condiment, extra, or non-meal item? Do not promote a side into a meal to satisfy a plan. Use all menu evidence, not keywords.")
    reason: str = Field(description="Brief evidence for the eligibility judgment; acknowledge missing evidence.")
    dish_identity: str = Field(description="Canonical English dish identity. Equivalent sizes or drink bundles share identity; distinct recipes do not.")
    dish_format: str = Field(description="Specific culinary format in canonical English, inferred from the main dish, or empty if unknown. A bundle keeps its main dish format. Do not use generic labels such as meal or food. No fixed taxonomy.")
    protein_sources: list[str] = Field(description="Main protein-bearing ingredients in canonical English, supported by name and description; omit incidental protein from bread, starches or garnishes; empty if unknown. No fixed taxonomy.")
    preparation: str = Field(description="Main preparation style in canonical English, or empty if unknown.")
    confidence: Literal['high', 'medium', 'low']


def is_meal_candidate(item_name, category=None, assessment=None):
    """Unknown items remain candidates for AI review, never keyword exclusion."""
    if not isinstance(item_name, str) or not item_name.strip():
        return False
    if assessment:
        return FoodAssessment.model_validate(assessment).is_meal
    return True
