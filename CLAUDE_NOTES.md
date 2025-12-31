# Shopping Helper - Architecture & Design Document

## Product Vision

**Personal shopping assistant** for Japanese marketplaces (Mercari, Yahoo Auctions Japan).

- **Fashion-first but category-agnostic** - optimized for clothing/textiles but works for any item type
- **Multi-marketplace expansion planned** - currently JP (Mercari, Yahoo), future: Rakuma, Surugaya, eBay, Grailed, Depop
- **Building toward a product** - not just personal use; pricing tiers planned

## Core Workflow

Users have **established keywords** (brands, styles) they continuously monitor:

1. **Reroll View** (`/new`) - Browse 5 items at a time, press 1-5 to save, Space to load next batch
2. **Category Blocklists** - Essential for filtering irrelevant items (e.g., women's skirts when browsing menswear)
3. **Saved Items** (`/saved`) - Review, rate (1-5 stars), organize into decks
4. **Keywords** (`/keywords/{deck_id}`) - Manage search terms, trigger scrapes, view blocklists

---

## Architecture Overview

### Backend (FastAPI + SQLite)

| File | Lines | Purpose |
|------|-------|---------|
| `app.py` | ~1,267 | FastAPI app with 40+ REST endpoints |
| `database.py` | ~1,354 | Schema, migrations, all DB queries |
| `scraper.py` | ~501 | Mercari (API) + Yahoo (HTTP) scrapers |
| `detail_scraper.py` | ~461 | Individual item page scraping via API/simulated creds |
| `cli.py` | ~144 | CLI for init, serve, scrape commands |

### Frontend (Jinja2 Templates)

| Template | Purpose |
|----------|---------|
| `base.html` | Layout with navbar, deck sidebar, theme toggle |
| `reroll.html` | Main 5-card browsing interface with keyboard shortcuts |
| `deck.html` | Saved items grid with filters and image gallery |
| `keywords.html` | Keyword management, blocklist tags, scrape controls |
| `dashboard.html` | Stats overview |
| `review.html` | Item rating interface |

### Database Schema

```sql
-- Core tables
items (id, source, source_id, title, price, image_url, url, description,
       images JSON, seen, saved, stars, keyword_id, sold_status, is_auction,
       auction_end_time, category_id, hidden, in_cart, fit_score)

keywords (id, keyword, source, deck_id, priority, item_count, last_scraped_at)

decks (id, name, priority, size_a_op, size_a_val, ... size_h_op, size_h_val)

-- Filtering system
categories (id, source, name, parent_id, path)  -- hierarchy cache
category_blocklist (id, category_id, keyword_id, created_at)  -- global + per-keyword
```

---

## Key Features

### Smart Scraping
- Sorts by **newest first**
- Stops after **5 consecutive existing items** (fast incremental updates)
- Background detail scraping queues when user saves an item

### 5-Layer Filtering System

1. **Blocklist check at scrape time** → sets `hidden=TRUE` in `database.py:738-779`
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
- Location: `database.py:1137-1232`

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

### Category Blocklist Flow
```
User clicks "Hide Category" in reroll.html
           │
           ▼
┌─────────────────────────────┐
│ POST /api/categories/hide   │
│ {category_id, keyword_id?}  │
└──────────────┬──────────────┘
               │
               ▼
┌─────────────────────────────┐
│ hide_items_by_category()    │
│ 1. Find all descendants     │◀──┐
│ 2. INSERT into blocklist    │   │
│ 3. UPDATE items SET hidden  │   │
│    WHERE category matches   │   │
└──────────────┬──────────────┘   │
               │                   │
               ▼                   │
┌─────────────────────────────┐   │
│ Future scrapes check        │   │
│ is_category_blocked()       │───┘
│ before inserting items      │
└─────────────────────────────┘
```

### Item Discovery Flow
```
GET /api/items/next (reroll)
           │
           ▼
┌─────────────────────────────┐
│ get_next_unseen(count=5)    │
│ WHERE:                      │
│   - seen = FALSE            │
│   - saved = FALSE           │
│   - hidden = FALSE          │
│ ORDER BY:                   │
│   - keyword.priority DESC   │
│   - scraped_at DESC         │
└──────────────┬──────────────┘
               │
               ▼
┌─────────────────────────────┐
│ User presses 1-5 to save    │
└──────────────┬──────────────┘
               │
               ▼
┌─────────────────────────────┐
│ POST /api/items/{id}/save   │
│ 1. saved = TRUE             │
│ 2. Queue for detail scrape  │
└──────────────┬──────────────┘
               │
               ▼
┌─────────────────────────────┐
│ Background detail scraper   │
│ - Fetches description       │
│ - Fetches all images        │
│ - (Future) LLM fit scoring  │
└─────────────────────────────┘
```

### Saved Items Filtering
```
GET /api/items/saved?sort=X&filter_type=Y&filter_value=Z
           │
           ▼
┌─────────────────────────────────────────┐
│ filter_type options:                    │
│   - 'cart'    → in_cart = TRUE          │
│   - 'stars'   → stars = Z               │
│   - 'deck'    → keyword.deck_id = Z     │
│   - 'keyword' → keyword_id = Z          │
│                                         │
│ sort options:                           │
│   - 'price' / 'date' / 'stars' / 'fit'  │
└─────────────────────────────────────────┘
```

---

## Design Philosophy

### Owner Priorities
- **Maintainability > Performance** - main concern is ability to add features/fix bugs
- **Both marketplaces equally important** - Mercari and Yahoo have equal priority
- **Decks hierarchy essential** - users need organizational control
- **Blocklist system is core** - not optional, essential for filtering junk

### Future Features (Planned)
- **LLM fit scoring** - paid feature, scores items against deck sizing profiles
- **More marketplaces** - Rakuma, Surugaya, eBay, Grailed, Depop
- **Pricing tiers**:
  - Free: 24h auto-refresh, 1 manual refresh, 2 keywords max
  - Paid: 4h auto-refresh, unlimited refreshes/keywords, AI features
- **Client-side scraping** - distribute load via user browsers

---

## Maintainability Notes

### Identified Complexity Areas
- **Category hierarchy fetching** (`database.py:955-1134`) - Heavy API usage, recursive caching
- **Marketplace parsing** - Mercari vs Yahoo logic scattered across files
- **Background threading** - Manual thread management for detail scraping
- **URL import endpoint** (`app.py:478-682`) - ~200 LOC duplicating parsing logic

### Potential Simplifications (Not Urgent)
- Extract marketplace-specific logic into adapter pattern
- Replace manual threading with task queue (Celery/RQ)
- Consolidate parsing utilities into shared module

---

## Running the App

```bash
cd /Users/u/Desktop/shoppinghelper
source venv/bin/activate
uvicorn app:app --reload --port 8000
```

Then visit http://localhost:8000

---

## Key API Endpoints Reference

### Items
- `GET /api/items/next?count=5` - Fetch unseen items
- `POST /api/items/seen` - Mark items as seen (bulk)
- `POST /api/items/{id}/save` - Save item
- `PUT /api/items/{id}/rate` - Rate 1-5 stars
- `GET /api/items/saved` - Get saved items with filters

### Scraping
- `POST /api/scrape` - Scrape all keywords
- `POST /api/scrape/{keyword_id}` - Scrape single keyword
- `GET /api/scrape/status` - Check progress

### Categories
- `POST /api/categories/hide` - Add to blocklist
- `GET /api/keywords/{id}/blocklist` - View blocked categories
- `DELETE /api/keywords/{id}/blocklist?category_id=X` - Remove from blocklist

### Decks
- `GET /api/decks` - List all decks
- `POST /api/decks` - Create deck
- `PUT /api/decks/{id}/sizing` - Set sizing profile
