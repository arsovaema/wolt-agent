"""Download only model weights/config/tokenizers, pinned to recorded revisions."""
import json
import os
from pathlib import Path
ROOT = Path(__file__).resolve().parent
os.environ['HF_HOME'] = str(ROOT / '.hf-cache')
os.environ['HF_HUB_DISABLE_XET'] = '1'
from huggingface_hub import HfApi, snapshot_download

api = HfApi()
records = {}
for name, repo in [('base', 'Qwen/Qwen3-VL-2B-Instruct'), ('adapter', 'Ateeqq/food-analysis')]:
    info = api.model_info(repo)
    target = ROOT / 'models' / name
    print('Downloading', repo, info.sha, flush=True)
    snapshot_download(repo, revision=info.sha, local_dir=target,
                      allow_patterns=['*.json', '*.safetensors', '*.txt', '*.jinja', 'README.md'],
                      max_workers=2)
    records[name] = {'repo': repo, 'revision': info.sha, 'path': str(target.relative_to(ROOT))}
(ROOT / 'models' / 'revisions.json').write_text(json.dumps(records, indent=2))
print('Model downloads complete.', flush=True)
