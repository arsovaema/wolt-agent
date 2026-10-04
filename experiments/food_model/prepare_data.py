"""Download a reproducible, small Nutrition5k test sample; no model calls."""
import csv
import hashlib
import io
import json
import random
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent
DATA = ROOT / 'data'
BASE = 'https://storage.googleapis.com/nutrition5k_dataset/nutrition5k_dataset/'
SEED = 20260918


def main():
    DATA.mkdir(exist_ok=True)
    (DATA / 'images').mkdir(exist_ok=True)
    sources = {}
    with httpx.Client(follow_redirects=True, timeout=60) as client:
        def get(path):
            target = DATA / Path(path).name
            if not target.exists():
                r = client.get(BASE + path)
                r.raise_for_status()
                target.write_bytes(r.content)
            content = target.read_bytes()
            sources[path] = {'url': BASE + path, 'sha256': hashlib.sha256(content).hexdigest()}
            return content.decode()

        test_ids = set(get('dish_ids/splits/rgb_test_ids.txt').split())
        train_ids = set(get('dish_ids/splits/rgb_train_ids.txt').split())
        dishes = {}
        for name in ('dish_metadata_cafe1.csv', 'dish_metadata_cafe2.csv'):
            for row in csv.reader(io.StringIO(get('metadata/' + name))):
                if not row or not row[0].startswith('dish_'):
                    continue
                assert row[6].startswith('ingr_'), row[:7]
                assert (len(row) - 6) % 7 == 0, (row[0], len(row))
                nutrients = dict(zip(('calories', 'mass_g', 'fat', 'carbs', 'protein'), map(float, row[1:6])))
                ingredients = sorted({row[i + 1].strip() for i in range(6, len(row), 7)})
                dishes[row[0]] = {'dish_id': row[0], 'ingredients': ingredients,
                                 'reference': nutrients, 'image_path': f'data/images/{row[0]}.png'}
        print('Metadata dishes:', len(dishes), 'test IDs:', len(test_ids), flush=True)
        skipped = []
        def fetch_image(dish):
            image_url = BASE + f'imagery/realsense_overhead/{dish["dish_id"]}/rgb.png'
            path = ROOT / dish['image_path']
            if not path.exists():
                r = client.get(image_url)
                if r.status_code == 404:
                    skipped.append({'dish_id': dish['dish_id'], 'reason': 'overhead image unavailable'})
                    return False
                r.raise_for_status()
                if not r.content.startswith(b'\x89PNG'):
                    raise ValueError('Unexpected image content: ' + dish['dish_id'])
                path.write_bytes(r.content)
            dish['image_url'] = image_url
            dish['image_sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
            return True

        rng = random.Random(SEED)
        selected = []
        for lower, upper in ((50, 350), (350, 650), (650, 1500)):
            candidates = [dishes[key].copy() for key in sorted(test_ids & dishes.keys())
                          if lower <= dishes[key]['reference']['calories'] < upper
                          and all(value >= 0 for value in dishes[key]['reference'].values())
                          and dishes[key]['reference']['protein'] > 0]
            rng.shuffle(candidates)
            count = 0
            for dish in candidates:
                if fetch_image(dish):
                    dish['stratum_kcal'] = [lower, upper]
                    selected.append(dish)
                    count += 1
                    print('Selected', len(selected), dish['dish_id'], flush=True)
                if count == 10:
                    break
            if count != 10:
                raise RuntimeError('Not enough images in stratum')
        smoke = None
        for key in sorted((train_ids - test_ids) & dishes.keys()):
            if 200 <= dishes[key]['reference']['calories'] < 800:
                candidate = dishes[key].copy()
                if fetch_image(candidate):
                    smoke = candidate
                    break
        if smoke is None:
            raise RuntimeError('No smoke image')
        manifest = {'seed': SEED, 'dataset': 'Nutrition5k', 'sources': sources,
                    'skipped': skipped, 'smoke': smoke, 'samples': selected}
        (DATA / 'manifest.json').write_text(json.dumps(manifest, indent=2))
        print('Saved 30 evaluation images and one separate smoke image.', flush=True)


if __name__ == '__main__':
    main()
