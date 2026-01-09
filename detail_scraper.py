"""
Detail scraper for Mercari and Yahoo Auctions item pages.

Uses Mercari API for Mercari items (fast, no browser needed).
Uses httpx for Yahoo items (parses __NEXT_DATA__ JSON).
"""

import json
import re
import time
import random
from typing import Optional

from database import get_connection


def is_mercari_shop_item(item_id: str) -> bool:
    """
    Check if item_id is a business/shop item (not regular m-prefixed).

    Regular items: m followed by 11 digits (e.g., m86254101449)
    Shop items: Alphanumeric string NOT matching m\d{11} pattern
    """
    return not re.match(r'^m\d{11}$', item_id)


def scrape_mercari_shop_detail(item_id: str) -> dict:
    """
    Scrape Mercari shop/business item details via HTML page.
    Shop items don't work with the regular API endpoint.
    """
    result = {"description": None, "price": None, "images": [], "sold_status": None}

    try:
        import httpx

        url = f"https://jp.mercari.com/shops/product/{item_id}"
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            "Accept-Language": "ja-JP,ja;q=0.9",
        }

        with httpx.Client(headers=headers, follow_redirects=True, timeout=15.0) as client:
            response = client.get(url)
            response.raise_for_status()
            html = response.text

        # Parse __NEXT_DATA__ JSON (similar to Yahoo approach)
        match = re.search(r'<script id="__NEXT_DATA__"[^>]*>([^<]+)</script>', html)
        if match:
            data = json.loads(match.group(1))
            # Extract item details from pageProps
            item_data = data.get("props", {}).get("pageProps", {}).get("item", {})

            result["description"] = item_data.get("description")
            result["price"] = item_data.get("price")

            # Extract images - shop items have photos array with imageUrl field
            photos = item_data.get("photos", [])
            if photos:
                result["images"] = [img.get("imageUrl") for img in photos if img.get("imageUrl")][:20]

            # Status
            status = item_data.get("status", "")
            if status == "on_sale":
                result["sold_status"] = "available"
            elif status == "trading":
                result["sold_status"] = "trading"
            elif status == "sold_out":
                result["sold_status"] = "sold"
            else:
                result["sold_status"] = status or "unknown"

    except Exception as e:
        print(f"Error fetching Mercari shop item {item_id}: {e}")

    return result


def scrape_mercari_detail(url: str, page=None) -> dict:
    """
    Fetch Mercari item details using the API directly.
    No browser needed - fast and returns JPY prices.
    Routes to shop scraper for business/shop items.
    """
    result = {"description": None, "price": None, "images": [], "sold_status": None}

    # Extract item ID from URL - handle both /item/ and /shops/product/ URLs
    match = re.search(r'/(?:item|shops/product)/([a-zA-Z0-9]+)', url)
    if not match:
        print(f"Could not extract item ID from URL: {url}")
        return result

    item_id = match.group(1)

    # Route shop/business items to dedicated scraper
    if is_mercari_shop_item(item_id):
        return scrape_mercari_shop_detail(item_id)

    try:
        import requests
        from mercari_api import generate_dpop

        api_url = "https://api.mercari.jp/items/get"
        dpop = generate_dpop(uuid="Mercari Python Bot", method="GET", url=api_url)

        headers = {
            'DPOP': dpop,
            'X-Platform': 'web',
            'Accept': '*/*',
            'Accept-Encoding': 'deflate, gzip',
            'Content-Type': 'application/json; charset=utf-8',
            'User-Agent': 'python-mercari',
        }

        # No country_code = returns JPY prices without localization
        r = requests.get(api_url, headers=headers, params={"id": item_id}, timeout=15)
        r.raise_for_status()
        item_data = r.json().get('data', {})

        result["description"] = item_data.get('description')
        result["price"] = item_data.get('price')
        result["images"] = item_data.get('photos', [])[:20]

        # Status
        status = item_data.get('status', '')
        if status == "on_sale":
            result["sold_status"] = "available"
        elif status == "trading":
            result["sold_status"] = "trading"
        elif status == "sold_out":
            result["sold_status"] = "sold"
        else:
            result["sold_status"] = status or "unknown"

    except Exception as e:
        print(f"Error fetching Mercari {url}: {e}")

    return result


def scrape_yahoo_detail(url: str, page=None) -> dict:
    """
    Fetch Yahoo Auctions item details using httpx.
    Parses __NEXT_DATA__ JSON from the page - no browser needed.
    """
    result = {"description": None, "price": None, "images": [], "is_auction": False, "auction_end_time": None, "sold_status": None}

    try:
        import httpx

        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            "Accept-Language": "ja-JP,ja;q=0.9",
        }

        with httpx.Client(headers=headers, follow_redirects=True, timeout=15.0) as client:
            response = client.get(url)
            response.raise_for_status()
            html = response.text

        # Extract __NEXT_DATA__ JSON
        match = re.search(r'<script id="__NEXT_DATA__"[^>]*>([^<]+)</script>', html)
        if match:
            data = json.loads(match.group(1))
            item = (data.get("props", {})
                       .get("pageProps", {})
                       .get("initialState", {})
                       .get("item", {})
                       .get("detail", {})
                       .get("item", {}))

            if item:
                img_list = item.get("img", [])
                result["images"] = [img["image"] for img in img_list if img.get("image")][:20]
                result["price"] = item.get("taxinPrice") or item.get("price")

                # Description is an array of strings - join them
                desc_list = item.get("description", [])
                if desc_list and isinstance(desc_list, list):
                    result["description"] = "\n".join(desc_list)
                else:
                    result["description"] = item.get("title")

                # Auction vs Buy-It-Now classification
                price = item.get("price", 0)
                bidorbuy = item.get("bidorbuy")
                if bidorbuy is not None and bidorbuy == price:
                    result["is_auction"] = False
                else:
                    result["is_auction"] = True

                # End time
                end_time = item.get("endTime")
                if end_time:
                    if isinstance(end_time, (int, float)):
                        result["auction_end_time"] = int(end_time / 1000) if end_time > 9999999999 else int(end_time)
                    elif isinstance(end_time, str):
                        try:
                            from datetime import datetime
                            dt = datetime.fromisoformat(end_time.replace('Z', '+00:00'))
                            result["auction_end_time"] = int(dt.timestamp())
                        except:
                            pass

                # Sold status
                status = item.get("status", "")
                if status == "open":
                    result["sold_status"] = "available"
                elif status == "closed":
                    result["sold_status"] = "sold"
                elif status == "cancelled":
                    result["sold_status"] = "cancelled"
                else:
                    result["sold_status"] = status or "unknown"

    except Exception as e:
        print(f"Error fetching Yahoo {url}: {e}")

    return result


def scrape_rakuten_detail(url: str, page=None) -> dict:
    """
    Fetch Rakuten (Fril) item details using httpx.
    Parses JSON-LD structured data and HTML - no browser needed.
    """
    result = {"description": None, "price": None, "images": [], "sold_status": None}

    try:
        import httpx
        import json
        from bs4 import BeautifulSoup

        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept-Language": "ja-JP,ja;q=0.9",
        }

        with httpx.Client(headers=headers, follow_redirects=True, timeout=30.0) as client:
            response = client.get(url)
            if response.status_code != 200:
                print(f"Error fetching Rakuten {url}: Status {response.status_code}")
                return result

            soup = BeautifulSoup(response.text, "html.parser")

            # Try to extract from JSON-LD structured data first
            json_ld = soup.select_one('script[type="application/ld+json"]')
            if json_ld:
                try:
                    data = json.loads(json_ld.string)
                    if data.get("@type") == "Product":
                        result["description"] = data.get("description")
                        if data.get("offers", {}).get("price"):
                            result["price"] = int(data["offers"]["price"])
                        # Check availability from JSON-LD
                        avail = data.get("offers", {}).get("availability", "")
                        if "OutOfStock" in avail or "SoldOut" in avail:
                            result["sold_status"] = "sold"
                        else:
                            result["sold_status"] = "available"
                except (json.JSONDecodeError, KeyError, ValueError):
                    pass

            # Extract description from HTML if not found in JSON-LD
            if not result["description"]:
                desc_elem = soup.select_one("div.item__description__line-limited")
                if desc_elem:
                    result["description"] = desc_elem.get_text(strip=True)

            # Extract images from sp-image elements (the main gallery)
            images = []
            for img in soup.select("img.sp-image"):
                img_url = img.get("src")
                if img_url and img_url not in images and "item_square_dummy" not in img_url:
                    images.append(img_url)

            # Fallback to og:image if no images found
            if not images:
                og_img = soup.select_one('meta[property="og:image"]')
                if og_img and og_img.get("content"):
                    images.append(og_img["content"])

            result["images"] = images

            # Check sold status from HTML if not determined yet
            if not result["sold_status"]:
                # Look for sold indicators
                sold_text = soup.find(string=re.compile(r'SOLD|売り切れ|売却済み'))
                if sold_text:
                    result["sold_status"] = "sold"
                else:
                    result["sold_status"] = "available"

    except Exception as e:
        print(f"Error fetching Rakuten {url}: {e}")

    return result


def scrape_item_detail(item: dict, page=None) -> dict:
    """
    Scrape detail for an item based on its source.

    Args:
        item: dict with 'source', 'url' keys
        page: Optional Playwright page to reuse

    Returns:
        dict with description, price, images
    """
    source = item.get('source', '')
    url = item.get('url', '')

    if source == 'mercari':
        return scrape_mercari_detail(url, page)
    elif source == 'yahoo':
        return scrape_yahoo_detail(url, page)
    elif source == 'rakuten':
        return scrape_rakuten_detail(url, page)
    else:
        print(f"Unknown source: {source}")
        return {"description": None, "price": None, "images": []}


def update_item_details(item_id: int, details: dict):
    """
    Update an item in the database with scraped details.
    """
    conn = get_connection()
    cursor = conn.cursor()

    images_json = json.dumps(details.get("images", []))

    # Build dynamic update query based on what fields we have
    update_parts = ["description = ?", "images = ?"]
    params = [details.get("description"), images_json]

    # Only update price if we got a new one and don't have one
    if details.get("price"):
        update_parts.append("price = COALESCE(price, ?)")
        params.append(details.get("price"))

    # Update sold_status if present
    if details.get("sold_status"):
        update_parts.append("sold_status = ?")
        params.append(details.get("sold_status"))

    # Update is_auction if present (for Yahoo)
    if "is_auction" in details and details.get("is_auction") is not None:
        update_parts.append("is_auction = ?")
        params.append(1 if details.get("is_auction") else 0)

    # Update auction_end_time if present (for Yahoo)
    if details.get("auction_end_time"):
        update_parts.append("auction_end_time = ?")
        params.append(details.get("auction_end_time"))

    params.append(item_id)

    cursor.execute(f"""
        UPDATE items
        SET {', '.join(update_parts)}
        WHERE id = ?
    """, params)

    conn.commit()
    conn.close()


def get_items_needing_details(limit: int = 100, source: str = None) -> list:
    """
    Get items that don't have description or images yet.
    """
    conn = get_connection()
    cursor = conn.cursor()

    query = """
        SELECT id, source, source_id, url, title
        FROM items
        WHERE (description IS NULL OR description = '' OR images IS NULL OR images = '' OR images = '[]')
    """
    params = []

    if source:
        query += " AND source = ?"
        params.append(source)

    query += " ORDER BY scraped_at DESC LIMIT ?"
    params.append(limit)

    cursor.execute(query, params)
    items = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return items


def scrape_details_batch(items: list = None, limit: int = 100, source: str = None) -> int:
    """
    Scrape details for multiple items using API/HTTP (no browser needed).

    Args:
        items: Optional list of items to scrape. If None, fetches from DB.
        limit: Max items to process
        source: Optional filter by source ('mercari' or 'yahoo')

    Returns:
        Number of items updated
    """
    from tqdm import tqdm

    if items is None:
        items = get_items_needing_details(limit, source)

    if not items:
        print("No items need detail scraping")
        return 0

    print(f"Scraping details for {len(items)} items...")

    updated = 0

    for item in tqdm(items, desc="Scraping details"):
        try:
            details = scrape_item_detail(item)

            if details.get("description") or details.get("images"):
                update_item_details(item["id"], details)
                updated += 1

            # Rate limit - be gentle with APIs
            time.sleep(0.3 + random.random() * 0.2)

        except Exception as e:
            print(f"Error on item {item['id']}: {e}")
            continue

    print(f"Updated {updated} items with details")
    return updated


def get_item_with_details(item_id: int) -> Optional[dict]:
    """
    Get an item with its full details, parsing images JSON.
    """
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT * FROM items WHERE id = ?", (item_id,))
    row = cursor.fetchone()
    conn.close()

    if not row:
        return None

    item = dict(row)

    # Parse images JSON
    if item.get("images"):
        try:
            item["images"] = json.loads(item["images"])
        except:
            item["images"] = []
    else:
        item["images"] = []

    return item


def get_item_display_data(item_id: int) -> Optional[dict]:
    """
    Get item data formatted for display on the page.

    Returns:
        dict with keys:
            - description: str
            - price: str (formatted with yen symbol)
            - img_url_1 through img_url_N: individual image URLs
    """
    item = get_item_with_details(item_id)
    if not item:
        return None

    result = {
        "id": item["id"],
        "title": item.get("title", ""),
        "description": item.get("description", ""),
        "price": f"¥{item['price']:,}" if item.get("price") else "",
        "url": item.get("url", ""),
        "source": item.get("source", ""),
    }

    # Add individual image URLs
    images = item.get("images", [])
    for i, img_url in enumerate(images[:20], start=1):
        result[f"img_url_{i}"] = img_url

    return result


def get_items_display_data(item_ids: list = None, limit: int = 50) -> list:
    """
    Get display data for multiple items.

    Args:
        item_ids: List of specific item IDs, or None to get items with details
        limit: Max items to return

    Returns:
        List of item display dicts
    """
    conn = get_connection()
    cursor = conn.cursor()

    if item_ids:
        placeholders = ",".join("?" * len(item_ids))
        cursor.execute(f"""
            SELECT * FROM items
            WHERE id IN ({placeholders}) AND description IS NOT NULL
            LIMIT ?
        """, item_ids + [limit])
    else:
        cursor.execute("""
            SELECT * FROM items
            WHERE description IS NOT NULL AND description != ''
            ORDER BY scraped_at DESC
            LIMIT ?
        """, (limit,))

    rows = cursor.fetchall()
    conn.close()

    results = []
    for row in rows:
        item = dict(row)

        # Parse images
        images = []
        if item.get("images"):
            try:
                images = json.loads(item["images"])
            except:
                pass

        display = {
            "id": item["id"],
            "title": item.get("title", ""),
            "description": item.get("description", ""),
            "price": f"¥{item['price']:,}" if item.get("price") else "",
            "url": item.get("url", ""),
            "source": item.get("source", ""),
        }

        for i, img_url in enumerate(images[:20], start=1):
            display[f"img_url_{i}"] = img_url

        results.append(display)

    return results


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Scrape item details from Mercari/Yahoo")
    parser.add_argument("-n", "--limit", type=int, default=10, help="Max items to scrape")
    parser.add_argument("--source", choices=["mercari", "yahoo"], help="Only scrape this source")
    parser.add_argument("--item", type=int, help="Scrape a single item by ID")
    parser.add_argument("--url", type=str, help="Test scrape a single URL")

    args = parser.parse_args()

    if args.url:
        # Test single URL
        if "mercari" in args.url:
            result = scrape_mercari_detail(args.url)
        elif "yahoo" in args.url or "auctions" in args.url:
            result = scrape_yahoo_detail(args.url)
        else:
            print("Unknown URL source")
            exit(1)

        print(f"\nDescription ({len(result['description'] or '')} chars):")
        print(result['description'][:500] if result['description'] else "(none)")
        print(f"\nPrice: {result['price']}")
        print(f"\nImages ({len(result['images'])}):")
        for i, img in enumerate(result['images'][:5]):
            print(f"  {i+1}. {img}")
        if len(result['images']) > 5:
            print(f"  ... and {len(result['images']) - 5} more")

    elif args.item:
        # Scrape single item from DB
        from database import get_item
        item = get_item(args.item)
        if item:
            print(f"Scraping: {item['title']}")
            details = scrape_item_detail(item)
            update_item_details(args.item, details)

            updated = get_item_with_details(args.item)
            print(f"\nDescription: {updated['description'][:200] if updated['description'] else '(none)'}...")
            print(f"Images: {len(updated['images'])}")
        else:
            print(f"Item {args.item} not found")

    else:
        # Batch scrape
        scrape_details_batch(limit=args.limit, source=args.source)
