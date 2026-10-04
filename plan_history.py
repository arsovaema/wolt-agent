"""Bounded per-user selection history stored with the weekly plan state."""
from collections import Counter


def prior_weeks(previous):
    history = list(previous.get('selection_history', []))
    if previous.get('status') in ('ready', 'poor_fit') and previous.get('weekly_plan'):
        history.append([[{key: meal[key] for key in ('id','restaurant_name','item_name','food_assessment') if key in meal}
                         for meal in day.get('meals', [])] for day in previous['weekly_plan']])
    return history[-3:]


def usage_from_history(history):
    usage = Counter()
    combos = set()
    ids = set()
    # Recent weeks cost more than older weeks; counts are never clamped to one.
    for weight, week in enumerate(history, 1):
        for day in week:
            keys = frozenset((m['restaurant_name'], m['item_name']) for m in day)
            combos.add(keys)
            for key in keys:
                usage[key] += weight
            ids.update(m['id'] for m in day if 'id' in m)
    return dict(usage), combos, sorted(ids)
