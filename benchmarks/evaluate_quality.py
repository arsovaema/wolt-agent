"""Evaluate real, human-labeled queries against the current local catalog.

This opt-in command makes query embedding calls. Labels must refer to menu IDs in
this installation and be independently reviewed; no synthetic relevance labels.
"""
import argparse
import asyncio
import json
import math
from pathlib import Path
import statistics
import sys
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from pydantic import BaseModel, Field


class Case(BaseModel):
    case_id: str
    profile_id: int = Field(gt=0)
    query: str = Field(min_length=1,max_length=500)
    relevant_meal_ids: list[int] = Field(min_length=1)
    limit: int = Field(default=10,ge=1,le=20)


async def run(path,output):
    import main
    from service_area import eligible_restaurants
    from meal_search import SearchRequest,search
    from benchmarks.scale import percentiles
    cases=[Case.model_validate(row) for row in json.loads(Path(path).read_text())]
    results=[]
    try:
        for case in cases:
            async with main.async_session() as db:
                profile=await db.get(main.UserProfile,case.profile_id)
                if profile is None:
                    raise ValueError(f'Missing profile for {case.case_id}')
                restaurants,status=await eligible_restaurants(db,profile,days=1)
                if not restaurants:
                    raise ValueError(f'{case.case_id} has no verified service: {status}')
                begin=time.perf_counter()
                result=await search(db,main.openai_client,SearchRequest(query=case.query,limit=case.limit,generate_answer=False),restaurants)
                duration=time.perf_counter()-begin
            ids=[meal['id'] for meal in result['meals']]
            relevant=set(case.relevant_meal_ids)
            hits=len(set(ids)&relevant)
            dcg=sum(1/math.log2(i+2) for i,mid in enumerate(ids) if mid in relevant)
            ideal=sum(1/math.log2(i+2) for i in range(min(case.limit,len(relevant))))
            results.append({'case_id':case.case_id,'recall':hits/len(relevant),
                'precision':hits/len(ids) if ids else 0,'ndcg':dcg/ideal,'seconds':duration})
    finally:
        await main.engine.dispose()
        await main.openai_client.close()
    report={'cases':len(results),'label_source':str(path),'results':results,
        'mean_recall':statistics.mean(r['recall'] for r in results) if results else None,
        'mean_ndcg':statistics.mean(r['ndcg'] for r in results) if results else None,
        'latency':percentiles([r['seconds'] for r in results])}
    Path(output).write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report,indent=2))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cases',required=True)
    parser.add_argument('--output',default='benchmarks/labeled-quality.json')
    args=parser.parse_args()
    asyncio.run(run(args.cases,args.output))
