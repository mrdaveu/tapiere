# TAPIERE - Architecture & Design Document

## Product Vision

**TAPIERE** - Personal shopping assistant for Japanese marketplaces (Mercari, Yahoo Auctions Japan).

- **Fashion-first but category-agnostic** - optimized for clothing/textiles but works for any item type
- **Multi-user beta** - Invite-only registration with magic link auth
- **Public deck sharing** - Users can share their saved items at `/u/username/deck-slug`
- **Multi-marketplace expansion planned** - currently JP (Mercari, Yahoo), future: Rakuma, Surugaya, eBay, Grailed, Depop

---

## Deployment

### Production
- **Hosting**: Railway (https://tapiere.com)
- **Database**: SQLite with persistent volume at `/app/data/shoppinghelper.db`
- **Environment Variables**:
  - `RESEND_API_KEY` - For magic link emails
  - `BASE_URL` - https://tapiere.com
  - `DATABASE_PATH` - /app/data/shoppinghelper.db
  - `UPLOAD_SECRET` - For admin DB upload endpoint (temporary)

### Local Development
```bash
cd /Users/u/Desktop/shoppinghelper
source venv/bin/activate
uvicorn app:app --reload --port 8000
```
Then visit http://localhost:8000

---

## Core Workflow

Users have **established keywords** (brands, styles) they continuously monitor:

1. **Reroll View** (`/new`) - Browse 5 items at a time, press 1-5 to save, Space to load next batch
2. **Category Blocklists** - Essential for filtering irrelevant items (e.g., women's skirts when browsing menswear)
3. **Saved Items** (`/saved`) - Review, rate (1-5 stars), organize into decks
4. **Keywords** (`/keywords/{deck_id}`) - Manage search terms, trigger scrapes, view blocklists

### Keyboard Shortcuts (Reroll View)
- `1-5` - Save item at that position
- `Space` - Load next batch (marks unsaved as seen)
- `Backspace` - Go back to previous batch
- `U` - Skip/deprioritize current keyword

---

## Architecture Overview

### Backend (FastAPI + SQLite)

| File | Purpose |
|------|---------|
| `app.py` | FastAPI app with 50+ REST endpoints, auth middleware |
| `database.py` | Schema, migrations, all DB queries, multi-user support |
| `scraper.py` | Mercari (API) + Yahoo (HTTP) scrapers |
| `detail_scraper.py` | Individual item page scraping via httpx (no browser) |
| `mercari_api.py` | Local Mercari API client with DPOP auth (pure Python) |
| `email_service.py` | Resend API via httpx for magic link emails |
| `cli.py` | CLI for init, serve, scrape commands |

### Frontend (Jinja2 Templates)

| Template | Purpose |
|----------|---------|
| `base.html` | Layout with navbar, deck sidebar, user info, logout |
| `landing.html` | Public landing page with invite request modal |
| `onboarding.html` | Username selection for new users |
| `reroll.html` | Main 5-card browsing with keyboard shortcuts, preloading |
| `deck.html` | Saved items grid with filters and image gallery |
| `keywords.html` | Keyword management, blocklist tags, scrape controls |
| `dashboard.html` | Stats overview |
| `review.html` | Item rating interface |
| `admin_invites.html` | Admin page for invite approval |
| `public_deck.html` | Public view of shared decks |

### Database Schema

```sql
-- Multi-user auth
users (id, email UNIQUE, username UNIQUE, created_at, last_login_at)
sessions (id, user_id, token UNIQUE, created_at)
magic_links (id, email, token UNIQUE, created_at, expires_at, used_at, link_type)
invite_requests (id, email UNIQUE, reason, status, created_at, approved_at, approved_by)

-- Deck sharing
deck_shares (deck_id UNIQUE, share_slug, is_public, created_at)

-- Core tables (all have user_id for multi-tenancy)
items (id, user_id, source, source_id, title, price, image_url, url, description,
       images JSON, seen, saved, stars, keyword_id, sold_status, is_auction,
       auction_end_time, category_id, hidden, in_cart, fit_score)

keywords (id, user_id, keyword, source, deck_id, priority, item_count, last_scraped_at)

decks (id, user_id, name, priority, size_a_op, size_a_val, ... size_h_op, size_h_val)

-- Filtering system
categories (id, source, name, parent_id, path)  -- hierarchy cache
category_blocklist (id, user_id, category_id, keyword_id, created_at)  -- global + per-keyword
```

---

## Authentication System

### Magic Link Flow
1. User enters email at `/` (landing page)
2. `POST /api/auth/login` creates magic link (24h validity)
3. Email sent via Resend API (or logged to console in dev)
4. User clicks link → `GET /auth/verify?token=X`
5. If new user → redirect to `/onboarding` for username selection
6. Session cookie set (HttpOnly, 1-year expiry)

### Invite System
- New users must request invite at landing page
- Admin (user #1) approves at `/admin/invites`
- Approval sends magic link email automatically
- Owner email: `ulokwa@gmail.com`

### Demo Mode
- Guests can try UI without account
- `GET /demo` sets `demo_mode=true` cookie
- Demo uses items from `static/demo-items.json`
- Saves go to localStorage (not server)

---

## Key Features

### Smart Scraping
- Sorts by **newest first**
- Stops after **5 consecutive existing items** (fast incremental updates)
- Background detail scraping queues when user saves an item
- Uses httpx for Yahoo, local mercari_api.py for Mercari (no browser needed)

### Preloading (Reroll View)
- Client preloads 2 batches ahead for instant navigation
- Images preloaded in hidden container
- Implemented in `reroll.html` JavaScript

### 5-Layer Filtering System

1. **Blocklist check at scrape time** → sets `hidden=TRUE`
2. **Hidden flag filtered** in `get_next_unseen()` query
3. **Query-level filters** (seen, saved, hidden)
4. **Saved items filters** (stars, deck, keyword, cart)
5. **Future: LLM fit scoring** against deck sizing profiles

### Decks (Folders)
- Organizational hierarchy for keywords
- Each deck can have sizing profile (measurements a-h)
- Full CRUD control required

### Category Blocklists
- **Essential feature** - filters entire category trees
- Can be **global** or **per-keyword** scoped
- **Recursive**: blocking parent blocks all descendants

### Public Deck Sharing
- Toggle share on/off per deck
- Public URL: `/u/{username}/{deck-slug}`
- Shows items + star ratings (no fit scores)

---

## Dependencies & Build Notes

### Pure Python (No Native Dependencies)
The app uses pure Python packages to avoid cffi/libffi build issues on Railway:

- `ecdsa` - Pure Python ECDSA for Mercari DPOP authentication
- `httpx` - HTTP client (used instead of requests in some places)
- No `mercari` package - replaced with local `mercari_api.py`
- No `resend` package - replaced with direct httpx calls to Resend API
- No `playwright` - detail scraping uses httpx for both Yahoo and Mercari

### requirements.txt Key Packages
```
fastapi, uvicorn, jinja2, python-multipart
beautifulsoup4, lxml, httpx
Pillow, numpy, tqdm
openai (for future LLM features)
ecdsa (for Mercari DPOP auth)
```

---

## Data Flow Diagrams

### Scraping Pipeline
```
┌─────────────┐     ┌─────────────┐     ┌──────────────┐     ┌─────────────┐
│  Keywords   │────▶│   Scraper   │────▶│  database.py │────▶│   items     │
│  (priority) │     │ (Mercari/   │     │ save_scraped │     │ (hidden=?)  │
└─────────────┘     │  Yahoo)     │     │   _items()   │     └─────────────┘
                    └─────────────┘     └──────┬───────┘
                                               │
                                               ▼
                                    ┌──────────────────┐
                                    │ is_category_     │
                                    │ blocked()?       │
                                    │ → sets hidden=T  │
                                    └──────────────────┘
```

### Item Status Refresh (On Click)
```
User clicks saved item
        │
        ▼
POST /api/items/{id}/refresh-status
        │
        ├── Mercari items: mercari_api.get_item_info()
        │   └── Returns status, price updates
        │
        └── Yahoo items: httpx fetch __NEXT_DATA__
            └── Returns status, price, auction end time
        │
        ▼
Updates item in database with new status/price
```

### Detail Scraping (Background)
```
User saves item → queue_detail_scrape()
        │
        ▼
run_detail_scrape_worker() (background thread)
        │
        ├── Mercari: mercari_api.py (API call, no browser)
        │   └── description, images, status
        │
        └── Yahoo: httpx to parse __NEXT_DATA__ JSON
            └── description, images, auction info
        │
        ▼
update_item_details() in database
```

---

## Key API Endpoints Reference

### Auth
- `POST /api/auth/login` - Request magic link
- `GET /auth/verify?token=X` - Verify magic link, create session
- `POST /api/auth/logout` - Clear session
- `POST /api/auth/set-username` - Set username (onboarding)
- `GET /api/auth/me` - Get current user info

### Invites
- `POST /api/invite/request` - Request invite (with optional reason)
- `GET /api/invite/requests` - List pending (admin only)
- `POST /api/invite/approve/{id}` - Approve invite (admin only)

### Items
- `GET /api/items/next?count=5` - Fetch unseen items
- `POST /api/items/seen` - Mark items as seen (bulk)
- `POST /api/items/{id}/save` - Save item
- `POST /api/items/{id}/unsave` - Unsave item
- `PUT /api/items/{id}/rate` - Rate 1-5 stars
- `GET /api/items/saved` - Get saved items with filters
- `POST /api/items/{id}/refresh-status` - Refresh price/status from source

### Scraping
- `POST /api/scrape` - Scrape all keywords
- `POST /api/scrape/{keyword_id}` - Scrape single keyword
- `GET /api/scrape/status` - Check progress

### Categories
- `POST /api/categories/hide` - Add to blocklist
- `GET /api/keywords/{id}/blocklist` - View blocked categories
- `DELETE /api/keywords/{id}/blocklist?category_id=X` - Remove from blocklist

### Decks & Sharing
- `GET /api/decks` - List all decks
- `POST /api/decks` - Create deck
- `PUT /api/decks/{id}/sizing` - Set sizing profile
- `GET /api/decks/{id}/share` - Get share settings
- `PUT /api/decks/{id}/share` - Toggle public sharing
- `GET /u/{username}/{slug}` - Public deck view

### Admin (User #1 Only)
- `GET /admin/invites` - Invite approval page
- `POST /admin/upload-db` - Upload database (temporary endpoint)

---

## Design Philosophy

### Owner Priorities
- **Maintainability > Performance** - main concern is ability to add features/fix bugs
- **Both marketplaces equally important** - Mercari and Yahoo have equal priority
- **Decks hierarchy essential** - users need organizational control
- **Blocklist system is core** - not optional, essential for filtering junk
- **Pure Python dependencies** - avoid native build issues on Railway

### Future Features (Planned)
- **LLM fit scoring** - paid feature, scores items against deck sizing profiles
- **More marketplaces** - Rakuma, Surugaya, eBay, Grailed, Depop
- **Pricing tiers**:
  - Free: 24h auto-refresh, 1 manual refresh, 2 keywords max
  - Paid: 4h auto-refresh, unlimited refreshes/keywords, AI features
- **Client-side scraping** - distribute load via user browsers
