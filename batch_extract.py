#!/usr/bin/env python3
"""
Batch extract LLM features for all items using parallel requests.
Run with: DASHSCOPE_API_KEY=xxx python batch_extract.py
"""

import os
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from openai import OpenAI
from database import get_connection
from tqdm import tqdm

# Config - Alibaba Cloud DashScope
MODEL = "qwen3-vl-8b-instruct"
BASE_URL = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
MAX_WORKERS = 5   # Parallel requests - reduced to avoid rate limits
SAVE_EVERY = 10   # Save results every N items

STYLE_THESIS = """
Identity: "Duality Dancer"

Qualities I want my outfit to embody:
- Shrouded: coverage, drape, layers that conceal and reveal
- Precise: clean construction, intentional details, nothing sloppy
- Mysterious: depth, intrigue, not immediately readable
- Composed: put-together, harmonious, not chaotic
- Graceful: flow, elegance, ease of movement

Aesthetic references: Japanese minimalism, architectural fashion,
monochromatic earth tones, quality over quantity.
"""

PROMPT = """You are a fashion analyst. Evaluate this clothing item.

## User's Style Thesis:
{thesis}

## Item:
Title: {title}
Price: ¥{price}

Look at the image. Output ONLY valid JSON (no markdown, no explanation):
{{"attributes":{{"colors":[],"fit":"","material":"","style":""}},"thesis_alignment":{{"shrouded":0.0,"precise":0.0,"mysterious":0.0,"composed":0.0,"graceful":0.0}},"overall_fit":"poor/acceptable/good","reasoning":""}}"""


def get_client():
    """Create a new client per thread."""
    return OpenAI(
        base_url=BASE_URL,
        api_key=os.environ["DASHSCOPE_API_KEY"]
    )


def extract_features(item: dict, max_retries: int = 3) -> dict:
    """Extract features for a single item. Thread-safe with retry."""
    client = get_client()

    prompt = PROMPT.format(
        thesis=STYLE_THESIS,
        title=item["title"],
        price=item["price"] or 0
    )

    last_error = None
    for attempt in range(max_retries):
        try:
            # Backoff delay: 0.3s, 1s, 2s
            time.sleep(0.3 + attempt * 0.7)

            completion = client.chat.completions.create(
                model=MODEL,
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": item["image_url"]}},
                        {"type": "text", "text": prompt}
                    ]
                }],
                temperature=0.3,
                max_tokens=500
            )

            content = completion.choices[0].message.content

            # Parse JSON
            if "```json" in content:
                content = content.split("```json")[1].split("```")[0]
            elif "```" in content:
                content = content.split("```")[1].split("```")[0]

            features = json.loads(content.strip())
            features["item_id"] = item["id"]
            features["success"] = True
            return features

        except json.JSONDecodeError as e:
            # Don't retry JSON errors - the response was bad
            return {
                "item_id": item["id"],
                "success": False,
                "error": f"JSON parse error: {e}",
                "raw": content if 'content' in locals() else None
            }
        except Exception as e:
            last_error = str(e)
            # Retry on other errors (rate limit, network, etc)
            if attempt < max_retries - 1:
                continue

    # All retries failed
    return {
        "item_id": item["id"],
        "success": False,
        "error": last_error
    }


def get_items_without_features(limit: int = 100) -> list:
    """Get items that don't have extracted features yet."""
    conn = get_connection()
    cursor = conn.cursor()

    # For now, just get items with images
    # Later we'll add a features table and filter by that
    cursor.execute("""
        SELECT id, title, price, image_url
        FROM items
        WHERE image_url IS NOT NULL
        ORDER BY id
        LIMIT ?
    """, (limit,))

    items = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return items


def batch_extract(limit: int = 100, max_workers: int = MAX_WORKERS):
    """Extract features for multiple items in parallel."""

    if not os.environ.get("DASHSCOPE_API_KEY"):
        print("ERROR: Set DASHSCOPE_API_KEY environment variable")
        return

    items = get_items_without_features(limit)
    print(f"Processing {len(items)} items with {max_workers} workers...")

    results = []
    errors = []

    start_time = time.time()

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        # Submit all tasks
        future_to_item = {
            executor.submit(extract_features, item): item
            for item in items
        }

        # Process as they complete
        with tqdm(total=len(items), desc="Extracting") as pbar:
            for future in as_completed(future_to_item):
                item = future_to_item[future]
                try:
                    result = future.result()
                    if result.get("success"):
                        results.append(result)
                    else:
                        errors.append(result)
                except Exception as e:
                    errors.append({
                        "item_id": item["id"],
                        "error": str(e)
                    })
                pbar.update(1)

                # Save intermediate results
                if len(results) % SAVE_EVERY == 0 and results:
                    save_results(results, "features_partial.json")

    elapsed = time.time() - start_time

    # Final save
    save_results(results, "features_extracted.json")
    if errors:
        save_results(errors, "features_errors.json")

    print(f"\n{'='*50}")
    print(f"Completed in {elapsed:.1f}s ({len(items)/elapsed:.1f} items/sec)")
    print(f"Success: {len(results)}")
    print(f"Errors: {len(errors)}")
    print(f"Results saved to features_extracted.json")

    return results, errors


def save_results(data: list, filename: str):
    """Save results to JSON file."""
    with open(filename, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def show_sample_results(results: list, n: int = 3):
    """Show a few sample results."""
    print(f"\nSample results:")
    for r in results[:n]:
        print(f"\n--- Item {r.get('item_id')} ---")
        print(json.dumps(r, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("-n", "--limit", type=int, default=100, help="Number of items")
    parser.add_argument("-w", "--workers", type=int, default=10, help="Parallel workers")
    parser.add_argument("--show", action="store_true", help="Show sample results")
    args = parser.parse_args()

    results, errors = batch_extract(args.limit, args.workers)

    if args.show and results:
        show_sample_results(results)
