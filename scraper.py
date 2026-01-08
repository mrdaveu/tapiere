"""
Scrapers for Mercari and Yahoo Auctions Japan.
Uses direct HTTP for Yahoo (fast) and Playwright for Mercari (JS-rendered).
Smart scraping: stops when we find 5 consecutive items that already exist.
"""

import time
import re
import random
import asyncio
from urllib.parse import quote
from tqdm import tqdm

from database import save_scraped_items, update_keyword_scraped, get_keywords, get_existing_source_ids

# Number of consecutive existing items to trigger stop
OVERLAP_THRESHOLD = 5


# ============== FAST HTTP SCRAPER FOR YAHOO ==============

async def scrape_yahoo_fast(keyword: str, max_items: int = 300,
                            existing_ids: set = None, keyword_id: int = None) -> list:
    """
    Fast Yahoo Auctions scraper using direct HTTP requests.
    Parses data-* attributes from HTML - no browser needed.
    """
    try:
        import httpx
        from bs4 import BeautifulSoup
    except ImportError:
        print("Missing dependencies. Run: pip install httpx beautifulsoup4")
        return []

    if existing_ids is None:
        existing_ids = get_existing_source_ids('yahoo', keyword_id)

    all_items = []
    consecutive_existing = 0

    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept-Language": "ja-JP,ja;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }

    async with httpx.AsyncClient(headers=headers, follow_redirects=True, timeout=30.0) as client:
        page_num = 1
        items_per_page = 100
        max_pages = (max_items // items_per_page) + 2

        while len(all_items) < max_items and page_num <= max_pages:
            # Calculate offset: b=1 for page 1, b=101 for page 2, etc.
            offset = (page_num - 1) * items_per_page + 1
            search_url = f"https://auctions.yahoo.co.jp/search/search?p={quote(keyword)}&va={quote(keyword)}&exflg=1&b={offset}&n={items_per_page}&s1=new&o1=d"

            if page_num == 1:
                print(f"[Yahoo-Fast] Searching: {search_url}")

            try:
                response = await client.get(search_url)
                response.raise_for_status()
            except Exception as e:
                print(f"[Yahoo-Fast] Request failed: {e}")
                break

            soup = BeautifulSoup(response.text, 'html.parser')

            # Find all product links with data attributes
            products = soup.select('a.Product__imageLink[data-auction-id]')

            if not products:
                # Try alternative selector
                products = soup.select('[data-auction-id]')

            if not products:
                print(f"[Yahoo-Fast] No products found on page {page_num}")
                break

            page_new_items = 0
            for product in products:
                if len(all_items) >= max_items:
                    break

                auction_id = product.get('data-auction-id')
                if not auction_id:
                    continue

                # Skip duplicates in current batch
                if any(i["source_id"] == auction_id for i in all_items):
                    continue

                # Check against existing items
                if auction_id in existing_ids:
                    consecutive_existing += 1
                    if consecutive_existing >= OVERLAP_THRESHOLD:
                        print(f"[Yahoo-Fast] Found {OVERLAP_THRESHOLD} consecutive existing items, stopping")
                        return all_items
                    continue
                else:
                    consecutive_existing = 0

                title = product.get('data-auction-title', f'Auction {auction_id}')
                image_url = product.get('data-auction-img')
                price_str = product.get('data-auction-price', '')

                try:
                    price = int(price_str) if price_str else None
                except ValueError:
                    price = None

                # Extract category from data-auction-category attribute
                category = product.get('data-auction-category')
                category_id = f"yahoo:{category}" if category else None

                all_items.append({
                    "source": "yahoo",
                    "source_id": auction_id,
                    "url": f"https://page.auctions.yahoo.co.jp/jp/auction/{auction_id}",
                    "title": title[:200],
                    "price": price,
                    "image_url": image_url,
                    "category_id": category_id,
                })
                page_new_items += 1

            if page_new_items == 0:
                # No new items on this page, stop
                break

            page_num += 1

    print(f"[Yahoo-Fast] Scraped {len(all_items)} new items")
    return all_items


async def scrape_mercari_fast(keyword: str, max_items: int = 300,
                              existing_ids: set = None, keyword_id: int = None) -> list:
    """
    Fast Mercari scraper using the Mercari API directly.
    No browser needed - uses API calls with DPOP authentication.
    Returns JPY prices without localization issues.
    """
    try:
        import requests
        from mercari_api import generate_dpop
    except ImportError:
        print("Missing mercari package. Run: pip install mercari")
        return []

    if existing_ids is None:
        existing_ids = get_existing_source_ids('mercari', keyword_id)

    all_items = []
    consecutive_existing = 0

    search_url = "https://api.mercari.jp/v2/entities:search"
    root_product_url = "https://jp.mercari.com/item/"

    print(f"[Mercari-API] Searching: {keyword}")

    page_token = "v1:0"
    has_next_page = True

    while has_next_page and len(all_items) < max_items:
        dpop = generate_dpop(uuid="Mercari Python Bot", method="POST", url=search_url)

        headers = {
            'DPOP': dpop,
            'X-Platform': 'web',
            'Accept': '*/*',
            'Accept-Encoding': 'deflate, gzip',
            'Content-Type': 'application/json; charset=utf-8',
            'User-Agent': 'python-mercari',
        }

        data = {
            "userId": f"SCRAPER_{random.randint(10000, 99999)}",
            "pageSize": 120,
            "pageToken": page_token,
            "searchSessionId": f"SCRAPER_{random.randint(10000, 99999)}",
            "indexRouting": "INDEX_ROUTING_UNSPECIFIED",
            "searchCondition": {
                "keyword": keyword,
                "sort": "SORT_CREATED_TIME",
                "order": "ORDER_DESC",
                "status": ["STATUS_ON_SALE"],
            },
            "withAuction": True,
            "defaultDatasets": ["DATASET_TYPE_MERCARI", "DATASET_TYPE_BEYOND"]
        }

        try:
            r = requests.post(search_url, headers=headers, json=data, timeout=30)
            r.raise_for_status()
            resp = r.json()
        except Exception as e:
            print(f"[Mercari-API] Request failed: {e}")
            break

        items = resp.get("items", [])
        if not items:
            break

        next_page_token = resp.get("meta", {}).get("nextPageToken")
        has_next_page = bool(next_page_token)
        page_token = next_page_token

        for item_data in items:
            if len(all_items) >= max_items:
                break

            item_id = item_data.get("id")
            if not item_id:
                continue

            # Skip duplicates in current batch
            if any(i["source_id"] == item_id for i in all_items):
                continue

            # Check against existing items
            if item_id in existing_ids:
                consecutive_existing += 1
                if consecutive_existing >= OVERLAP_THRESHOLD:
                    print(f"[Mercari-API] Found {OVERLAP_THRESHOLD} consecutive existing items, stopping")
                    return all_items
                continue
            else:
                consecutive_existing = 0

            title = item_data.get("name", f"Item {item_id}")
            price = item_data.get("price")
            thumbnails = item_data.get("thumbnails", [])
            image_url = thumbnails[0] if thumbnails else None

            # Extract category ID
            cat_id = item_data.get("categoryId")
            category_id = f"mercari:{cat_id}" if cat_id else None

            all_items.append({
                "source": "mercari",
                "source_id": item_id,
                "url": f"{root_product_url}{item_id}",
                "title": title[:200],
                "price": price,
                "image_url": image_url,
                "category_id": category_id,
            })

    print(f"[Mercari-API] Scraped {len(all_items)} new items")
    return all_items


async def scrape_rakuten_fast(keyword: str, max_items: int = 300,
                               existing_ids: set = None, keyword_id: int = None) -> list:
    """
    Fast Rakuten (Fril) scraper using direct HTTP requests.
    Parses HTML structure - no browser needed.
    """
    try:
        import httpx
        from bs4 import BeautifulSoup
    except ImportError:
        print("Missing dependencies. Run: pip install httpx beautifulsoup4")
        return []

    if existing_ids is None:
        existing_ids = get_existing_source_ids('rakuten', keyword_id)

    all_items = []
    consecutive_existing = 0

    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept-Language": "ja-JP,ja;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }

    async with httpx.AsyncClient(headers=headers, follow_redirects=True, timeout=30.0) as client:
        page_num = 1
        max_pages = 10

        print(f"[Rakuten-Fast] Searching: {keyword}")

        while page_num <= max_pages and len(all_items) < max_items:
            # Rakuten (Fril) search URL
            url = f"https://fril.jp/s?query={quote(keyword)}&sort=1&page={page_num}"

            try:
                response = await client.get(url)
                if response.status_code != 200:
                    print(f"[Rakuten-Fast] Error: Status {response.status_code}")
                    break

                soup = BeautifulSoup(response.text, "html.parser")
                items = soup.select("div.item")

                if not items:
                    print(f"[Rakuten-Fast] No items on page {page_num}, stopping")
                    break

                for item_div in items:
                    # Extract item ID from the link
                    link = item_div.select_one("a.link_search_image")
                    if not link or not link.get("href"):
                        continue

                    item_url = link["href"]
                    # Extract item ID from URL like: https://item.fril.jp/f86ec7e80b0df0cedc30ddd1548841b1
                    item_id_match = re.search(r'/([a-f0-9]{32})', item_url)
                    if not item_id_match:
                        continue

                    item_id = item_id_match.group(1)

                    # Check if we already have this item
                    if item_id in existing_ids:
                        consecutive_existing += 1
                        if consecutive_existing >= OVERLAP_THRESHOLD:
                            print(f"[Rakuten-Fast] Found {consecutive_existing} consecutive existing items, stopping")
                            return all_items
                        continue

                    consecutive_existing = 0

                    # Extract title
                    title_elem = item_div.select_one("a.link_search_title span")
                    title = title_elem.get_text(strip=True) if title_elem else "Untitled"

                    # Extract price
                    price_elem = item_div.select_one("p.item-box__item-price")
                    price = 0
                    if price_elem:
                        price_text = price_elem.get_text(strip=True)
                        price_match = re.search(r'[\d,]+', price_text)
                        if price_match:
                            price = int(price_match.group().replace(',', ''))

                    # Extract image
                    img_elem = item_div.select_one("img.img-responsive")
                    image_url = img_elem.get("src") or img_elem.get("data-original") if img_elem else ""

                    # Extract brand (if available)
                    brand_elem = item_div.select_one("a.brand-name")
                    brand = brand_elem.get_text(strip=True) if brand_elem else None

                    # Build title with brand if available
                    if brand and brand not in title:
                        title = f"{brand} {title}"

                    all_items.append({
                        "source": "rakuten",
                        "source_id": item_id,
                        "url": f"https://item.fril.jp/{item_id}",
                        "title": title[:200],
                        "price": price,
                        "image_url": image_url,
                        "category_id": None,  # Can be extracted later if needed
                    })

                await asyncio.sleep(random.uniform(0.5, 1.5))
                page_num += 1

            except Exception as e:
                print(f"[Rakuten-Fast] Error on page {page_num}: {e}")
                break

    print(f"[Rakuten-Fast] Scraped {len(all_items)} new items")
    return all_items


async def scrape_keyword_fast(keyword_id: int, keyword: str, source: str = 'both',
                              max_items: int = 300) -> dict:
    """
    Fast async scraper that runs multiple sources in parallel.
    """
    all_items = []

    tasks = []
    if source in ('mercari', 'both', 'all'):
        tasks.append(scrape_mercari_fast(keyword, max_items=max_items, keyword_id=keyword_id))
    if source in ('yahoo', 'both', 'all'):
        tasks.append(scrape_yahoo_fast(keyword, max_items=max_items, keyword_id=keyword_id))
    if source in ('rakuten', 'all'):
        tasks.append(scrape_rakuten_fast(keyword, max_items=max_items, keyword_id=keyword_id))

    if tasks:
        results = await asyncio.gather(*tasks)
        for result in results:
            all_items.extend(result)

    if all_items:
        new_count = save_scraped_items(all_items, keyword_id)
        update_keyword_scraped(keyword_id, new_count)
        return {"scraped": len(all_items), "saved": new_count}

    return {"scraped": 0, "saved": 0}


def scrape_keyword_fast_sync(keyword_id: int, keyword: str, source: str = 'both',
                             max_items: int = 300) -> dict:
    """Synchronous wrapper for fast scraping."""
    return asyncio.run(scrape_keyword_fast(keyword_id, keyword, source, max_items))


# ============== SYNC API SCRAPERS ==============


def scrape_mercari(keyword: str, max_items: int = 300, headless: bool = True,
                   existing_ids: set = None, keyword_id: int = None) -> list:
    """
    Scrape Mercari Japan for items matching keyword using API.
    Smart scraping: stops after finding OVERLAP_THRESHOLD consecutive existing items.
    Returns list of item dicts with JPY prices.
    """
    try:
        import requests
        from mercari_api import generate_dpop
    except ImportError:
        print("Missing mercari package. Run: pip install mercari")
        return []

    if existing_ids is None:
        existing_ids = get_existing_source_ids('mercari', keyword_id)

    all_items = []
    consecutive_existing = 0

    search_url = "https://api.mercari.jp/v2/entities:search"
    root_product_url = "https://jp.mercari.com/item/"

    print(f"[Mercari] Searching: {keyword}")

    page_token = "v1:0"
    has_next_page = True

    with tqdm(total=max_items, desc="[Mercari]") as pbar:
        while has_next_page and len(all_items) < max_items:
            dpop = generate_dpop(uuid="Mercari Python Bot", method="POST", url=search_url)

            headers = {
                'DPOP': dpop,
                'X-Platform': 'web',
                'Accept': '*/*',
                'Accept-Encoding': 'deflate, gzip',
                'Content-Type': 'application/json; charset=utf-8',
                'User-Agent': 'python-mercari',
            }

            data = {
                "userId": f"SCRAPER_{random.randint(10000, 99999)}",
                "pageSize": 120,
                "pageToken": page_token,
                "searchSessionId": f"SCRAPER_{random.randint(10000, 99999)}",
                "indexRouting": "INDEX_ROUTING_UNSPECIFIED",
                "searchCondition": {
                    "keyword": keyword,
                    "sort": "SORT_CREATED_TIME",
                    "order": "ORDER_DESC",
                    "status": ["STATUS_ON_SALE"],
                },
                "withAuction": True,
                "defaultDatasets": ["DATASET_TYPE_MERCARI", "DATASET_TYPE_BEYOND"]
            }

            try:
                r = requests.post(search_url, headers=headers, json=data, timeout=30)
                r.raise_for_status()
                resp = r.json()
            except Exception as e:
                print(f"[Mercari] Request failed: {e}")
                break

            items = resp.get("items", [])
            if not items:
                break

            next_page_token = resp.get("meta", {}).get("nextPageToken")
            has_next_page = bool(next_page_token)
            page_token = next_page_token

            for item_data in items:
                if len(all_items) >= max_items:
                    break

                item_id = item_data.get("id")
                if not item_id:
                    continue

                # Skip duplicates in current batch
                if any(i["source_id"] == item_id for i in all_items):
                    continue

                # Check against existing items
                if item_id in existing_ids:
                    consecutive_existing += 1
                    if consecutive_existing >= OVERLAP_THRESHOLD:
                        print(f"[Mercari] Found {OVERLAP_THRESHOLD} consecutive existing items, stopping")
                        return all_items
                    continue
                else:
                    consecutive_existing = 0

                title = item_data.get("name", f"Item {item_id}")
                price = item_data.get("price")
                thumbnails = item_data.get("thumbnails", [])
                image_url = thumbnails[0] if thumbnails else None

                # Extract category ID
                cat_id = item_data.get("categoryId")
                category_id = f"mercari:{cat_id}" if cat_id else None

                all_items.append({
                    "source": "mercari",
                    "source_id": item_id,
                    "url": f"{root_product_url}{item_id}",
                    "title": title[:200],
                    "price": price,
                    "image_url": image_url,
                    "category_id": category_id,
                })
                pbar.update(1)

    print(f"[Mercari] Scraped {len(all_items)} new items")
    return all_items


# LEGACY: Playwright-based Yahoo scraper - commented out, using HTTP scraper instead
# def scrape_yahoo_auctions(keyword: str, max_items: int = 300, headless: bool = True,
#                           existing_ids: set = None, keyword_id: int = None) -> list:
#     """Legacy Playwright scraper - use scrape_yahoo_fast instead."""
#     pass


def scrape_keyword(keyword_id: int, keyword: str, source: str = 'both', max_items: int = 300, use_fast: bool = True) -> dict:
    """
    Scrape items for a keyword and save to database.
    Always uses API-based scrapers (fast, no browser needed).
    """
    # Always use the fast API-based scrapers
    return scrape_keyword_fast_sync(keyword_id, keyword, source, max_items)


def scrape_all_keywords(max_items_per_source: int = 300) -> dict:
    """
    Scrape all keywords in the database.
    """
    keywords = get_keywords()
    if not keywords:
        print("No keywords to scrape. Add keywords first.")
        return {"total_scraped": 0, "total_saved": 0}

    total_scraped = 0
    total_saved = 0

    for kw in keywords:
        print(f"\n=== Scraping keyword: {kw['keyword']} (source: {kw['source']}) ===")
        result = scrape_keyword(kw['id'], kw['keyword'], kw['source'], max_items_per_source)
        total_scraped += result['scraped']
        total_saved += result['saved']

    print(f"\n=== Total: {total_scraped} scraped, {total_saved} new items saved ===")
    return {"total_scraped": total_scraped, "total_saved": total_saved}


# Mock data for testing without scraping
def generate_mock_items(keyword: str, count: int = 100) -> list:
    """Generate mock items for testing."""
    brands = ["MHL", "Margaret Howell", "Uniqlo", "Muji", "Comme des Garcons",
              "Yohji Yamamoto", "Issey Miyake", "Kapital", "Visvim", "Needles"]
    item_types = ["sweater", "cardigan", "jacket", "coat", "shirt", "pants", "jeans", "hoodie"]
    colors = ["black", "navy", "grey", "white", "brown", "olive", "cream"]
    sources = ["mercari", "yahoo"]

    mock_items = []
    for i in range(count):
        source = random.choice(sources)
        brand = random.choice(brands)
        item_type = random.choice(item_types)
        color = random.choice(colors)
        price = round(random.randint(1000, 15000) / 100) * 100
        image_id = random.randint(1, 1000)

        mock_items.append({
            "source": source,
            "source_id": f"mock_{source}_{i}_{random.randint(10000, 99999)}",
            "url": f"https://example.com/{source}/{i}",
            "title": f"{brand} {color} {item_type}",
            "price": price,
            "image_url": f"https://picsum.photos/seed/{image_id}/400/400",
        })

    return mock_items


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Scrape Mercari and Yahoo Auctions")
    parser.add_argument("keyword", nargs="?", help="Search keyword")
    parser.add_argument("-n", "--max-items", type=int, default=100, help="Max items per source")
    parser.add_argument("--source", choices=["mercari", "yahoo", "both"], default="both")
    parser.add_argument("--mock", action="store_true", help="Use mock data")

    args = parser.parse_args()

    if args.mock:
        items = generate_mock_items(args.keyword or "test", args.max_items)
        print(f"Generated {len(items)} mock items")
        for item in items[:5]:
            print(f"  - {item['source']}: {item['title']} ¥{item['price']}")
    elif args.keyword:
        if args.source in ("mercari", "both"):
            mercari_items = scrape_mercari(args.keyword, args.max_items)
            print(f"Mercari: {len(mercari_items)} items")

        if args.source in ("yahoo", "both"):
            yahoo_items = scrape_yahoo_auctions(args.keyword, args.max_items)
            print(f"Yahoo: {len(yahoo_items)} items")
    else:
        print("Provide a keyword to search, or use --mock for testing")
