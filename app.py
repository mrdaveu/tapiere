"""
TAPIERE - Shopping Helper for Japanese Marketplaces
FastAPI backend with embedded frontend and multi-user auth.
"""

from fastapi import FastAPI, HTTPException, BackgroundTasks, Request, Cookie, Depends, UploadFile, File, Header, Query
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from typing import List, Optional
import threading
import re
import os

from database import (
    init_db,
    get_next_unseen,
    mark_seen,
    save_item,
    unsave_item,
    rate_item,
    set_item_cart,
    get_saved_items,
    get_item,
    add_keyword,
    get_keywords,
    get_keywords_with_unseen_counts,
    delete_keyword,
    get_stats,
    prioritize_keyword,
    deprioritize_keyword,
    reorder_keywords,
    # Deck functions
    create_deck,
    get_decks,
    get_decks_with_keywords,
    get_deck,
    update_deck,
    update_deck_sizing,
    delete_deck,
    reorder_decks,
    get_keywords_by_deck,
    move_keyword_to_deck,
    get_deck_for_keyword,
    update_item_fit_score,
    # Category functions
    get_category_ancestors_with_names,
    hide_items_by_category,
    add_to_blocklist,
    get_keyword_blocklist,
    remove_from_keyword_blocklist,
    # Auth functions
    get_user_from_session,
    get_or_create_user,
    get_user_by_email,
    get_user_by_username,
    set_username,
    create_magic_link,
    verify_magic_link,
    create_session,
    delete_session,
    create_invite_request,
    get_invite_request_by_email,
    get_pending_invite_requests,
    approve_invite_request,
    # Sharing functions
    get_deck_share,
    set_deck_share,
    get_public_deck_by_slug,
    get_public_deck_items,
    # Migration
    migrate_to_multiuser,
)
from scraper import scrape_keyword, scrape_all_keywords, generate_mock_items, save_scraped_items
from email_service import send_magic_link, send_invite_confirmation

# Optional ML dependencies (not available on Railway due to size)
try:
    from detail_scraper import scrape_item_detail, update_item_details, get_item_display_data
    DETAIL_SCRAPER_AVAILABLE = True
except ImportError:
    DETAIL_SCRAPER_AVAILABLE = False
    scrape_item_detail = None
    update_item_details = None
    def get_item_display_data(item): return item  # passthrough

try:
    from llm_scorer import score_item_fit_sync
    LLM_SCORER_AVAILABLE = True
except ImportError:
    LLM_SCORER_AVAILABLE = False
    score_item_fit_sync = None

app = FastAPI(title="TAPIERE")
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# Environment
IS_PRODUCTION = os.environ.get('PRODUCTION', 'false').lower() == 'true'
UPLOAD_SECRET = os.environ.get('UPLOAD_SECRET', 'temp-secret-change-me')


# =====================================================
# AUTH DEPENDENCIES
# =====================================================

async def get_current_user_optional(session: Optional[str] = Cookie(None)) -> Optional[dict]:
    """Get current user from session cookie. Returns None if not logged in."""
    if not session:
        return None
    return get_user_from_session(session)


async def get_current_user(session: Optional[str] = Cookie(None)) -> dict:
    """Require authentication. Raises 401 if not logged in."""
    user = get_user_from_session(session) if session else None
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user


def is_demo_mode(request: Request) -> bool:
    """Check if request is in demo mode."""
    return request.cookies.get("demo_mode") == "true"

# Track scraping status
scrape_status = {"running": False, "message": ""}

# Background detail scraping queue
detail_scrape_queue = []
detail_scrape_running = False


def run_detail_scrape_worker():
    """Background worker that scrapes item details from the queue.
    Uses httpx for Yahoo and requests/mercari API for Mercari - no browser needed.
    """
    global detail_scrape_running, detail_scrape_queue

    if not DETAIL_SCRAPER_AVAILABLE:
        print("[DetailScraper] Not available (detail_scraper module not loaded)")
        return

    if detail_scrape_running:
        return

    detail_scrape_running = True

    try:
        while detail_scrape_queue:
            item = detail_scrape_queue.pop(0)
            try:
                details = scrape_item_detail(item)
                if details.get("description") or details.get("images"):
                    update_item_details(item["id"], details)
                    print(f"[DetailScraper] Updated item {item['id']}: {len(details.get('images', []))} images")
            except Exception as e:
                print(f"[DetailScraper] Error on item {item['id']}: {e}")

            import time
            time.sleep(0.5)  # Rate limit

    except Exception as e:
        print(f"[DetailScraper] Worker error: {e}")
    finally:
        detail_scrape_running = False


def queue_detail_scrape(item: dict):
    """Add an item to the detail scrape queue and start worker if needed."""
    global detail_scrape_queue

    # Only queue if item doesn't have details yet
    if not item.get("description") and not item.get("images"):
        # Avoid duplicates
        if not any(q["id"] == item["id"] for q in detail_scrape_queue):
            detail_scrape_queue.append({
                "id": item["id"],
                "source": item["source"],
                "url": item["url"],
            })
            print(f"[DetailScraper] Queued item {item['id']} for scraping")

            # Start worker thread if not running
            if not detail_scrape_running:
                thread = threading.Thread(target=run_detail_scrape_worker, daemon=True)
                thread.start()


@app.on_event("startup")
async def startup():
    init_db()

    # One-time cleanup: delete unseen Rakuten items with placeholder images
    # This runs on startup to clean up items scraped with the buggy scraper
    try:
        from database import get_connection
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            DELETE FROM items
            WHERE source = 'rakuten'
              AND seen = 0
              AND saved = 0
              AND (image_url LIKE '%item_square_dummy%' OR image_url = '' OR image_url IS NULL)
        """)
        deleted = cursor.rowcount
        conn.commit()
        conn.close()
        if deleted > 0:
            print(f"[Startup] Cleaned up {deleted} Rakuten items with placeholder images")
    except Exception as e:
        print(f"[Startup] Error during Rakuten cleanup: {e}")


# --- Pydantic Models ---

class SeenRequest(BaseModel):
    item_ids: List[int]


class RateRequest(BaseModel):
    stars: int


class CartRequest(BaseModel):
    in_cart: bool


class KeywordCreate(BaseModel):
    keyword: str
    source: str = "all"
    deck_id: Optional[int] = None


class KeywordUpdate(BaseModel):
    keyword: str
    source: Optional[str] = None
    whitelist: Optional[List[str]] = None


class MockDataRequest(BaseModel):
    keyword: str = "test"
    count: int = 50


class DeckCreate(BaseModel):
    name: str


class DeckUpdate(BaseModel):
    name: Optional[str] = None


class SizingMeasurement(BaseModel):
    op: Optional[str] = None  # '>', '<', '~' or None
    val: Optional[int] = None  # cm or None


class DeckSizingUpdate(BaseModel):
    a: Optional[SizingMeasurement] = None  # Shoulder width
    b: Optional[SizingMeasurement] = None  # Chest width
    c: Optional[SizingMeasurement] = None  # Length
    d: Optional[SizingMeasurement] = None  # Waist
    e: Optional[SizingMeasurement] = None  # Hip
    f: Optional[SizingMeasurement] = None  # Rise
    g: Optional[SizingMeasurement] = None  # Inseam
    h: Optional[SizingMeasurement] = None  # Reserved


class KeywordDeckUpdate(BaseModel):
    deck_id: int


class DeckReorderRequest(BaseModel):
    deck_ids: List[int]


# Auth request models
class LoginRequest(BaseModel):
    email: str


class SetUsernameRequest(BaseModel):
    username: str


class InviteRequest(BaseModel):
    email: str
    reason: Optional[str] = None


class DeckShareUpdate(BaseModel):
    is_public: bool


# =====================================================
# AUTH ENDPOINTS
# =====================================================

@app.post("/api/auth/login")
async def request_login(request: LoginRequest):
    """Request magic link for existing user."""
    user = get_user_by_email(request.email)
    if not user:
        raise HTTPException(status_code=404, detail="No account found. Request an invite first.")

    token = create_magic_link(request.email)
    send_magic_link(request.email, token, 'login')
    return {"status": "ok", "message": "Check your email for a login link"}


@app.get("/auth/verify")
async def verify_login(token: str):
    """Verify magic link and create session."""
    result = verify_magic_link(token)
    if not result:
        return RedirectResponse(url="/?error=invalid_link")

    user = get_or_create_user(result['email'])
    session_token = create_session(user['id'])

    # If user has no username, redirect to onboarding
    redirect_url = "/onboarding" if not user.get('username') else "/"

    response = RedirectResponse(url=redirect_url)
    response.set_cookie(
        key="session",
        value=session_token,
        httponly=True,
        secure=IS_PRODUCTION,
        samesite="lax",
        max_age=60*60*24*365  # 1 year
    )
    return response


@app.post("/api/auth/logout")
async def logout(session: Optional[str] = Cookie(None)):
    """Log out current user."""
    if session:
        delete_session(session)
    response = JSONResponse({"status": "ok"})
    response.delete_cookie("session")
    response.delete_cookie("demo_mode")
    return response


@app.get("/api/auth/me")
async def get_current_user_info(user: dict = Depends(get_current_user_optional)):
    """Get current user info. Returns null if not logged in."""
    if not user:
        return {"user": None}
    return {"user": {
        "id": user["id"],
        "email": user["email"],
        "username": user.get("username"),
    }}


@app.post("/api/auth/set-username")
async def set_username_endpoint(request: SetUsernameRequest, user: dict = Depends(get_current_user)):
    """Set username for current user (first login only)."""
    username = request.username.strip().lower()

    # Validate username
    if len(username) < 3 or len(username) > 20:
        raise HTTPException(status_code=400, detail="Username must be 3-20 characters")
    if not re.match(r'^[a-z0-9_-]+$', username):
        raise HTTPException(status_code=400, detail="Username can only contain lowercase letters, numbers, underscores, and hyphens")

    success = set_username(user['id'], username)
    if not success:
        raise HTTPException(status_code=400, detail="Username already taken")

    return {"status": "ok", "username": username}


# =====================================================
# INVITE ENDPOINTS
# =====================================================

@app.post("/api/invite/request")
async def request_invite(request: InviteRequest):
    """Request an invite to TAPIERE."""
    email = request.email.lower().strip()

    # Check if already a user
    if get_user_by_email(email):
        raise HTTPException(status_code=400, detail="You already have an account. Try signing in.")

    # Check if already requested
    existing = get_invite_request_by_email(email)
    if existing:
        if existing['status'] == 'pending':
            return {"status": "ok", "message": "You're already on the waitlist!"}
        elif existing['status'] == 'approved':
            return {"status": "ok", "message": "You've already been approved. Check your email for a login link."}

    # Create invite request
    request_id = create_invite_request(email, request.reason)
    if not request_id:
        return {"status": "ok", "message": "You're already on the waitlist!"}

    send_invite_confirmation(email)
    return {"status": "ok", "message": "You're on the waitlist! We'll send you a link when you're approved."}


@app.get("/api/invite/requests")
async def list_invite_requests(user: dict = Depends(get_current_user)):
    """List pending invite requests (admin only - user #1)."""
    if user['id'] != 1:
        raise HTTPException(status_code=403, detail="Not authorized")

    requests = get_pending_invite_requests()
    return {"requests": requests}


@app.post("/api/invite/approve/{request_id}")
async def approve_invite_endpoint(request_id: int, user: dict = Depends(get_current_user)):
    """Approve an invite request (admin only - user #1)."""
    if user['id'] != 1:
        raise HTTPException(status_code=403, detail="Not authorized")

    email = approve_invite_request(request_id, user['id'])
    if not email:
        raise HTTPException(status_code=404, detail="Request not found")

    # Send magic link
    token = create_magic_link(email, 'invite')
    send_magic_link(email, token, 'invite')

    return {"status": "ok", "email": email}


# =====================================================
# DECK SHARING ENDPOINTS
# =====================================================

@app.get("/api/decks/{deck_id}/share")
async def get_deck_share_settings(deck_id: int, user: dict = Depends(get_current_user)):
    """Get share settings for a deck."""
    deck = get_deck(deck_id)
    if not deck or deck.get('user_id') != user['id']:
        raise HTTPException(status_code=404, detail="Deck not found")

    share = get_deck_share(deck_id)
    if share and share['is_public'] and user.get('username'):
        share_url = f"/u/{user['username']}/{share['share_slug']}"
        return {"is_public": True, "share_url": share_url}
    return {"is_public": False, "share_url": None}


@app.put("/api/decks/{deck_id}/share")
async def update_deck_share(deck_id: int, request: DeckShareUpdate, user: dict = Depends(get_current_user)):
    """Enable/disable public sharing for a deck."""
    if not user.get('username'):
        raise HTTPException(status_code=400, detail="Set a username first before sharing")

    deck = get_deck(deck_id)
    if not deck or deck.get('user_id') != user['id']:
        raise HTTPException(status_code=404, detail="Deck not found")

    # Create slug from deck name
    slug = re.sub(r'[^a-z0-9]+', '-', deck['name'].lower()).strip('-')

    set_deck_share(deck_id, slug, request.is_public)

    share_url = f"/u/{user['username']}/{slug}" if request.is_public else None
    return {"status": "ok", "is_public": request.is_public, "share_url": share_url}


@app.get("/u/{username}/{deck_slug}", response_class=HTMLResponse)
async def public_deck_page(request: Request, username: str, deck_slug: str):
    """Public deck view page."""
    deck = get_public_deck_by_slug(username, deck_slug)
    if not deck:
        raise HTTPException(status_code=404, detail="Deck not found")

    items = get_public_deck_items(deck['id'], deck['user_id'])

    return templates.TemplateResponse("public_deck.html", {
        "request": request,
        "owner_username": deck['owner_username'],
        "deck_name": deck['name'],
        "items": items,
    })


# --- API Endpoints ---

@app.get("/api/stats")
async def api_stats():
    return get_stats()


@app.get("/api/items/next")
async def get_next_items(
    request: Request,
    count: int = 5,
    exclude: str = None,
    user: dict = Depends(get_current_user_optional)
):
    """Get next unseen items for the reroll view."""
    # Demo mode: return random demo items
    demo = is_demo_mode(request)
    if demo and not user:
        import json
        import random
        try:
            with open("static/demo-items.json") as f:
                demo_items = json.load(f)
            items = random.sample(demo_items, min(count, len(demo_items)))
            return {"items": items, "count": len(items), "demo": True}
        except Exception as e:
            print(f"Demo items error: {e}")
            return {"items": [], "count": 0, "demo": True}

    # Auth required for real data
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    # Parse exclude IDs for preloading
    exclude_ids = []
    if exclude:
        try:
            exclude_ids = [int(x) for x in exclude.split(',') if x.strip()]
        except ValueError:
            pass

    items = get_next_unseen(count, exclude_ids=exclude_ids)
    return {"items": items, "count": len(items)}


@app.post("/api/items/seen")
async def mark_items_seen(request: SeenRequest):
    """Mark items as seen (after reroll)."""
    mark_seen(request.item_ids)
    return {"status": "ok", "marked": len(request.item_ids)}


@app.post("/api/items/{item_id}/save")
async def save_item_endpoint(item_id: int):
    """Save an item to the deck and queue detail scraping."""
    item = get_item(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")
    save_item(item_id)

    # Queue background detail scraping
    queue_detail_scrape(item)

    return {"status": "ok", "item_id": item_id}


@app.post("/api/items/{item_id}/unsave")
async def unsave_item_endpoint(item_id: int):
    """Remove an item from the deck."""
    unsave_item(item_id)
    return {"status": "ok", "item_id": item_id}


@app.put("/api/items/{item_id}/rate")
async def rate_item_endpoint(item_id: int, request: RateRequest):
    """Rate an item 1-5 stars."""
    if request.stars < 1 or request.stars > 5:
        raise HTTPException(status_code=400, detail="Stars must be 1-5")
    rate_item(item_id, request.stars)
    return {"status": "ok", "item_id": item_id, "stars": request.stars}


@app.put("/api/items/{item_id}/cart")
async def set_item_cart_endpoint(item_id: int, request: CartRequest):
    """Set an item's cart status."""
    set_item_cart(item_id, request.in_cart)
    return {"status": "ok", "item_id": item_id, "in_cart": request.in_cart}


@app.get("/api/items/saved")
async def get_saved_items_endpoint(
    source: str = None,
    sort: str = "date",
    order: str = "desc",
    filter_cart: int = None,
    filter_stars: List[int] = Query(None),
    filter_deck: int = None,
    filter_keyword: int = None
):
    """Get all saved items with optional additive filtering.

    Filters are additive (AND logic):
    - filter_cart: 1 to show only cart items
    - filter_stars: list of 1-5 to show items with those star ratings (OR logic within stars)
    - filter_deck: deck_id to show items from that deck
    - filter_keyword: keyword_id to show items from that keyword
    """
    items = get_saved_items(
        source=source,
        sort_by=sort,
        order=order,
        filter_cart=bool(filter_cart),
        filter_stars=filter_stars,
        filter_deck=filter_deck,
        filter_keyword=filter_keyword
    )
    return {"items": items, "count": len(items)}


@app.get("/api/items/{item_id}")
async def get_item_endpoint(item_id: int):
    """Get a single item."""
    item = get_item(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")
    return item


@app.get("/api/items/{item_id}/details")
async def get_item_details_endpoint(item_id: int):
    """Get item with full details including parsed images array."""
    data = get_item_display_data(item_id)
    if not data:
        raise HTTPException(status_code=404, detail="Item not found")
    return data


@app.post("/api/items/{item_id}/refresh-status")
async def refresh_item_status_endpoint(item_id: int):
    """
    Refresh an item's sold status, price, auction info by re-fetching from the listing page.
    Called when user opens item detail to check current status.
    Returns: { changed: bool, sold_status: str, price: int|null, is_auction: bool, auction_end_time: int|null }
    """
    import re
    import json
    import httpx
    from database import get_item, get_connection

    item = get_item(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")

    old_status = item.get('sold_status', 'unknown')
    old_price = item.get('price')
    old_is_auction = item.get('is_auction', False)
    old_auction_end = item.get('auction_end_time')

    new_status = old_status
    new_price = old_price
    new_is_auction = old_is_auction
    new_auction_end = old_auction_end

    try:
        url = item.get('url', '')

        if "mercari" in url:
            # Extract item ID from URL
            match = re.search(r'/item/(m\d+)', url)
            if match:
                mercari_item_id = match.group(1)
                # Use mercari library - fast API call, no browser needed
                from mercari_api import get_item_info
                mercari_item = get_item_info(mercari_item_id)
                status = mercari_item.status

                # Mercari returns lowercase status like "on_sale", "trading", "sold_out"
                if status == "on_sale":
                    new_status = "available"
                elif status == "trading":
                    new_status = "trading"
                elif status == "sold_out":
                    new_status = "sold"
                else:
                    new_status = status or "unknown"

                # Update price if available
                if hasattr(mercari_item, 'price') and mercari_item.price:
                    new_price = mercari_item.price

        elif "yahoo" in url or "auctions" in url:
            # Yahoo: Fetch HTML and parse __NEXT_DATA__
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    url,
                    headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"},
                    timeout=10.0,
                    follow_redirects=True
                )
                html = response.text

            # Parse __NEXT_DATA__ from HTML
            match = re.search(r'<script id="__NEXT_DATA__"[^>]*>([^<]+)</script>', html)
            if match:
                data = json.loads(match.group(1))
                item_data = (data.get("props", {})
                                .get("pageProps", {})
                                .get("initialState", {})
                                .get("item", {})
                                .get("detail", {})
                                .get("item", {}))
                if item_data:
                    status = item_data.get("status", "")

                    # Yahoo status: "open" = available, "closed" = sold/ended
                    if status == "open":
                        new_status = "available"
                    elif status == "closed":
                        new_status = "sold"
                    elif status == "cancelled":
                        new_status = "cancelled"
                    else:
                        new_status = status or "unknown"

                    # Price - use current price
                    item_price = item_data.get("price")
                    if item_price:
                        new_price = item_price

                    # Auction vs Buy-It-Now classification:
                    # - bidorbuy present AND equals price = Buy It Now only
                    # - bidorbuy present AND > price = Auction OR Buy It Now
                    # - bidorbuy absent = Auction only
                    # - bids > 0 = Has active bids (definitely auction)
                    price = item_data.get("price", 0)
                    bidorbuy = item_data.get("bidorbuy")
                    bids = item_data.get("bids", 0)

                    if bidorbuy is not None and bidorbuy == price:
                        # Buy It Now only (price equals bidorbuy)
                        new_is_auction = False
                    else:
                        # Auction only OR Auction+BuyItNow
                        new_is_auction = True

                    # End time
                    end_time = item_data.get("endTime")
                    if end_time:
                        if isinstance(end_time, (int, float)):
                            new_auction_end = int(end_time / 1000) if end_time > 9999999999 else int(end_time)
                        elif isinstance(end_time, str):
                            try:
                                from datetime import datetime
                                dt = datetime.fromisoformat(end_time.replace('Z', '+00:00'))
                                new_auction_end = int(dt.timestamp())
                            except:
                                pass

        # Update database if anything changed
        changed = (new_status != old_status or new_price != old_price or
                   new_is_auction != old_is_auction or new_auction_end != old_auction_end)

        if changed:
            conn = get_connection()
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE items
                SET sold_status = ?, price = ?, is_auction = ?, auction_end_time = ?
                WHERE id = ?
            """, (new_status, new_price, 1 if new_is_auction else 0, new_auction_end, item_id))
            conn.commit()
            conn.close()

        return {
            "changed": changed,
            "sold_status": new_status,
            "price": new_price,
            "is_auction": new_is_auction,
            "auction_end_time": new_auction_end
        }

    except Exception as e:
        print(f"Error refreshing status for item {item_id}: {e}")
        return {
            "changed": False,
            "sold_status": old_status,
            "price": old_price,
            "is_auction": old_is_auction,
            "auction_end_time": old_auction_end,
            "error": str(e)
        }


# --- Import ---

class ImportUrlsRequest(BaseModel):
    urls: List[str]


@app.post("/api/items/import")
async def import_items_endpoint(request: ImportUrlsRequest):
    """
    Import items from URLs. Parses Mercari and Yahoo Auction URLs,
    fetches item details, and saves them directly to the saved list.
    """
    import re
    import json
    import httpx
    from database import import_item

    results = []

    for url in request.urls:
        url = url.strip()
        if not url:
            continue

        try:
            source = None
            source_id = None

            # Parse Mercari URL
            mercari_match = re.search(r'mercari\.com/(?:jp/)?item/(m\d+)', url)
            if mercari_match:
                source = "mercari"
                source_id = mercari_match.group(1)

            # Parse Yahoo Auction URL
            yahoo_match = re.search(r'auctions\.yahoo\.co\.jp/(?:jp/)?auction/([a-zA-Z]\d+)', url)
            if yahoo_match:
                source = "yahoo"
                source_id = yahoo_match.group(1)

            if not source or not source_id:
                results.append({"url": url, "success": False, "error": "Unrecognized URL format"})
                continue

            # Fetch item details
            title = None
            price = None
            image_url = None
            images = []
            description = None
            is_auction = False
            auction_end_time = None
            sold_status = "unknown"

            if source == "mercari":
                # Direct API call to avoid localization/currency issues
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
                    r = requests.get(api_url, headers=headers, params={"id": source_id}, timeout=15)
                    r.raise_for_status()
                    item_data = r.json().get('data', {})

                    title = item_data.get('name')
                    price = item_data.get('price')
                    images = item_data.get('photos', [])
                    image_url = images[0] if images else None
                    description = item_data.get('description')

                    # Status
                    status = item_data.get('status', '')
                    if status == "on_sale":
                        sold_status = "available"
                    elif status == "trading":
                        sold_status = "trading"
                    elif status == "sold_out":
                        sold_status = "sold"
                    else:
                        sold_status = status or "unknown"
                except Exception as e:
                    results.append({"url": url, "success": False, "error": f"Mercari API error: {str(e)}"})
                    continue

            elif source == "yahoo":
                # Fetch Yahoo page and parse __NEXT_DATA__
                try:
                    async with httpx.AsyncClient() as client:
                        response = await client.get(
                            url,
                            headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"},
                            timeout=15.0,
                            follow_redirects=True
                        )
                        html = response.text

                    match = re.search(r'<script id="__NEXT_DATA__"[^>]*>([^<]+)</script>', html)
                    if match:
                        data = json.loads(match.group(1))
                        item_data = (data.get("props", {})
                                        .get("pageProps", {})
                                        .get("initialState", {})
                                        .get("item", {})
                                        .get("detail", {})
                                        .get("item", {}))
                        if item_data:
                            title = item_data.get("title")
                            price = item_data.get("price")
                            img_list = item_data.get("img", [])
                            images = [img.get("image") for img in img_list if img.get("image")][:20]
                            image_url = images[0] if images else None

                            desc_list = item_data.get("description", [])
                            if desc_list and isinstance(desc_list, list):
                                description = "\n".join(desc_list)
                            else:
                                description = title

                            # Auction vs Buy-It-Now
                            bidorbuy = item_data.get("bidorbuy")
                            item_price = item_data.get("price", 0)
                            if bidorbuy is not None and bidorbuy == item_price:
                                is_auction = False
                            else:
                                is_auction = True

                            # End time
                            end_time = item_data.get("endTime")
                            if end_time:
                                if isinstance(end_time, (int, float)):
                                    auction_end_time = int(end_time / 1000) if end_time > 9999999999 else int(end_time)
                                elif isinstance(end_time, str):
                                    try:
                                        from datetime import datetime
                                        dt = datetime.fromisoformat(end_time.replace('Z', '+00:00'))
                                        auction_end_time = int(dt.timestamp())
                                    except:
                                        pass

                            # Status
                            status = item_data.get("status", "")
                            if status == "open":
                                sold_status = "available"
                            elif status == "closed":
                                sold_status = "sold"
                            elif status == "cancelled":
                                sold_status = "cancelled"
                            else:
                                sold_status = status or "unknown"
                        else:
                            results.append({"url": url, "success": False, "error": "Could not parse Yahoo data"})
                            continue
                    else:
                        results.append({"url": url, "success": False, "error": "Could not find __NEXT_DATA__"})
                        continue
                except Exception as e:
                    results.append({"url": url, "success": False, "error": f"Yahoo fetch error: {str(e)}"})
                    continue

            # Normalize URL
            if source == "mercari":
                normalized_url = f"https://jp.mercari.com/item/{source_id}"
            else:
                normalized_url = f"https://auctions.yahoo.co.jp/jp/auction/{source_id}"

            # Import to database
            item_id = import_item(
                source=source,
                source_id=source_id,
                url=normalized_url,
                title=title,
                price=price,
                image_url=image_url,
                images=images,
                description=description,
                is_auction=is_auction,
                auction_end_time=auction_end_time,
                sold_status=sold_status
            )

            results.append({
                "url": url,
                "success": True,
                "item_id": item_id,
                "title": title,
                "source": source
            })

        except Exception as e:
            results.append({"url": url, "success": False, "error": str(e)})

    successful = sum(1 for r in results if r.get("success"))
    return {
        "status": "ok",
        "imported": successful,
        "total": len(results),
        "results": results
    }


# --- Decks ---

@app.get("/api/decks")
async def get_decks_endpoint():
    """Get all decks with keyword and item counts."""
    decks = get_decks()
    return {"decks": decks}


@app.get("/api/decks-with-keywords")
async def get_decks_with_keywords_endpoint():
    """Get all decks with their keywords for hierarchical filter menu."""
    decks = get_decks_with_keywords()
    return {"decks": decks}


@app.post("/api/decks")
async def create_deck_endpoint(request: DeckCreate):
    """Create a new deck."""
    deck_id = create_deck(request.name)
    return {"status": "ok", "deck_id": deck_id}


@app.get("/api/decks/{deck_id}")
async def get_deck_endpoint(deck_id: int):
    """Get a single deck with full sizing profile."""
    deck = get_deck(deck_id)
    if not deck:
        raise HTTPException(status_code=404, detail="Deck not found")
    return deck


@app.put("/api/decks/{deck_id}")
async def update_deck_endpoint(deck_id: int, request: DeckUpdate):
    """Update deck name."""
    update_deck(deck_id, request.name)
    return {"status": "ok"}


@app.delete("/api/decks/{deck_id}")
async def delete_deck_endpoint(deck_id: int):
    """Delete a deck. Keywords are moved to Default deck."""
    if deck_id == 1:
        raise HTTPException(status_code=400, detail="Cannot delete Default deck")
    delete_deck(deck_id)
    return {"status": "ok"}


@app.put("/api/decks/reorder")
async def reorder_decks_endpoint(request: DeckReorderRequest):
    """Reorder decks by setting their priorities."""
    reorder_decks(request.deck_ids)
    return {"status": "ok"}


@app.put("/api/decks/{deck_id}/sizing")
async def update_deck_sizing_endpoint(deck_id: int, request: DeckSizingUpdate):
    """Update deck sizing profile."""
    sizing = {}
    for key in ['a', 'b', 'c', 'd', 'e', 'f', 'g', 'h']:
        measurement = getattr(request, key, None)
        if measurement:
            sizing[key] = {'op': measurement.op, 'val': measurement.val}
    update_deck_sizing(deck_id, sizing)
    return {"status": "ok"}


@app.get("/api/decks/{deck_id}/keywords")
async def get_deck_keywords_endpoint(deck_id: int):
    """Get all keywords in a deck."""
    keywords = get_keywords_by_deck(deck_id)
    return {"keywords": keywords}


# --- Keywords ---

@app.get("/api/keywords")
async def get_keywords_endpoint():
    keywords = get_keywords()
    return {"keywords": keywords}


@app.post("/api/keywords")
async def add_keyword_endpoint(request: KeywordCreate):
    keyword_id = add_keyword(request.keyword, request.source, request.deck_id)
    return {"status": "ok", "keyword_id": keyword_id}


@app.put("/api/keywords/{keyword_id}")
async def update_keyword_endpoint(keyword_id: int, request: KeywordUpdate):
    """Update keyword text, source, and whitelist.

    Blocklist changes are handled separately via DELETE /api/keywords/{id}/blocklist.
    """
    conn = get_connection()
    cursor = conn.cursor()

    # Update keyword text and source
    cursor.execute(
        "UPDATE keywords SET keyword = ?, source = ? WHERE id = ?",
        (request.keyword, request.source or 'all', keyword_id)
    )

    # Handle whitelist updates (if provided)
    if request.whitelist is not None:
        # Clear existing whitelist and add new ones
        cursor.execute("DELETE FROM keyword_whitelist WHERE keyword_id = ?", (keyword_id,))
        for category_id in request.whitelist:
            cursor.execute(
                "INSERT INTO keyword_whitelist (keyword_id, category_id) VALUES (?, ?)",
                (keyword_id, category_id)
            )

    conn.commit()
    conn.close()
    return {"status": "ok"}


@app.delete("/api/keywords/{keyword_id}")
async def delete_keyword_endpoint(keyword_id: int):
    delete_keyword(keyword_id)
    return {"status": "ok"}


@app.get("/api/keywords/unseen-counts")
async def get_keywords_unseen_counts_endpoint():
    """Get all keywords with their unseen item counts."""
    keywords = get_keywords_with_unseen_counts()
    return {"keywords": keywords}


@app.post("/api/keywords/{keyword_id}/prioritize")
async def prioritize_keyword_endpoint(keyword_id: int):
    """Set keyword to highest priority (top of deck)."""
    prioritize_keyword(keyword_id)
    return {"status": "ok"}


@app.post("/api/keywords/{keyword_id}/deprioritize")
async def deprioritize_keyword_endpoint(keyword_id: int):
    """Set keyword to lowest priority (bottom of deck)."""
    deprioritize_keyword(keyword_id)
    return {"status": "ok"}


class KeywordReorderRequest(BaseModel):
    keyword_ids: List[int]


@app.put("/api/keywords/reorder")
async def reorder_keywords_endpoint(request: KeywordReorderRequest):
    """Reorder keywords by setting their priorities based on position in list."""
    reorder_keywords(request.keyword_ids)
    return {"status": "ok"}


@app.put("/api/keywords/{keyword_id}/deck")
async def move_keyword_to_deck_endpoint(keyword_id: int, request: KeywordDeckUpdate):
    """Move a keyword to a different deck."""
    move_keyword_to_deck(keyword_id, request.deck_id)
    return {"status": "ok"}


@app.get("/api/keywords/{keyword_id}/blocklist")
async def get_keyword_blocklist_endpoint(keyword_id: int):
    """Get all blocked categories for a keyword."""
    entries = get_keyword_blocklist(keyword_id)
    return {"entries": entries, "count": len(entries)}


class RemoveBlocklistRequest(BaseModel):
    category_id: str


@app.delete("/api/keywords/{keyword_id}/blocklist")
async def remove_keyword_blocklist_endpoint(keyword_id: int, request: RemoveBlocklistRequest):
    """Remove a category from a keyword's blocklist."""
    removed = remove_from_keyword_blocklist(keyword_id, request.category_id)
    return {"status": "ok", "removed": removed, "category_id": request.category_id}


# --- Categories ---

@app.get("/api/items/{item_id}/category-ancestors")
async def get_item_category_ancestors_endpoint(item_id: int):
    """Get category hierarchy for an item (root to leaf)."""
    item = get_item(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")

    category_id = item.get("category_id")
    if not category_id:
        return {"ancestors": [], "category_id": None}

    ancestors = get_category_ancestors_with_names(category_id)
    return {"ancestors": ancestors, "category_id": category_id}


class HideCategoryRequest(BaseModel):
    category_id: str
    keyword_id: Optional[int] = None  # None = apply to all keywords


@app.post("/api/categories/hide")
async def hide_category_endpoint(request: HideCategoryRequest):
    """
    Hide all items in a category (and its descendants).
    Also adds to blocklist for future scrapes.
    """
    # Hide existing items
    hidden_count = hide_items_by_category(request.category_id, request.keyword_id)

    # Add to blocklist for future scrapes
    add_to_blocklist(request.category_id, request.keyword_id)

    return {
        "status": "ok",
        "hidden_count": hidden_count,
        "category_id": request.category_id,
        "keyword_id": request.keyword_id
    }


@app.get("/api/categories/search")
async def search_categories_endpoint(keyword: str):
    """
    Search for categories by doing a cursory search on Mercari and Yahoo,
    then extracting parent category hierarchies.
    Returns only top-level categories and their immediate children.
    """
    import httpx
    from collections import Counter, defaultdict
    from urllib.parse import quote
    from database import get_connection, get_category_ancestors_with_names

    # Track leaf categories found in search results
    leaf_categories = []

    try:
        # Search Mercari API for category data
        import requests
        from mercari_api import generate_dpop

        search_url = "https://api.mercari.jp/v2/entities:search"
        dpop = generate_dpop(uuid="Mercari Python Bot", method="POST", url=search_url)

        headers = {
            'DPOP': dpop,
            'X-Platform': 'web',
            'Accept': '*/*',
            'Content-Type': 'application/json; charset=utf-8',
            'User-Agent': 'python-mercari',
        }

        data = {
            "userId": "CATEGORY_SEARCH",
            "pageSize": 50,
            "pageToken": "v1:0",
            "searchCondition": {
                "keyword": keyword,
                "sort": "SORT_CREATED_TIME",
                "order": "ORDER_DESC",
                "status": ["STATUS_ON_SALE"],
            },
            "defaultDatasets": ["DATASET_TYPE_MERCARI"]
        }

        r = requests.post(search_url, headers=headers, json=data, timeout=15)
        if r.status_code == 200:
            resp = r.json()
            items = resp.get("items", [])

            for item in items:
                cat_id = item.get("categoryId")
                if cat_id:
                    full_id = f"mercari:{cat_id}"
                    # Mercari items include parent_category_name and item_category
                    # which gives us the hierarchy directly
                    parents = item.get("parentCategoryNames", [])
                    leaf_name = item.get("itemCategoryName", "")

                    if parents:
                        # parents[0] is top-level, parents[1] is second-level if exists
                        leaf_categories.append({
                            "leaf_id": full_id,
                            "source": "mercari",
                            "hierarchy": parents + ([leaf_name] if leaf_name else [])
                        })

    except Exception as e:
        print(f"[CategorySearch] Mercari error: {e}")

    try:
        # Search Yahoo Auctions for category data
        yahoo_url = f"https://auctions.yahoo.co.jp/search/search?p={quote(keyword)}&n=50"

        async with httpx.AsyncClient() as client:
            response = await client.get(
                yahoo_url,
                headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"},
                timeout=15.0,
                follow_redirects=True
            )

            if response.status_code == 200:
                from bs4 import BeautifulSoup
                soup = BeautifulSoup(response.text, 'html.parser')

                # Extract category data from product elements
                products = soup.select('[data-auction-category]')
                for product in products:
                    cat = product.get('data-auction-category')
                    if cat:
                        full_id = f"yahoo:{cat}"
                        leaf_categories.append({
                            "leaf_id": full_id,
                            "source": "yahoo",
                            "hierarchy": None  # Will need to fetch
                        })

    except Exception as e:
        print(f"[CategorySearch] Yahoo error: {e}")

    # Build hierarchical structure: top-level -> children -> count
    # Structure: { top_level_name: { "id": ..., "children": { child_name: { "id": ..., "count": N } } } }
    hierarchy = defaultdict(lambda: {"id": None, "source": None, "count": 0, "children": defaultdict(lambda: {"id": None, "count": 0})})

    for leaf in leaf_categories:
        source = leaf["source"]

        if leaf["hierarchy"]:
            # We have hierarchy from Mercari
            h = leaf["hierarchy"]
            if len(h) >= 1:
                top_name = h[0]
                hierarchy[top_name]["source"] = source
                hierarchy[top_name]["count"] += 1

                if len(h) >= 2:
                    child_name = h[1]
                    hierarchy[top_name]["children"][child_name]["count"] += 1
        else:
            # For Yahoo, fetch hierarchy from database/API
            ancestors = get_category_ancestors_with_names(leaf["leaf_id"])
            if ancestors:
                if len(ancestors) >= 1:
                    top = ancestors[0]
                    top_name = top.get("name", top.get("id", "Unknown"))
                    hierarchy[top_name]["id"] = top.get("id")
                    hierarchy[top_name]["source"] = source
                    hierarchy[top_name]["count"] += 1

                    if len(ancestors) >= 2:
                        child = ancestors[1]
                        child_name = child.get("name", child.get("id", "Unknown"))
                        hierarchy[top_name]["children"][child_name]["id"] = child.get("id")
                        hierarchy[top_name]["children"][child_name]["count"] += 1

    # Convert to list format, sorted by count
    result_categories = []
    for top_name, top_data in sorted(hierarchy.items(), key=lambda x: -x[1]["count"]):
        top_entry = {
            "name": top_name,
            "id": top_data["id"],
            "source": top_data["source"],
            "count": top_data["count"],
            "children": []
        }

        for child_name, child_data in sorted(top_data["children"].items(), key=lambda x: -x[1]["count"]):
            top_entry["children"].append({
                "name": child_name,
                "id": child_data["id"],
                "count": child_data["count"]
            })

        result_categories.append(top_entry)

    return {"categories": result_categories}


# --- Scraping ---

def run_scrape(keyword_id: int = None, keyword: str = None, source: str = "both"):
    """Background scraping task."""
    global scrape_status
    scrape_status = {"running": True, "message": "Scraping in progress..."}
    try:
        if keyword_id and keyword:
            result = scrape_keyword(keyword_id, keyword, source)
            scrape_status = {"running": False, "message": f"Done! Scraped {result['scraped']}, saved {result['saved']} new items."}
        else:
            result = scrape_all_keywords()
            scrape_status = {"running": False, "message": f"Done! Total: {result['total_scraped']} scraped, {result['total_saved']} new."}
    except Exception as e:
        scrape_status = {"running": False, "message": f"Error: {str(e)}"}


@app.post("/api/scrape")
async def trigger_scrape(background_tasks: BackgroundTasks):
    """Trigger scraping for all keywords."""
    if scrape_status["running"]:
        return {"status": "already_running", "message": scrape_status["message"]}
    background_tasks.add_task(run_scrape)
    return {"status": "started", "message": "Scraping started in background"}


@app.post("/api/scrape/{keyword_id}")
async def trigger_scrape_keyword(keyword_id: int, background_tasks: BackgroundTasks):
    """Trigger scraping for a specific keyword."""
    keywords = get_keywords()
    kw = next((k for k in keywords if k["id"] == keyword_id), None)
    if not kw:
        raise HTTPException(status_code=404, detail="Keyword not found")
    if scrape_status["running"]:
        return {"status": "already_running", "message": scrape_status["message"]}
    background_tasks.add_task(run_scrape, keyword_id, kw["keyword"], kw["source"])
    return {"status": "started", "message": f"Scraping '{kw['keyword']}' in background"}


@app.get("/api/scrape/status")
async def get_scrape_status():
    return scrape_status


@app.post("/api/mock")
async def add_mock_data(request: MockDataRequest):
    """Add mock data for testing."""
    from database import add_keyword as db_add_keyword, save_scraped_items as db_save_items
    keyword_id = db_add_keyword(request.keyword, "both")
    items = generate_mock_items(request.keyword, request.count)
    saved = db_save_items(items, keyword_id)
    return {"status": "ok", "saved": saved}


@app.get("/api/check-sold")
async def check_sold_status(url: str):
    """
    Check if an item is sold/unavailable.
    - Mercari: Uses mercari library (fast API calls, no browser)
    - Yahoo: Fetches HTML and parses __NEXT_DATA__
    Returns: { available: bool, status: str }
    """
    import re
    import json
    import httpx

    result = {"available": False, "status": "unknown"}

    try:
        if "mercari" in url:
            # Extract item ID from URL
            match = re.search(r'/item/(m\d+)', url)
            if match:
                item_id = match.group(1)
                # Use mercari library - fast API call, no browser needed
                from mercari_api import get_item_info
                item = get_item_info(item_id)
                status = item.status

                if status == "on_sale":
                    result["available"] = True
                    result["status"] = "available"
                elif status == "trading":
                    result["available"] = False
                    result["status"] = "trading"
                elif status == "sold_out":
                    result["available"] = False
                    result["status"] = "sold"
                else:
                    result["status"] = status or "unknown"

        elif "yahoo" in url or "auctions" in url:
            # Yahoo: Fetch HTML and parse __NEXT_DATA__
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    url,
                    headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"},
                    timeout=10.0,
                    follow_redirects=True
                )
                html = response.text

            # Parse __NEXT_DATA__ from HTML
            match = re.search(r'<script id="__NEXT_DATA__"[^>]*>([^<]+)</script>', html)
            if match:
                data = json.loads(match.group(1))
                item_data = (data.get("props", {})
                                .get("pageProps", {})
                                .get("initialState", {})
                                .get("item", {})
                                .get("detail", {})
                                .get("item", {}))
                if item_data:
                    status = item_data.get("status", "")
                    is_closed = item_data.get("isClosed", False)
                    close_type = item_data.get("closeType", "")

                    if status == "open" and not is_closed:
                        result["available"] = True
                        result["status"] = "available"
                    elif status in ("closed", "cancelled", "sold") or is_closed:
                        if status == "cancelled" or close_type == "cancelled":
                            result["status"] = "cancelled"
                        elif status == "sold" or close_type == "sold":
                            result["status"] = "sold"
                        else:
                            result["status"] = "ended"
                        result["available"] = False
                    else:
                        result["status"] = status or "unknown"

    except Exception as e:
        result["status"] = f"error: {str(e)}"

    return result


@app.get("/api/proxy-html")
async def proxy_html(url: str):
    """
    Lightweight HTML proxy for Yahoo Auctions.
    Just fetches and returns raw HTML - client parses __NEXT_DATA__.
    No Playwright needed, just httpx/aiohttp.
    """
    import httpx
    from fastapi.responses import PlainTextResponse

    if "auctions.yahoo.co.jp" not in url:
        return PlainTextResponse("Only Yahoo Auctions URLs allowed", status_code=400)

    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                url,
                headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"},
                timeout=10.0,
                follow_redirects=True
            )
            return PlainTextResponse(response.text)
    except Exception as e:
        return PlainTextResponse(f"Error: {str(e)}", status_code=500)


# --- HTML Routes ---

@app.get("/", response_class=HTMLResponse)
async def home_page(request: Request, user: dict = Depends(get_current_user_optional)):
    """Home page - landing for guests, dashboard for logged in users."""
    if user:
        return templates.TemplateResponse("dashboard.html", {
            "request": request,
            "active_page": "dashboard",
            "user": user
        })
    # Guest: show landing page
    return templates.TemplateResponse("landing.html", {"request": request})


@app.get("/onboarding", response_class=HTMLResponse)
async def onboarding_page(request: Request, user: dict = Depends(get_current_user)):
    """Username selection page for new users."""
    if user.get('username'):
        # Already has username, redirect to dashboard
        return RedirectResponse(url="/")
    return templates.TemplateResponse("onboarding.html", {
        "request": request,
        "user": user
    })


@app.get("/admin/invites", response_class=HTMLResponse)
async def admin_invites_page(request: Request, user: dict = Depends(get_current_user)):
    """Admin page to manage invite requests."""
    if user['id'] != 1:
        raise HTTPException(status_code=403, detail="Not authorized")
    requests = get_pending_invite_requests()
    return templates.TemplateResponse("admin_invites.html", {
        "request": request,
        "user": user,
        "pending_requests": requests
    })


@app.get("/new", response_class=HTMLResponse)
async def new_items_page(request: Request, user: dict = Depends(get_current_user_optional)):
    """Reroll page - requires auth or demo mode."""
    demo = is_demo_mode(request)
    if not user and not demo:
        return RedirectResponse(url="/")
    return templates.TemplateResponse("reroll.html", {
        "request": request,
        "active_page": "new",
        "user": user,
        "demo_mode": demo
    })


@app.get("/keywords", response_class=HTMLResponse)
async def keywords_page_redirect(request: Request, user: dict = Depends(get_current_user)):
    # Redirect to first deck's keywords
    return RedirectResponse(url="/keywords/1", status_code=302)


@app.get("/keywords/{deck_id}", response_class=HTMLResponse)
async def keywords_page(request: Request, deck_id: int, user: dict = Depends(get_current_user)):
    """Keywords page for a specific deck."""
    deck = get_deck(deck_id)
    if not deck:
        return RedirectResponse(url="/keywords/1", status_code=302)
    return templates.TemplateResponse("keywords.html", {
        "request": request,
        "active_page": "keywords",
        "deck_id": deck_id,
        "deck": deck,
        "active_deck_id": deck_id,
        "user": user
    })


@app.get("/decks", response_class=HTMLResponse)
async def decks_page(request: Request, user: dict = Depends(get_current_user)):
    # Redirect to keywords for first deck
    return RedirectResponse(url="/keywords/1", status_code=302)


@app.get("/review", response_class=HTMLResponse)
async def review_page(request: Request, user: dict = Depends(get_current_user)):
    return templates.TemplateResponse("review.html", {
        "request": request,
        "active_page": "review",
        "user": user
    })


@app.get("/saved", response_class=HTMLResponse)
async def saved_page(request: Request, user: dict = Depends(get_current_user)):
    return templates.TemplateResponse("deck.html", {
        "request": request,
        "active_page": "saved",
        "user": user
    })


# Demo mode entry point
@app.get("/demo", response_class=HTMLResponse)
async def start_demo(request: Request):
    """Start demo mode and redirect to reroll page."""
    response = RedirectResponse(url="/new")
    response.set_cookie(
        key="demo_mode",
        value="true",
        httponly=False,
        max_age=60*60  # 1 hour
    )
    return response


# =====================================================
# TEMPORARY: Database upload endpoint (remove after use)
# =====================================================

@app.post("/admin/upload-db")
async def upload_database(
    file: UploadFile = File(...),
    x_upload_secret: str = Header(...)
):
    """
    Upload database file. Protected by secret header.
    Usage: curl -X POST -H "X-Upload-Secret: YOUR_SECRET" -F "file=@shoppinghelper.db" https://tapiere.com/admin/upload-db
    """
    if x_upload_secret != UPLOAD_SECRET:
        raise HTTPException(status_code=403, detail="Invalid secret")

    from database import DB_PATH

    # Backup existing if present
    if DB_PATH.exists():
        import shutil
        shutil.copy(DB_PATH, DB_PATH.with_suffix('.db.bak'))

    # Write uploaded file
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    content = await file.read()
    with open(DB_PATH, 'wb') as f:
        f.write(content)

    return {"status": "ok", "size": len(content), "path": str(DB_PATH)}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
