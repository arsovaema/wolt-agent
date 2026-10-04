"""Run preregistered arms, saving raw predictions; never read reference nutrition."""
import argparse
import asyncio
import base64
import importlib.metadata
import json
import math
import os
import platform
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
os.environ['HF_HOME'] = str(ROOT / '.hf-cache')
os.environ['TORCH_HOME'] = str(ROOT / '.torch-cache')
os.environ['HF_HUB_OFFLINE'] = '1'
os.environ['TRANSFORMERS_OFFLINE'] = '1'
os.environ['TOKENIZERS_PARALLELISM'] = 'false'
os.environ['PYTORCH_ENABLE_MPS_FALLBACK'] = '1'

KEYS = ('calories', 'protein', 'carbs', 'fat')


def normalize_output(raw):
    """Accept the documented model schema and the app schema, never repair values."""
    text = raw.strip()
    if text.startswith('```'):
        lines = text.splitlines()
        text = '\n'.join(lines[1:-1]) if lines[-1].strip() == '```' else text
    data = json.loads(text)
    for key in ('nutritional_summary', 'nutritional_profile'):
        if key in data:
            data = data[key]
            break
    aliases = {'calories': ('calories', 'calories_kcal'),
               'protein': ('protein', 'protein_grams', 'protein_g'),
               'carbs': ('carbs', 'carbs_grams', 'carbohydrate_g', 'carbohydrates_g'),
               'fat': ('fat', 'fat_grams', 'fat_g')}
    prediction = {}
    for key, options in aliases.items():
        value = next((data[k] for k in options if k in data), None)
        if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or value < 0:
            raise ValueError(f'Missing or invalid {key}')
        prediction[key] = float(value)
    if prediction['calories'] <= 0:
        raise ValueError('Nonpositive calories')
    return prediction


def load_inputs(smoke=False):
    # Deliberately project only inputs. Ground truth never reaches inference code.
    manifest = json.loads((ROOT / 'data/manifest.json').read_text())
    samples = [manifest['smoke']] if smoke else manifest['samples']
    return [{key: dish[key] for key in ('dish_id', 'ingredients', 'image_path')} for dish in samples]


def output_file(provider, smoke):
    folder = ROOT / 'results'
    folder.mkdir(exist_ok=True)
    return folder / (f'{provider}_smoke.jsonl' if smoke else f'{provider}.jsonl')


def existing_keys(path):
    if not path.exists():
        return set()
    return {(row['dish_id'], row['arm']) for row in map(json.loads, path.read_text().splitlines())}


def save(path, record):
    with path.open('a') as handle:
        handle.write(json.dumps(record, allow_nan=False) + '\n')
    print(record['arm'], record['dish_id'], record['status'], f"{record['seconds']:.1f}s", flush=True)


def text_prompt(ingredients):
    return f'''Estimate the approximate nutritional values of this restaurant meal.

Restaurant:
Restaurant

Category:
Meal

Meal:
Mixed meal

Description:
Ingredients: {', '.join(ingredients)}.

Estimate the nutritional values for the entire menu item:
- calories
- protein in grams
- carbohydrates in grams
- fat in grams

Use the meal name, size information and listed ingredients
to make a reasonable estimate.
'''


async def run_openai(smoke, retry_transient=False):
    from dotenv import load_dotenv
    from openai import AsyncOpenAI
    from pydantic import BaseModel, Field
    load_dotenv(ROOT.parent.parent / '.env')

    class MacroNutrients(BaseModel):
        calories: int = Field(description='Estimated total calories for the dish')
        protein_grams: float = Field(description='Estimated grams of protein')
        carbs_grams: float = Field(description='Estimated grams of carbohydrates')
        fat_grams: float = Field(description='Estimated grams of fat')

    path = output_file('openai', smoke)
    done = existing_keys(path)
    previous = {}
    if path.exists():
        for row in map(json.loads, path.read_text().splitlines()):
            previous[(row['dish_id'], row['arm'])] = row
    transient = {'RateLimitError', 'APIConnectionError', 'APITimeoutError', 'InternalServerError'}
    if retry_transient:
        done = {key for key, row in previous.items()
                if row['status'] == 'ok' or row.get('error_type') not in transient}
    limiter = asyncio.Semaphore(1 if retry_transient else 3)
    async with AsyncOpenAI(max_retries=2, timeout=60) as client:
        async def infer(dish, arm):
            if (dish['dish_id'], arm) in done:
                return
            async with limiter:
                if retry_transient:
                    await asyncio.sleep(10)
                prompt = text_prompt(dish['ingredients'])
                content = [{'type': 'text', 'text': prompt}]
                if arm == 'openai_image_text':
                    encoded = base64.b64encode((ROOT / dish['image_path']).read_bytes()).decode()
                    content.append({'type': 'image_url', 'image_url': {
                        'url': 'data:image/png;base64,' + encoded, 'detail': 'high'}})
                started = time.monotonic()
                record = {'dish_id': dish['dish_id'], 'arm': arm, 'prompt': prompt,
                          'model_requested': 'gpt-4o-mini', 'temperature': 0,
                          'retry_of_transient_failure': retry_transient,
                          'started_at': datetime.now(timezone.utc).isoformat()}
                try:
                    response = await client.beta.chat.completions.parse(
                        model='gpt-4o-mini', temperature=0, max_tokens=512,
                        messages=[{'role': 'system', 'content': 'You estimate approximate nutritional values for restaurant meals.'},
                                  {'role': 'user', 'content': content}], response_format=MacroNutrients)
                    raw = response.choices[0].message.content or ''
                    record.update(raw=raw, model_returned=response.model,
                                  usage=response.usage.model_dump() if response.usage else None,
                                  finish_reason=response.choices[0].finish_reason)
                    record.update(prediction=normalize_output(raw), status='ok')
                except Exception as error:
                    # Never log request headers, keys, or the full client configuration.
                    record.update(status='error', error_type=type(error).__name__,
                                  error=str(error)[:500])
                record['seconds'] = time.monotonic() - started
                save(path, record)
        await asyncio.gather(*(infer(dish, arm) for dish in load_inputs(smoke)
                               for arm in ('openai_text', 'openai_image_text')))


def run_hf(smoke, device_arg):
    import torch
    from PIL import Image
    from transformers import AutoProcessor, Qwen3VLForConditionalGeneration
    from peft import PeftModel

    path = output_file('hf', smoke)
    done = existing_keys(path)
    torch.set_num_threads(4)
    torch.manual_seed(20260918)
    device = device_arg or ('mps' if torch.backends.mps.is_available() else 'cpu')
    dtype = torch.float16 if device == 'mps' else torch.float32
    print('Loading food adapter on', device, str(dtype), flush=True)
    started = time.monotonic()
    model = Qwen3VLForConditionalGeneration.from_pretrained(
        ROOT / 'models/base', dtype=dtype, attn_implementation='eager',
        local_files_only=True, trust_remote_code=False,
    )
    model = PeftModel.from_pretrained(model, ROOT / 'models/adapter',
                                     local_files_only=True, is_trainable=False)
    model = model.to(device=device, dtype=dtype).eval()
    processor = AutoProcessor.from_pretrained(ROOT / 'models/base', local_files_only=True, trust_remote_code=False)
    print('Loaded in', round(time.monotonic() - started, 1), 'seconds', flush=True)
    environment = {'device': device, 'dtype': str(dtype), 'platform': platform.platform(),
                   'model_revisions': json.loads((ROOT / 'models/revisions.json').read_text()),
                   'versions': {name: importlib.metadata.version(name) for name in ('torch', 'transformers', 'peft', 'Pillow')},
                   'generation': {'do_sample': False, 'max_new_tokens': 512},
                   'image_max_size': 768, 'attention': 'eager'}
    (ROOT / 'results/environment.json').write_text(json.dumps(environment, indent=2))
    for dish in load_inputs(smoke):
        for arm in ('hf_image', 'hf_image_text'):
            if (dish['dish_id'], arm) in done:
                continue
            prompt = ('As a food-analyzer AI, analyze the image and return a single JSON object containing nutritional information. '
                      'Estimate nutrition for the entire depicted serving. Respond with JSON only. No extra text.')
            if arm == 'hf_image_text':
                prompt += '\nThe menu lists these ingredients: ' + ', '.join(dish['ingredients']) + '. Use this ingredient information together with the image.'
            started = time.monotonic()
            record = {'dish_id': dish['dish_id'], 'arm': arm, 'prompt': prompt,
                      'started_at': datetime.now(timezone.utc).isoformat()}
            try:
                with Image.open(ROOT / dish['image_path']) as source:
                    image = source.convert('RGB')
                image.thumbnail((768, 768), Image.Resampling.LANCZOS)
                messages = [{'role': 'user', 'content': [{'type': 'image', 'image': image}, {'type': 'text', 'text': prompt}]}]
                text = processor.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
                inputs = processor(text=[text], images=[image], return_tensors='pt', padding=True).to(device)
                if 'pixel_values' in inputs:
                    inputs['pixel_values'] = inputs['pixel_values'].to(dtype=dtype)
                with torch.inference_mode():
                    outputs = model.generate(**inputs, max_new_tokens=512, do_sample=False,
                                             use_cache=True, pad_token_id=processor.tokenizer.eos_token_id)
                generated = outputs[0, inputs['input_ids'].shape[-1]:]
                raw = processor.decode(generated, skip_special_tokens=True)
                record.update(raw=raw, generated_tokens=len(generated))
                record.update(prediction=normalize_output(raw), status='ok')
                del inputs, outputs, generated
            except Exception as error:
                record.update(status='error', error_type=type(error).__name__, error=str(error)[:500])
            record['seconds'] = time.monotonic() - started
            save(path, record)
            if device == 'mps':
                torch.mps.empty_cache()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('provider', choices=['openai', 'hf'])
    parser.add_argument('--smoke', action='store_true')
    parser.add_argument('--device', choices=['mps', 'cpu'])
    parser.add_argument('--retry-transient', action='store_true')
    args = parser.parse_args()
    if args.provider == 'openai':
        asyncio.run(run_openai(args.smoke, args.retry_transient))
    else:
        run_hf(args.smoke, args.device)


if __name__ == '__main__':
    main()
