"""Diversity reporting from model judgments, without food-name dictionaries."""
import unicodedata
from itertools import combinations
from collections import Counter


def normalized_name(value):
    return ' '.join(unicodedata.normalize('NFKC', value).casefold().split())


def dish_identity(meal):
    assessment = meal.get('food_assessment') or {}
    return normalized_name(assessment.get('dish_identity') or meal['item_name'])


def features(meal):
    assessment = meal.get('food_assessment') or {}
    form = normalized_name(assessment.get('dish_format') or '')
    proteins = frozenset(normalized_name(p) for p in assessment.get('protein_sources', []) if p.strip())
    return frozenset([form]) if form else frozenset(), proteins


def combination_diversity(meals):
    identities = [dish_identity(meal) for meal in meals]
    signatures = [features(meal) for meal in meals]
    format_counts = Counter(kind for formats, _ in signatures for kind in formats)
    penalties = [.65 * bool(a[0] & b[0]) + .35 * bool(a[1] & b[1])
                 for a, b in combinations(signatures, 2)]
    return {'obvious_duplicate': len(set(identities)) != len(identities),
            'excessive_format_repetition': any(count > 2 for count in format_counts.values()),
            'similarity_penalty': round(sum(penalties) / len(penalties), 4) if penalties else 0.0}
