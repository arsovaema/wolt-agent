"""Hybrid pgvector/text, nutrition, budget and exploration retrieval for AI planning."""
import logging
from uuid import uuid4
from sqlalchemy import text
from service_area import eligible_restaurants
from meal_search import VERSION
import query_cache

logger = logging.getLogger(__name__)
RESTAURANT_LIMIT = 20
MEALS_PER_RESTAURANT = 18
CANDIDATE_LIMIT = RESTAURANT_LIMIT * MEALS_PER_RESTAURANT


def goal_queries(profile):
    # Describe intent, not a hand-written protein/ingredient taxonomy.
    goal = str(profile.goal)
    return [f'{goal}: varied, satisfying {occasion} meals with balanced nutrition'
            for occasion in ('breakfast', 'lunch', 'dinner')]


async def retrieve_candidates(db, client, profile, fee_allowance, previous_ids=()):
    allowance = profile.daily_budget_mkd - fee_allowance
    if allowance <= 0:
        return [], {'mode': 'budget_empty', 'candidate_count': 0}
    restaurant_ids, service_status = await eligible_restaurants(db, profile)
    if not restaurant_ids:
        await db.rollback()
        return [], {'mode': 'local_empty', 'candidate_count': 0, 'service_status': service_status}
    await db.rollback()
    vectors = []
    try:
        vectors = await query_cache.embed(db, client, goal_queries(profile))
    except Exception:
        logger.warning('Weekly embeddings unavailable; retaining text, nutrition and budget retrieval', exc_info=True)
        await db.rollback()
    params = {'restaurants': restaurant_ids, 'budget': allowance, 'previous': list(previous_ids),
              'seed': str(uuid4()), 'venues': RESTAURANT_LIMIT, 'per_venue': MEALS_PER_RESTAURANT,
              'calories': profile.daily_calorie_target / 3, 'protein': profile.daily_protein_target / 3,
              'carbs': profile.daily_carbs_target / 3, 'fat': profile.daily_fat_target / 3,
              'version': VERSION, 'query': ' OR '.join(dict.fromkeys(' '.join(goal_queries(profile)).split())),
              **{f'vector{i}': v for i,v in enumerate(vectors)}}
    # Exact local pgvector ranking keeps semantic recall within each eligible venue.
    # RRF fuses independent semantic and lexical ranks without comparing raw scores.
    semantic_sql = '''SELECT NULL::integer AS id, 0::float AS score WHERE false'''
    if vectors:
        values = ','.join(f'(CAST(:vector{i} AS vector))' for i in range(len(vectors)))
        semantic_sql = f'''SELECT n.id, 1.0/(60+n.position) AS score
            FROM (VALUES {values}) q(embedding) CROSS JOIN LATERAL (
                SELECT id, row_number() OVER (ORDER BY embedding <=> q.embedding,id) AS position
                FROM documents WHERE embedding IS NOT NULL AND embedding_version=:version
                ORDER BY embedding <=> q.embedding,id LIMIT 12) n'''
    await db.execute(text('SET LOCAL max_parallel_workers_per_gather = 0'))
    rows = (await db.execute(text(f'''WITH venues AS (
        SELECT id FROM unnest(CAST(:restaurants AS integer[])) r(id)
        ORDER BY md5(CAST(id AS text) || :seed) LIMIT :venues)
        SELECT m.id,m.restaurant_id,m.restaurant_name,m.item_name,m.description,m.category,
            m.price_mkd,m.estimated_calories AS calories,m.estimated_protein AS protein,
            m.estimated_carbs AS carbs,m.estimated_fat AS fat,m.image_url,
            m.nutrition_ranges,m.nutrition_confidence,m.nutrition_source,m.estimator_version,m.food_assessment,
            m.semantic_match,m.hybrid_score
        FROM venues r CROSS JOIN LATERAL (
            WITH available AS MATERIALIZED (
                SELECT m.*,
                    (m.id = ANY(CAST(:previous AS integer[]))) AS used,
                    md5(CAST(m.id AS text) || :seed) AS shuffle
                FROM menu_items m WHERE m.restaurant_id=r.id AND m.is_available
                    AND m.last_seen_at >= (now() AT TIME ZONE 'UTC') - interval '72 hours'
                    AND m.price_mkd > 0 AND m.price_mkd <= :budget
                    AND m.estimated_calories > 0 AND m.estimated_protein >= 0
                    AND m.estimated_carbs >= 0 AND m.estimated_fat >= 0
                    AND COALESCE(m.food_assessment->>'is_meal','true') <> 'false'),
            documents AS MATERIALIZED (
                SELECT a.id,d.embedding,d.embedding_version,d.search_text
                FROM available a JOIN meal_search_documents d ON d.menu_item_id=a.id),
            semantic AS ({semantic_sql}),
            lexical AS (
                SELECT id,1.0/(60+row_number() OVER (
                    ORDER BY ts_rank_cd(search_text,websearch_to_tsquery('simple',:query)) DESC,id)) AS score
                FROM documents WHERE search_text @@ websearch_to_tsquery('simple',:query)),
            fused AS (SELECT id,sum(score) AS score FROM (
                SELECT * FROM semantic UNION ALL SELECT * FROM lexical) ranked_matches GROUP BY id),
            ranked AS (
                SELECT a.*,COALESCE(f.score,0) AS hybrid_score,
                    EXISTS(SELECT 1 FROM semantic s WHERE s.id=a.id) AS semantic_match,
                    row_number() OVER (ORDER BY used,price_mkd,shuffle) AS affordable_rank,
                    row_number() OVER (ORDER BY used,
                        abs(estimated_calories-:calories)/:calories * 0.35 +
                        abs(estimated_protein-:protein)/:protein * 0.35 +
                        abs(estimated_carbs-:carbs)/:carbs * 0.15 +
                        abs(estimated_fat-:fat)/:fat * 0.15,shuffle) AS nutrition_rank,
                    row_number() OVER (ORDER BY used,COALESCE(f.score,0) DESC,shuffle) AS hybrid_rank,
                    row_number() OVER (PARTITION BY COALESCE(NULLIF(food_assessment->>'dish_format',''),category,'')
                        ORDER BY used,shuffle) AS category_rank
                FROM available a LEFT JOIN fused f ON f.id=a.id)
            SELECT * FROM ranked ORDER BY
                CASE WHEN affordable_rank<=4 OR nutrition_rank<=4 OR (hybrid_score>0 AND hybrid_rank<=4) THEN 0 ELSE 1 END,
                used,category_rank,shuffle LIMIT :per_venue
        ) m'''), params)).mappings().all()
    await db.rollback()
    candidates = [dict(row) for row in rows]
    for meal in candidates:
        meal['hybrid_score'] = float(meal['hybrid_score'])
    semantic_count = sum(meal['semantic_match'] for meal in candidates)
    return candidates, {
        'mode': 'hybrid' if semantic_count else 'nutrition_fallback',
        'semantic_candidates': semantic_count, 'query_embeddings': len(vectors),
        'candidate_count': len(rows), 'candidate_limit': CANDIDATE_LIMIT,
        'eligible_restaurants': len(restaurant_ids), 'city': getattr(profile, 'city', 'Skopje'),
        'service_status': service_status, 'meals_per_restaurant': MEALS_PER_RESTAURANT,
        'vector_strategy': 'exact_local', 'fusion': 'reciprocal_rank'}
