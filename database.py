"""
Database layer for TFT-style shopping helper.
Uses SQLite with fresh schema for items and keywords.
Multi-user support with magic link authentication.
"""

import sqlite3
import secrets
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional, List

DB_PATH = Path(__file__).parent / "shoppinghelper.db"

# Magic link validity (24 hours)
MAGIC_LINK_VALIDITY_HOURS = 24


def get_connection():
    """Get a database connection with row factory."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Initialize the database with fresh schema."""
    conn = get_connection()
    cursor = conn.cursor()

    # Decks table - folders for keywords with sizing profiles
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS decks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            priority INTEGER DEFAULT 0,
            size_a_op TEXT DEFAULT NULL,
            size_a_val INTEGER DEFAULT NULL,
            size_b_op TEXT DEFAULT NULL,
            size_b_val INTEGER DEFAULT NULL,
            size_c_op TEXT DEFAULT NULL,
            size_c_val INTEGER DEFAULT NULL,
            size_d_op TEXT DEFAULT NULL,
            size_d_val INTEGER DEFAULT NULL,
            size_e_op TEXT DEFAULT NULL,
            size_e_val INTEGER DEFAULT NULL,
            size_f_op TEXT DEFAULT NULL,
            size_f_val INTEGER DEFAULT NULL,
            size_g_op TEXT DEFAULT NULL,
            size_g_val INTEGER DEFAULT NULL,
            size_h_op TEXT DEFAULT NULL,
            size_h_val INTEGER DEFAULT NULL
        )
    """)

    # Keywords table - search terms to scrape
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS keywords (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            keyword TEXT NOT NULL UNIQUE,
            source TEXT DEFAULT 'both',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_scraped_at TIMESTAMP,
            item_count INTEGER DEFAULT 0,
            priority INTEGER DEFAULT 0,
            deck_id INTEGER REFERENCES decks(id)
        )
    """)

    # Migration: Add priority column if it doesn't exist
    cursor.execute("PRAGMA table_info(keywords)")
    columns = [col[1] for col in cursor.fetchall()]
    if 'priority' not in columns:
        cursor.execute("ALTER TABLE keywords ADD COLUMN priority INTEGER DEFAULT 0")

    # Migration: Add deck_id column if it doesn't exist
    if 'deck_id' not in columns:
        cursor.execute("ALTER TABLE keywords ADD COLUMN deck_id INTEGER REFERENCES decks(id)")

    # Items table - scraped marketplace items
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,
            source_id TEXT NOT NULL,
            title TEXT,
            price INTEGER,
            image_url TEXT,
            images TEXT,
            description TEXT,
            url TEXT,
            scraped_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            seen BOOLEAN DEFAULT FALSE,
            saved BOOLEAN DEFAULT FALSE,
            stars INTEGER DEFAULT NULL,
            time_spent_ms INTEGER,
            keyword_id INTEGER,
            sold_status TEXT DEFAULT 'unknown',
            is_auction BOOLEAN DEFAULT FALSE,
            auction_end_time INTEGER DEFAULT NULL,
            UNIQUE(source, source_id),
            FOREIGN KEY (keyword_id) REFERENCES keywords(id)
        )
    """)

    # Migration: Add sold_status, is_auction, auction_end_time columns if they don't exist
    cursor.execute("PRAGMA table_info(items)")
    item_columns = [col[1] for col in cursor.fetchall()]
    if 'sold_status' not in item_columns:
        cursor.execute("ALTER TABLE items ADD COLUMN sold_status TEXT DEFAULT 'unknown'")
    if 'is_auction' not in item_columns:
        cursor.execute("ALTER TABLE items ADD COLUMN is_auction BOOLEAN DEFAULT FALSE")
    if 'auction_end_time' not in item_columns:
        cursor.execute("ALTER TABLE items ADD COLUMN auction_end_time INTEGER DEFAULT NULL")
    if 'fit_score' not in item_columns:
        cursor.execute("ALTER TABLE items ADD COLUMN fit_score INTEGER DEFAULT NULL")

    # Migration: Add category_id column if it doesn't exist
    if 'category_id' not in item_columns:
        cursor.execute("ALTER TABLE items ADD COLUMN category_id TEXT")

    # Migration: Add hidden column if it doesn't exist
    if 'hidden' not in item_columns:
        cursor.execute("ALTER TABLE items ADD COLUMN hidden BOOLEAN DEFAULT FALSE")

    # Migration: Add in_cart column if it doesn't exist
    if 'in_cart' not in item_columns:
        cursor.execute("ALTER TABLE items ADD COLUMN in_cart BOOLEAN DEFAULT FALSE")

    # Category hierarchy cache (populated on-demand)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS categories (
            id TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            name TEXT,
            name_en TEXT,
            parent_id TEXT,
            path TEXT
        )
    """)

    # Whitelist: categories TO show (per keyword, set during creation)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS keyword_whitelist (
            keyword_id INTEGER NOT NULL,
            category_id TEXT NOT NULL,
            PRIMARY KEY (keyword_id, category_id),
            FOREIGN KEY (keyword_id) REFERENCES keywords(id)
        )
    """)

    # Blocklist: categories NOT to show
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS category_blocklist (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category_id TEXT NOT NULL,
            keyword_id INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(category_id, keyword_id)
        )
    """)

    # =====================================================
    # MULTI-USER TABLES
    # =====================================================

    # Users table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL UNIQUE,
            username TEXT UNIQUE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_login_at TIMESTAMP
        )
    """)

    # Sessions table (unlimited sessions per user)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            token TEXT NOT NULL UNIQUE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        )
    """)

    # Magic links table (24h validity)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS magic_links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL,
            token TEXT NOT NULL UNIQUE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            expires_at TIMESTAMP NOT NULL,
            used_at TIMESTAMP,
            link_type TEXT DEFAULT 'login'
        )
    """)

    # Invite requests table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS invite_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL UNIQUE,
            reason TEXT,
            status TEXT DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            approved_at TIMESTAMP,
            approved_by INTEGER REFERENCES users(id)
        )
    """)

    # Deck shares table (for public sharing)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS deck_shares (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            deck_id INTEGER NOT NULL UNIQUE,
            share_slug TEXT NOT NULL,
            is_public BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (deck_id) REFERENCES decks(id) ON DELETE CASCADE
        )
    """)

    # =====================================================
    # MULTI-USER COLUMN MIGRATIONS
    # =====================================================

    # Add user_id to decks
    cursor.execute("PRAGMA table_info(decks)")
    deck_columns = [col[1] for col in cursor.fetchall()]
    if 'user_id' not in deck_columns:
        cursor.execute("ALTER TABLE decks ADD COLUMN user_id INTEGER REFERENCES users(id)")

    # Add user_id to keywords
    cursor.execute("PRAGMA table_info(keywords)")
    kw_columns = [col[1] for col in cursor.fetchall()]
    if 'user_id' not in kw_columns:
        cursor.execute("ALTER TABLE keywords ADD COLUMN user_id INTEGER REFERENCES users(id)")

    # Add user_id to items
    cursor.execute("PRAGMA table_info(items)")
    item_cols = [col[1] for col in cursor.fetchall()]
    if 'user_id' not in item_cols:
        cursor.execute("ALTER TABLE items ADD COLUMN user_id INTEGER REFERENCES users(id)")

    # Add user_id to category_blocklist
    cursor.execute("PRAGMA table_info(category_blocklist)")
    bl_columns = [col[1] for col in cursor.fetchall()]
    if 'user_id' not in bl_columns:
        cursor.execute("ALTER TABLE category_blocklist ADD COLUMN user_id INTEGER REFERENCES users(id)")

    # =====================================================
    # INDEXES
    # =====================================================

    # Indexes for performance
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_items_unseen ON items(seen, saved)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_items_saved ON items(saved)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_items_keyword ON items(keyword_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_items_source ON items(source)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_keywords_deck ON keywords(deck_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_items_category ON items(category_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_blocklist_keyword ON category_blocklist(keyword_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_items_hidden ON items(hidden)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_items_cart ON items(in_cart)")

    # Multi-user indexes
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_sessions_token ON sessions(token)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_magic_links_token ON magic_links(token)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_magic_links_email ON magic_links(email)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_invite_requests_status ON invite_requests(status)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_deck_shares_slug ON deck_shares(share_slug)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_decks_user ON decks(user_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_keywords_user ON keywords(user_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_items_user ON items(user_id)")

    # Auto-create Default deck and migrate orphan keywords
    cursor.execute("SELECT COUNT(*) as count FROM decks")
    if cursor.fetchone()['count'] == 0:
        cursor.execute("INSERT INTO decks (name, priority) VALUES ('Default', 0)")
        cursor.execute("UPDATE keywords SET deck_id = 1 WHERE deck_id IS NULL")

    conn.commit()
    conn.close()
    print(f"Database initialized at {DB_PATH}")


def get_next_unseen(count: int = 5) -> List[dict]:
    """Get next N unseen, unsaved, unhidden items for the reroll view, ordered by keyword priority."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT i.id, i.source, i.source_id, i.title, i.price, i.image_url, i.url,
               i.saved, i.stars, i.keyword_id, i.category_id,
               k.keyword as keyword_name, k.deck_id, d.name as deck_name
        FROM items i
        LEFT JOIN keywords k ON i.keyword_id = k.id
        LEFT JOIN decks d ON k.deck_id = d.id
        WHERE i.seen = FALSE AND i.saved = FALSE AND (i.hidden = FALSE OR i.hidden IS NULL)
        ORDER BY COALESCE(k.priority, 0) DESC, i.scraped_at DESC
        LIMIT ?
    """, (count,))
    items = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return items


def mark_seen(item_ids: List[int]):
    """Mark items as seen."""
    if not item_ids:
        return
    conn = get_connection()
    cursor = conn.cursor()

    placeholders = ','.join('?' * len(item_ids))
    cursor.execute(f"""
        UPDATE items SET seen = TRUE
        WHERE id IN ({placeholders})
    """, item_ids)

    conn.commit()
    conn.close()


def save_item(item_id: int) -> dict:
    """Mark item as saved (add to deck). Also marks as seen."""
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("UPDATE items SET saved = TRUE, seen = TRUE WHERE id = ?", (item_id,))
    conn.commit()

    cursor.execute("SELECT * FROM items WHERE id = ?", (item_id,))
    row = cursor.fetchone()
    item = dict(row) if row else None
    conn.close()
    return item


def unsave_item(item_id: int):
    """Remove item from deck. Keeps seen=TRUE so it won't reappear in reroll."""
    conn = get_connection()
    cursor = conn.cursor()

    # Only set saved=FALSE, keep seen=TRUE so it doesn't come back to reroll
    cursor.execute("UPDATE items SET saved = FALSE, stars = NULL WHERE id = ?", (item_id,))
    conn.commit()
    conn.close()


def rate_item(item_id: int, stars: int):
    """Rate an item 1-5 stars."""
    if stars < 1 or stars > 5:
        raise ValueError("Stars must be 1-5")

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("UPDATE items SET stars = ? WHERE id = ?", (stars, item_id))
    conn.commit()
    conn.close()


def set_item_cart(item_id: int, in_cart: bool):
    """Set an item's cart status."""
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("UPDATE items SET in_cart = ? WHERE id = ?", (1 if in_cart else 0, item_id))
    conn.commit()
    conn.close()


def get_saved_items(source: str = None, sort_by: str = 'scraped_at', order: str = 'desc',
                    filter_cart: bool = False, filter_stars: int = None,
                    filter_deck: int = None, filter_keyword: int = None) -> List[dict]:
    """Get all saved items with optional additive filtering and sorting.

    Filters are additive (AND logic):
    - filter_cart: True to show only cart items
    - filter_stars: 1-5 to show items with that star rating
    - filter_deck: deck_id to show items from that deck
    - filter_keyword: keyword_id to show items from that keyword
    """
    conn = get_connection()
    cursor = conn.cursor()

    query = """
        SELECT i.*, k.keyword as keyword_name, k.deck_id, d.name as deck_name
        FROM items i
        LEFT JOIN keywords k ON i.keyword_id = k.id
        LEFT JOIN decks d ON k.deck_id = d.id
        WHERE i.saved = TRUE
    """
    params = []

    if source and source in ('mercari', 'yahoo'):
        query += " AND i.source = ?"
        params.append(source)

    # Apply additive filters (all conditions are ANDed together)
    if filter_cart:
        query += " AND i.in_cart = TRUE"
    if filter_stars is not None:
        query += " AND i.stars = ?"
        params.append(int(filter_stars))
    if filter_deck is not None:
        query += " AND k.deck_id = ?"
        params.append(int(filter_deck))
    if filter_keyword is not None:
        query += " AND i.keyword_id = ?"
        params.append(int(filter_keyword))

    valid_sorts = {'price': 'i.price', 'date': 'i.scraped_at', 'stars': 'i.stars', 'scraped_at': 'i.scraped_at', 'fit': 'i.fit_score'}
    sort_col = valid_sorts.get(sort_by, 'i.scraped_at')
    order_dir = 'ASC' if order.lower() == 'asc' else 'DESC'

    # Handle NULL stars/fit - put unrated at end when sorting by stars or fit
    if sort_col in ('i.stars', 'i.fit_score'):
        query += f" ORDER BY {sort_col} IS NULL, {sort_col} {order_dir}"
    else:
        query += f" ORDER BY {sort_col} {order_dir}"

    cursor.execute(query, params)
    items = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return items


def get_item(item_id: int) -> Optional[dict]:
    """Get a single item by ID."""
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT * FROM items WHERE id = ?", (item_id,))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None


def get_existing_source_ids(source: str, keyword_id: int = None) -> set:
    """Get set of source_ids already in database for a source."""
    conn = get_connection()
    cursor = conn.cursor()

    if keyword_id:
        cursor.execute(
            "SELECT source_id FROM items WHERE source = ? AND keyword_id = ?",
            (source, keyword_id)
        )
    else:
        cursor.execute("SELECT source_id FROM items WHERE source = ?", (source,))

    ids = {row['source_id'] for row in cursor.fetchall()}
    conn.close()
    return ids


# Deck management

def create_deck(name: str) -> int:
    """Create a new deck. Returns deck ID."""
    conn = get_connection()
    cursor = conn.cursor()

    try:
        cursor.execute("INSERT INTO decks (name) VALUES (?)", (name.strip(),))
        conn.commit()
        deck_id = cursor.lastrowid
    except sqlite3.IntegrityError:
        cursor.execute("SELECT id FROM decks WHERE name = ?", (name.strip(),))
        deck_id = cursor.fetchone()['id']

    conn.close()
    return deck_id


def get_decks() -> List[dict]:
    """Get all decks with keyword and item counts."""
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT d.*,
               COUNT(DISTINCT k.id) as keyword_count,
               COUNT(DISTINCT i.id) as item_count
        FROM decks d
        LEFT JOIN keywords k ON k.deck_id = d.id
        LEFT JOIN items i ON i.keyword_id = k.id
        GROUP BY d.id
        ORDER BY d.priority DESC, d.created_at DESC
    """)

    decks = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return decks


def get_decks_with_keywords() -> List[dict]:
    """Get all decks with their keywords for hierarchical filter menu."""
    conn = get_connection()
    cursor = conn.cursor()

    # Get decks
    cursor.execute("""
        SELECT d.id, d.name
        FROM decks d
        ORDER BY d.priority DESC, d.created_at DESC
    """)
    decks = [dict(row) for row in cursor.fetchall()]

    # Get keywords for each deck
    for deck in decks:
        cursor.execute("""
            SELECT k.id, k.keyword,
                   COUNT(CASE WHEN i.saved = TRUE THEN 1 END) as saved_count
            FROM keywords k
            LEFT JOIN items i ON i.keyword_id = k.id
            WHERE k.deck_id = ?
            GROUP BY k.id
            ORDER BY k.priority DESC, k.created_at DESC
        """, (deck['id'],))
        deck['keywords'] = [dict(row) for row in cursor.fetchall()]

    conn.close()
    return decks


def get_deck(deck_id: int) -> Optional[dict]:
    """Get a single deck by ID with full sizing profile."""
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT * FROM decks WHERE id = ?", (deck_id,))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None


def update_deck(deck_id: int, name: str = None):
    """Update deck name."""
    conn = get_connection()
    cursor = conn.cursor()

    if name:
        cursor.execute("UPDATE decks SET name = ? WHERE id = ?", (name.strip(), deck_id))

    conn.commit()
    conn.close()


def update_deck_sizing(deck_id: int, sizing: dict):
    """Update deck sizing profile. sizing is a dict with keys a-h, each having op and val."""
    conn = get_connection()
    cursor = conn.cursor()

    # Build dynamic update
    updates = []
    params = []
    for key in ['a', 'b', 'c', 'd', 'e', 'f', 'g', 'h']:
        if key in sizing:
            updates.append(f"size_{key}_op = ?")
            updates.append(f"size_{key}_val = ?")
            params.append(sizing[key].get('op'))
            params.append(sizing[key].get('val'))

    if updates:
        params.append(deck_id)
        cursor.execute(f"UPDATE decks SET {', '.join(updates)} WHERE id = ?", params)
        conn.commit()

    conn.close()


def delete_deck(deck_id: int):
    """Delete a deck. Keywords in this deck are moved to Default deck."""
    conn = get_connection()
    cursor = conn.cursor()

    # Move keywords to Default deck (id=1)
    cursor.execute("UPDATE keywords SET deck_id = 1 WHERE deck_id = ?", (deck_id,))
    # Delete the deck
    cursor.execute("DELETE FROM decks WHERE id = ?", (deck_id,))

    conn.commit()
    conn.close()


def reorder_decks(deck_ids: List[int]):
    """Reorder decks by setting their priorities based on position in list."""
    conn = get_connection()
    cursor = conn.cursor()

    for index, deck_id in enumerate(deck_ids):
        priority = len(deck_ids) - index
        cursor.execute("UPDATE decks SET priority = ? WHERE id = ?", (priority, deck_id))

    conn.commit()
    conn.close()


def get_keywords_by_deck(deck_id: int) -> List[dict]:
    """Get all keywords for a specific deck, including blocklist info."""
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT k.*, COUNT(i.id) as actual_item_count
        FROM keywords k
        LEFT JOIN items i ON i.keyword_id = k.id
        WHERE k.deck_id = ?
        GROUP BY k.id
        ORDER BY k.priority DESC, k.created_at DESC
    """, (deck_id,))

    keywords = [dict(row) for row in cursor.fetchall()]

    # For each keyword, get blocked categories with names
    for kw in keywords:
        cursor.execute("""
            SELECT b.id as entry_id, b.category_id, c.name as category_name
            FROM category_blocklist b
            LEFT JOIN categories c ON b.category_id = c.id
            WHERE b.keyword_id = ?
        """, (kw['id'],))
        blocked = [dict(row) for row in cursor.fetchall()]
        kw['blocked_categories'] = blocked
        kw['blocked_count'] = len(blocked)

    conn.close()
    return keywords


def get_keyword_blocklist(keyword_id: int) -> List[dict]:
    """Get blocklist entries for a specific keyword with category names."""
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT b.id as entry_id, b.category_id, b.created_at,
               c.name as category_name, c.parent_id
        FROM category_blocklist b
        LEFT JOIN categories c ON b.category_id = c.id
        WHERE b.keyword_id = ?
        ORDER BY b.created_at DESC
    """, (keyword_id,))

    entries = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return entries


def remove_from_keyword_blocklist(keyword_id: int, category_id: str) -> bool:
    """Remove a category from a keyword's blocklist. Returns True if removed."""
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        DELETE FROM category_blocklist
        WHERE keyword_id = ? AND category_id = ?
    """, (keyword_id, category_id))

    removed = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return removed


def move_keyword_to_deck(keyword_id: int, deck_id: int):
    """Move a keyword to a different deck."""
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("UPDATE keywords SET deck_id = ? WHERE id = ?", (deck_id, keyword_id))

    conn.commit()
    conn.close()


def get_deck_for_keyword(keyword_id: int) -> Optional[dict]:
    """Get the deck that contains a keyword. Used for breadcrumb display."""
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT d.* FROM decks d
        JOIN keywords k ON k.deck_id = d.id
        WHERE k.id = ?
    """, (keyword_id,))

    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None


def update_item_fit_score(item_id: int, score: int):
    """Update an item's fit score (1-4)."""
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("UPDATE items SET fit_score = ? WHERE id = ?", (score, item_id))

    conn.commit()
    conn.close()


# Keyword management

def add_keyword(keyword: str, source: str = 'both', deck_id: int = None) -> int:
    """Add a new keyword. Returns keyword ID."""
    if source not in ('mercari', 'yahoo', 'both'):
        source = 'both'

    # Default to deck_id 1 (Default deck) if not specified
    if deck_id is None:
        deck_id = 1

    conn = get_connection()
    cursor = conn.cursor()

    try:
        cursor.execute("""
            INSERT INTO keywords (keyword, source, deck_id)
            VALUES (?, ?, ?)
        """, (keyword.strip(), source, deck_id))
        conn.commit()
        keyword_id = cursor.lastrowid
    except sqlite3.IntegrityError:
        cursor.execute("SELECT id FROM keywords WHERE keyword = ?", (keyword.strip(),))
        keyword_id = cursor.fetchone()['id']

    conn.close()
    return keyword_id


def get_keywords() -> List[dict]:
    """Get all keywords with stats, ordered by priority."""
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT k.*, d.name as deck_name, COUNT(i.id) as actual_item_count
        FROM keywords k
        LEFT JOIN decks d ON k.deck_id = d.id
        LEFT JOIN items i ON i.keyword_id = k.id
        GROUP BY k.id
        ORDER BY k.priority DESC, k.created_at DESC
    """)

    keywords = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return keywords


def get_keywords_with_unseen_counts() -> List[dict]:
    """Get all keywords with their unseen item counts (excluding hidden items)."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT k.id, k.keyword, k.priority, k.deck_id, d.name as deck_name,
               COUNT(CASE WHEN i.seen = FALSE AND i.saved = FALSE AND (i.hidden = FALSE OR i.hidden IS NULL) THEN 1 END) as unseen_count
        FROM keywords k
        LEFT JOIN decks d ON k.deck_id = d.id
        LEFT JOIN items i ON i.keyword_id = k.id
        GROUP BY k.id
        ORDER BY k.priority DESC, k.created_at DESC
    """)
    keywords = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return keywords


def prioritize_keyword(keyword_id: int):
    """Set keyword to highest priority (top of deck)."""
    conn = get_connection()
    cursor = conn.cursor()

    # Get current max priority
    cursor.execute("SELECT MAX(priority) as max_priority FROM keywords")
    row = cursor.fetchone()
    max_priority = row['max_priority'] if row['max_priority'] is not None else 0

    # Set this keyword's priority to max + 1
    cursor.execute("UPDATE keywords SET priority = ? WHERE id = ?", (max_priority + 1, keyword_id))
    conn.commit()
    conn.close()


def deprioritize_keyword(keyword_id: int):
    """Set keyword to lowest priority (bottom of deck)."""
    conn = get_connection()
    cursor = conn.cursor()

    # Get current min priority
    cursor.execute("SELECT MIN(priority) as min_priority FROM keywords")
    row = cursor.fetchone()
    min_priority = row['min_priority'] if row['min_priority'] is not None else 0

    # Set this keyword's priority to min - 1
    cursor.execute("UPDATE keywords SET priority = ? WHERE id = ?", (min_priority - 1, keyword_id))
    conn.commit()
    conn.close()


def reorder_keywords(keyword_ids: List[int]):
    """Reorder keywords by setting their priorities based on position in list."""
    conn = get_connection()
    cursor = conn.cursor()

    # First ID in list gets highest priority, last gets lowest
    for index, keyword_id in enumerate(keyword_ids):
        priority = len(keyword_ids) - index
        cursor.execute("UPDATE keywords SET priority = ? WHERE id = ?", (priority, keyword_id))

    conn.commit()
    conn.close()


def delete_keyword(keyword_id: int):
    """Delete a keyword and its unseen/unsaved items."""
    conn = get_connection()
    cursor = conn.cursor()

    # Delete unseen/unsaved items associated with this keyword
    cursor.execute("""
        DELETE FROM items
        WHERE keyword_id = ? AND seen = FALSE AND saved = FALSE
    """, (keyword_id,))

    # Also delete blocklist entries for this keyword
    cursor.execute("DELETE FROM category_blocklist WHERE keyword_id = ?", (keyword_id,))

    # Also delete whitelist entries for this keyword
    cursor.execute("DELETE FROM keyword_whitelist WHERE keyword_id = ?", (keyword_id,))

    # Delete the keyword
    cursor.execute("DELETE FROM keywords WHERE id = ?", (keyword_id,))
    conn.commit()
    conn.close()


def update_keyword_scraped(keyword_id: int, item_count: int):
    """Update keyword after scraping."""
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        UPDATE keywords
        SET last_scraped_at = CURRENT_TIMESTAMP, item_count = ?
        WHERE id = ?
    """, (item_count, keyword_id))

    conn.commit()
    conn.close()


# Scraping

def save_scraped_items(items: List[dict], keyword_id: int) -> int:
    """Save scraped items. Returns count of new items added. Items in blocked categories are marked hidden."""
    import json
    conn = get_connection()
    cursor = conn.cursor()
    new_count = 0

    for item in items:
        try:
            images = item.get('images')
            if images and isinstance(images, list):
                images = json.dumps(images)

            # Check if category is blocked
            category_id = item.get('category_id')
            hidden = is_category_blocked(category_id, keyword_id) if category_id else False

            cursor.execute("""
                INSERT INTO items (source, source_id, title, price, image_url, images, description, url, keyword_id, is_auction, auction_end_time, category_id, hidden)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                item.get('source'),
                item.get('source_id'),
                item.get('title'),
                item.get('price'),
                item.get('image_url'),
                images,
                item.get('description'),
                item.get('url'),
                keyword_id,
                item.get('is_auction', False),
                item.get('auction_end_time'),
                category_id,
                hidden,
            ))
            new_count += 1
        except sqlite3.IntegrityError:
            pass  # Skip duplicates

    conn.commit()
    conn.close()
    return new_count


def update_item_sold_status(item_id: int, sold_status: str, price: int = None):
    """Update an item's sold status and optionally its price."""
    conn = get_connection()
    cursor = conn.cursor()

    if price is not None:
        cursor.execute(
            "UPDATE items SET sold_status = ?, price = ? WHERE id = ?",
            (sold_status, price, item_id)
        )
    else:
        cursor.execute(
            "UPDATE items SET sold_status = ? WHERE id = ?",
            (sold_status, item_id)
        )

    conn.commit()
    conn.close()


def import_item(source: str, source_id: str, url: str, title: str = None, price: int = None,
                image_url: str = None, images: str = None, description: str = None,
                is_auction: bool = False, auction_end_time: int = None,
                sold_status: str = "unknown") -> Optional[int]:
    """
    Import an item directly as saved. Used for manual URL imports.
    Updates existing items with fresh data, or creates new ones.
    Returns item_id.
    """
    import json as json_lib
    conn = get_connection()
    cursor = conn.cursor()

    # Convert images list to JSON if needed
    if images and isinstance(images, list):
        images = json_lib.dumps(images)

    # Check if item already exists
    cursor.execute(
        "SELECT id FROM items WHERE source = ? AND source_id = ?",
        (source, source_id)
    )
    existing = cursor.fetchone()
    if existing:
        # Already exists - update with fresh data and mark as saved
        cursor.execute("""
            UPDATE items SET
                saved = TRUE,
                seen = TRUE,
                title = COALESCE(?, title),
                price = COALESCE(?, price),
                image_url = COALESCE(?, image_url),
                images = COALESCE(?, images),
                description = COALESCE(?, description),
                is_auction = ?,
                auction_end_time = COALESCE(?, auction_end_time),
                sold_status = ?
            WHERE id = ?
        """, (
            title,
            price,
            image_url,
            images,
            description,
            1 if is_auction else 0,
            auction_end_time,
            sold_status,
            existing['id']
        ))
        conn.commit()
        conn.close()
        return existing['id']

    # Insert new item as saved
    cursor.execute("""
        INSERT INTO items (source, source_id, title, price, image_url, images, description, url,
                          keyword_id, is_auction, auction_end_time, sold_status, saved, seen)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, TRUE, TRUE)
    """, (
        source,
        source_id,
        title,
        price,
        image_url,
        images,
        description,
        url,
        1 if is_auction else 0,
        auction_end_time,
        sold_status,
    ))

    item_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return item_id


# Stats

def get_stats() -> dict:
    """Get database statistics."""
    conn = get_connection()
    cursor = conn.cursor()

    stats = {}

    cursor.execute("SELECT COUNT(*) as count FROM items")
    stats['total_items'] = cursor.fetchone()['count']

    # Unseen items excludes hidden items (blocked categories) and unsaved items
    cursor.execute("""
        SELECT COUNT(*) as count FROM items
        WHERE seen = FALSE AND saved = FALSE AND (hidden = FALSE OR hidden IS NULL)
    """)
    stats['unseen_items'] = cursor.fetchone()['count']

    cursor.execute("SELECT COUNT(*) as count FROM items WHERE saved = TRUE")
    stats['saved_items'] = cursor.fetchone()['count']

    cursor.execute("SELECT COUNT(*) as count FROM items WHERE stars IS NOT NULL")
    stats['rated_items'] = cursor.fetchone()['count']

    cursor.execute("SELECT COUNT(*) as count FROM keywords")
    stats['total_keywords'] = cursor.fetchone()['count']

    conn.close()
    return stats


# Category management

def add_category(cat_id: str, source: str, name: str, parent_id: str = None, path: str = None, name_en: str = None):
    """Add or update a category in the cache."""
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        INSERT OR REPLACE INTO categories (id, source, name, name_en, parent_id, path)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (cat_id, source, name, name_en, parent_id, path))

    conn.commit()
    conn.close()


def get_category(cat_id: str) -> Optional[dict]:
    """Get a category by ID."""
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT * FROM categories WHERE id = ?", (cat_id,))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None


def get_category_ancestors(cat_id: str) -> List[str]:
    """Get list of category IDs from this category up to root. No API fetching."""
    if not cat_id:
        return []
    conn = get_connection()
    cursor = conn.cursor()
    ancestors = [cat_id]
    current = cat_id
    for _ in range(10):  # max depth
        cursor.execute("SELECT parent_id FROM categories WHERE id = ?", (current,))
        row = cursor.fetchone()
        if row and row['parent_id']:
            ancestors.append(row['parent_id'])
            current = row['parent_id']
        else:
            break
    conn.close()
    return ancestors


def fetch_mercari_category_hierarchy(cat_id: str) -> List[dict]:
    """Fetch category hierarchy from Mercari API and cache it. Returns list from root to leaf."""
    import requests
    from mercari.DpopUtils import generate_DPOP

    # Extract numeric ID
    numeric_id = cat_id.replace("mercari:", "")

    # We need to find an item with this category to get the hierarchy
    # Instead, let's fetch any item and use parent_categories_ntiers structure
    # Actually, we can get category info by fetching item details
    # But we need an item ID... Let's query our DB for one
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT source_id FROM items WHERE category_id = ? LIMIT 1", (cat_id,))
    row = cursor.fetchone()
    conn.close()

    if not row:
        return [{"id": cat_id, "name": cat_id}]

    item_id = row['source_id']

    try:
        api_url = "https://api.mercari.jp/items/get"
        dpop = generate_DPOP(uuid="CategoryFetch", method="GET", url=api_url)
        headers = {"DPOP": dpop, "X-Platform": "web", "Accept": "*/*"}
        r = requests.get(api_url, headers=headers, params={"id": item_id}, timeout=10)

        if r.status_code != 200:
            return [{"id": cat_id, "name": cat_id}]

        data = r.json().get("data", {})
        parents = data.get("parent_categories_ntiers", [])
        leaf = data.get("item_category_ntiers", {})

        # Build hierarchy and cache it
        ancestors = []
        conn = get_connection()
        cursor = conn.cursor()

        # Parents go from root to immediate parent
        prev_id = None
        for p in parents:
            full_id = f"mercari:{p['id']}"
            ancestors.append({"id": full_id, "name": p["name"]})
            # Cache in DB
            cursor.execute("""
                INSERT OR REPLACE INTO categories (id, source, name, parent_id)
                VALUES (?, 'mercari', ?, ?)
            """, (full_id, p["name"], prev_id))
            prev_id = full_id

        # Add the leaf category
        if leaf:
            full_id = f"mercari:{leaf['id']}"
            ancestors.append({"id": full_id, "name": leaf["name"]})
            cursor.execute("""
                INSERT OR REPLACE INTO categories (id, source, name, parent_id)
                VALUES (?, 'mercari', ?, ?)
            """, (full_id, leaf["name"], prev_id))

        conn.commit()
        conn.close()
        return ancestors

    except Exception as e:
        print(f"Error fetching Mercari category: {e}")
        return [{"id": cat_id, "name": cat_id}]


def fetch_yahoo_category_hierarchy(cat_id: str) -> List[dict]:
    """Fetch category hierarchy from Yahoo and cache it. Returns list from root to leaf."""
    import httpx
    import re
    import json

    numeric_id = cat_id.replace("yahoo:", "")

    # Yahoo category API - try fetching category page
    try:
        # Get an item with this category to extract hierarchy from its page
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT url FROM items WHERE category_id = ? LIMIT 1", (cat_id,))
        row = cursor.fetchone()
        conn.close()

        if not row:
            return [{"id": cat_id, "name": cat_id}]

        # Fetch item page and parse __NEXT_DATA__
        with httpx.Client() as client:
            response = client.get(
                row['url'],
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=10.0,
                follow_redirects=True
            )
            html = response.text

        match = re.search(r'<script id="__NEXT_DATA__"[^>]*>([^<]+)</script>', html)
        if not match:
            return [{"id": cat_id, "name": cat_id}]

        data = json.loads(match.group(1))
        item_data = (data.get("props", {})
                        .get("pageProps", {})
                        .get("initialState", {})
                        .get("item", {})
                        .get("detail", {})
                        .get("item", {}))

        # Yahoo uses category.path structure
        category_data = item_data.get("category", {})
        category_path = category_data.get("path", [])
        if not category_path:
            return [{"id": cat_id, "name": cat_id}]

        # Build hierarchy and cache (skip first "オークション" root)
        ancestors = []
        conn = get_connection()
        cursor = conn.cursor()

        prev_id = None
        for cat in category_path:
            if cat.get("id") == "0":  # Skip root "オークション"
                continue
            cat_id_full = f"yahoo:{cat.get('id')}"
            cat_name = cat.get("name", cat_id_full)
            ancestors.append({"id": cat_id_full, "name": cat_name})
            cursor.execute("""
                INSERT OR REPLACE INTO categories (id, source, name, parent_id)
                VALUES (?, 'yahoo', ?, ?)
            """, (cat_id_full, cat_name, prev_id))
            prev_id = cat_id_full

        conn.commit()
        conn.close()
        return ancestors

    except Exception as e:
        print(f"Error fetching Yahoo category: {e}")
        return [{"id": cat_id, "name": cat_id}]


def get_category_ancestors_with_names(cat_id: str) -> List[dict]:
    """Get list of category dicts (id, name) from root down to this category.
    Fetches from API if not cached."""
    if not cat_id:
        return []

    conn = get_connection()
    cursor = conn.cursor()

    # First, try to build from cached data
    ancestors = []
    current = cat_id
    for _ in range(10):  # max depth
        cursor.execute("SELECT id, name, parent_id FROM categories WHERE id = ?", (current,))
        row = cursor.fetchone()
        if row:
            ancestors.append({"id": row['id'], "name": row['name']})
            if row['parent_id']:
                current = row['parent_id']
            else:
                break
        else:
            # Category not in DB - need to fetch
            conn.close()
            if cat_id.startswith("mercari:"):
                return fetch_mercari_category_hierarchy(cat_id)
            elif cat_id.startswith("yahoo:"):
                return fetch_yahoo_category_hierarchy(cat_id)
            else:
                return [{"id": cat_id, "name": cat_id}]

    conn.close()
    # Reverse so it goes from root to leaf
    return list(reversed(ancestors))


def hide_items_by_category(category_id: str, keyword_id: int = None) -> int:
    """
    Hide all unseen items that belong to a category or its descendants.
    If keyword_id is provided, only hide items from that keyword.
    Returns count of items hidden.
    """
    conn = get_connection()
    cursor = conn.cursor()

    # Get all descendant categories (categories where this is an ancestor)
    # We need to find categories where parent chain includes category_id
    # For now, we'll just match the exact category and its direct children
    # Since we store parent_id, we need to find all categories with this parent
    categories_to_hide = [category_id]

    # Find all descendant categories recursively
    def get_descendants(parent_id):
        cursor.execute("SELECT id FROM categories WHERE parent_id = ?", (parent_id,))
        children = [row['id'] for row in cursor.fetchall()]
        for child in children:
            categories_to_hide.append(child)
            get_descendants(child)

    get_descendants(category_id)

    # Build query
    placeholders = ','.join('?' * len(categories_to_hide))
    if keyword_id:
        cursor.execute(f"""
            UPDATE items SET hidden = TRUE
            WHERE category_id IN ({placeholders})
            AND keyword_id = ?
            AND seen = FALSE AND saved = FALSE
        """, categories_to_hide + [keyword_id])
    else:
        cursor.execute(f"""
            UPDATE items SET hidden = TRUE
            WHERE category_id IN ({placeholders})
            AND seen = FALSE AND saved = FALSE
        """, categories_to_hide)

    count = cursor.rowcount
    conn.commit()
    conn.close()
    return count


def is_category_blocked(category_id: str, keyword_id: int = None) -> bool:
    """
    Check if a category (or any of its ancestors) is in the blocklist.
    Returns True if blocked, False otherwise.
    """
    if not category_id:
        return False

    conn = get_connection()
    cursor = conn.cursor()

    # Get all ancestors for this category
    ancestors = [category_id]
    current = category_id
    for _ in range(10):  # max depth
        cursor.execute("SELECT parent_id FROM categories WHERE id = ?", (current,))
        row = cursor.fetchone()
        if row and row['parent_id']:
            ancestors.append(row['parent_id'])
            current = row['parent_id']
        else:
            break

    # Check blocklist for any ancestor
    placeholders = ','.join('?' * len(ancestors))

    # Check global blocklist
    cursor.execute(f"""
        SELECT 1 FROM category_blocklist
        WHERE category_id IN ({placeholders}) AND keyword_id IS NULL
        LIMIT 1
    """, ancestors)
    if cursor.fetchone():
        conn.close()
        return True

    # Check keyword-specific blocklist
    if keyword_id:
        cursor.execute(f"""
            SELECT 1 FROM category_blocklist
            WHERE category_id IN ({placeholders}) AND keyword_id = ?
            LIMIT 1
        """, ancestors + [keyword_id])
        if cursor.fetchone():
            conn.close()
            return True

    conn.close()
    return False


def add_keyword_whitelist(keyword_id: int, category_ids: List[str]):
    """Set the whitelist for a keyword (replaces existing)."""
    conn = get_connection()
    cursor = conn.cursor()

    # Clear existing whitelist
    cursor.execute("DELETE FROM keyword_whitelist WHERE keyword_id = ?", (keyword_id,))

    # Add new entries
    for cat_id in category_ids:
        cursor.execute("""
            INSERT INTO keyword_whitelist (keyword_id, category_id)
            VALUES (?, ?)
        """, (keyword_id, cat_id))

    conn.commit()
    conn.close()


def get_keyword_whitelist(keyword_id: int) -> set:
    """Get the whitelist for a keyword as a set of category IDs."""
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT category_id FROM keyword_whitelist WHERE keyword_id = ?", (keyword_id,))
    whitelist = {row['category_id'] for row in cursor.fetchall()}

    conn.close()
    return whitelist


def add_to_blocklist(category_id: str, keyword_id: int = None) -> int:
    """Add a category to the blocklist. Returns blocklist entry ID."""
    conn = get_connection()
    cursor = conn.cursor()

    try:
        cursor.execute("""
            INSERT INTO category_blocklist (category_id, keyword_id)
            VALUES (?, ?)
        """, (category_id, keyword_id))
        conn.commit()
        entry_id = cursor.lastrowid
    except sqlite3.IntegrityError:
        # Already exists
        cursor.execute("""
            SELECT id FROM category_blocklist
            WHERE category_id = ? AND (keyword_id = ? OR (keyword_id IS NULL AND ? IS NULL))
        """, (category_id, keyword_id, keyword_id))
        entry_id = cursor.fetchone()['id']

    conn.close()
    return entry_id


def remove_from_blocklist(entry_id: int):
    """Remove an entry from the blocklist."""
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("DELETE FROM category_blocklist WHERE id = ?", (entry_id,))

    conn.commit()
    conn.close()


def get_blocklist(keyword_id: int = None) -> tuple:
    """
    Get blocklist entries.
    Returns (global_blocklist, keyword_blocklist) as sets of category IDs.
    """
    conn = get_connection()
    cursor = conn.cursor()

    # Global blocklist (keyword_id IS NULL)
    cursor.execute("SELECT category_id FROM category_blocklist WHERE keyword_id IS NULL")
    global_blocklist = {row['category_id'] for row in cursor.fetchall()}

    # Keyword-specific blocklist
    keyword_blocklist = set()
    if keyword_id:
        cursor.execute("SELECT category_id FROM category_blocklist WHERE keyword_id = ?", (keyword_id,))
        keyword_blocklist = {row['category_id'] for row in cursor.fetchall()}

    conn.close()
    return global_blocklist, keyword_blocklist


def get_all_blocklist_entries() -> List[dict]:
    """Get all blocklist entries with category info."""
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT b.*, c.name as category_name, c.path as category_path, k.keyword
        FROM category_blocklist b
        LEFT JOIN categories c ON b.category_id = c.id
        LEFT JOIN keywords k ON b.keyword_id = k.id
        ORDER BY b.created_at DESC
    """)

    entries = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return entries


def update_item_category(item_id: int, category_id: str):
    """Update an item's category_id."""
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("UPDATE items SET category_id = ? WHERE id = ?", (category_id, item_id))

    conn.commit()
    conn.close()


# =====================================================
# AUTHENTICATION FUNCTIONS
# =====================================================

def create_magic_link(email: str, link_type: str = 'login') -> str:
    """Create a magic link token for email. Returns the token."""
    conn = get_connection()
    cursor = conn.cursor()

    token = secrets.token_urlsafe(32)
    expires_at = datetime.utcnow() + timedelta(hours=MAGIC_LINK_VALIDITY_HOURS)

    cursor.execute("""
        INSERT INTO magic_links (email, token, expires_at, link_type)
        VALUES (?, ?, ?, ?)
    """, (email.lower(), token, expires_at.isoformat(), link_type))

    conn.commit()
    conn.close()
    return token


def verify_magic_link(token: str) -> Optional[dict]:
    """Verify a magic link token. Returns {email, link_type} if valid, None otherwise."""
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT email, link_type, expires_at, used_at
        FROM magic_links WHERE token = ?
    """, (token,))

    row = cursor.fetchone()
    if not row:
        conn.close()
        return None

    # Check if already used
    if row['used_at']:
        conn.close()
        return None

    # Check if expired
    expires_at = datetime.fromisoformat(row['expires_at'])
    if datetime.utcnow() > expires_at:
        conn.close()
        return None

    # Mark as used
    cursor.execute("""
        UPDATE magic_links SET used_at = CURRENT_TIMESTAMP WHERE token = ?
    """, (token,))
    conn.commit()
    conn.close()

    return {'email': row['email'], 'link_type': row['link_type']}


def create_session(user_id: int) -> str:
    """Create a new session for user. Returns session token."""
    conn = get_connection()
    cursor = conn.cursor()

    token = secrets.token_urlsafe(32)

    cursor.execute("""
        INSERT INTO sessions (user_id, token)
        VALUES (?, ?)
    """, (user_id, token))

    conn.commit()
    conn.close()
    return token


def get_user_from_session(token: str) -> Optional[dict]:
    """Get user from session token. Returns user dict or None."""
    if not token:
        return None

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT u.id, u.email, u.username, u.created_at, u.last_login_at
        FROM users u
        JOIN sessions s ON s.user_id = u.id
        WHERE s.token = ?
    """, (token,))

    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None


def delete_session(token: str):
    """Delete a session (logout)."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM sessions WHERE token = ?", (token,))
    conn.commit()
    conn.close()


def get_or_create_user(email: str) -> dict:
    """Get existing user by email, or create new one. Returns user dict."""
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT * FROM users WHERE email = ?", (email.lower(),))
    row = cursor.fetchone()

    if row:
        # Update last login
        cursor.execute("""
            UPDATE users SET last_login_at = CURRENT_TIMESTAMP WHERE id = ?
        """, (row['id'],))
        conn.commit()
        conn.close()
        return dict(row)

    # Create new user
    cursor.execute("""
        INSERT INTO users (email) VALUES (?)
    """, (email.lower(),))
    user_id = cursor.lastrowid
    conn.commit()

    cursor.execute("SELECT * FROM users WHERE id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    return dict(row)


def get_user_by_email(email: str) -> Optional[dict]:
    """Get user by email. Returns user dict or None."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users WHERE email = ?", (email.lower(),))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None


def get_user_by_username(username: str) -> Optional[dict]:
    """Get user by username. Returns user dict or None."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users WHERE username = ?", (username,))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None


def set_username(user_id: int, username: str) -> bool:
    """Set username for user. Returns True if successful, False if taken."""
    conn = get_connection()
    cursor = conn.cursor()

    # Check if username is taken
    cursor.execute("SELECT id FROM users WHERE username = ? AND id != ?", (username, user_id))
    if cursor.fetchone():
        conn.close()
        return False

    cursor.execute("UPDATE users SET username = ? WHERE id = ?", (username, user_id))
    conn.commit()
    conn.close()
    return True


# =====================================================
# INVITE REQUEST FUNCTIONS
# =====================================================

def create_invite_request(email: str, reason: str = None) -> Optional[int]:
    """Create an invite request. Returns request ID or None if already exists."""
    conn = get_connection()
    cursor = conn.cursor()

    try:
        cursor.execute("""
            INSERT INTO invite_requests (email, reason)
            VALUES (?, ?)
        """, (email.lower(), reason))
        conn.commit()
        request_id = cursor.lastrowid
        conn.close()
        return request_id
    except sqlite3.IntegrityError:
        conn.close()
        return None


def get_invite_request_by_email(email: str) -> Optional[dict]:
    """Get invite request by email."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM invite_requests WHERE email = ?", (email.lower(),))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None


def get_pending_invite_requests() -> List[dict]:
    """Get all pending invite requests, ordered by date."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT * FROM invite_requests
        WHERE status = 'pending'
        ORDER BY created_at ASC
    """)
    requests = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return requests


def approve_invite_request(request_id: int, approved_by: int) -> Optional[str]:
    """Approve an invite request. Returns the email or None if not found."""
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT email FROM invite_requests WHERE id = ?", (request_id,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        return None

    email = row['email']

    cursor.execute("""
        UPDATE invite_requests
        SET status = 'approved', approved_at = CURRENT_TIMESTAMP, approved_by = ?
        WHERE id = ?
    """, (approved_by, request_id))
    conn.commit()
    conn.close()

    return email


def reject_invite_request(request_id: int) -> bool:
    """Reject an invite request. Returns True if found."""
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        UPDATE invite_requests
        SET status = 'rejected'
        WHERE id = ?
    """, (request_id,))

    updated = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return updated


# =====================================================
# DECK SHARING FUNCTIONS
# =====================================================

def get_deck_share(deck_id: int) -> Optional[dict]:
    """Get share settings for a deck."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM deck_shares WHERE deck_id = ?", (deck_id,))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None


def set_deck_share(deck_id: int, share_slug: str, is_public: bool) -> int:
    """Set or update share settings for a deck. Returns share ID."""
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        INSERT INTO deck_shares (deck_id, share_slug, is_public)
        VALUES (?, ?, ?)
        ON CONFLICT(deck_id) DO UPDATE SET
            is_public = excluded.is_public,
            share_slug = excluded.share_slug
    """, (deck_id, share_slug, is_public))

    conn.commit()
    share_id = cursor.lastrowid
    conn.close()
    return share_id


def get_public_deck_by_slug(username: str, share_slug: str) -> Optional[dict]:
    """Get a public deck by username and slug. Returns deck with owner info."""
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT d.*, ds.share_slug, ds.is_public, u.username as owner_username
        FROM decks d
        JOIN deck_shares ds ON ds.deck_id = d.id
        JOIN users u ON d.user_id = u.id
        WHERE u.username = ? AND ds.share_slug = ? AND ds.is_public = TRUE
    """, (username, share_slug))

    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None


def get_public_deck_items(deck_id: int, user_id: int) -> List[dict]:
    """Get saved items in a deck for public display."""
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT i.id, i.title, i.price, i.image_url, i.url, i.source, i.stars
        FROM items i
        JOIN keywords k ON i.keyword_id = k.id
        WHERE k.deck_id = ? AND i.saved = TRUE AND i.user_id = ?
        ORDER BY i.stars DESC NULLS LAST, i.scraped_at DESC
    """, (deck_id, user_id))

    items = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return items


# =====================================================
# MIGRATION FUNCTION
# =====================================================

def migrate_to_multiuser(owner_email: str = "ulokwa@gmail.com"):
    """
    One-time migration to multi-user system.
    Creates user #1 and assigns all existing data to them.
    Safe to run multiple times (idempotent).
    """
    conn = get_connection()
    cursor = conn.cursor()

    # Check if user #1 already exists
    cursor.execute("SELECT id FROM users WHERE id = 1")
    if cursor.fetchone():
        print("Migration already complete - user #1 exists")
        conn.close()
        return

    # Create owner user
    cursor.execute("""
        INSERT INTO users (id, email, username)
        VALUES (1, ?, 'owner')
    """, (owner_email.lower(),))

    # Assign all existing data to user #1
    cursor.execute("UPDATE decks SET user_id = 1 WHERE user_id IS NULL")
    decks_updated = cursor.rowcount

    cursor.execute("UPDATE keywords SET user_id = 1 WHERE user_id IS NULL")
    keywords_updated = cursor.rowcount

    cursor.execute("UPDATE items SET user_id = 1 WHERE user_id IS NULL")
    items_updated = cursor.rowcount

    cursor.execute("UPDATE category_blocklist SET user_id = 1 WHERE user_id IS NULL")
    blocklist_updated = cursor.rowcount

    conn.commit()
    conn.close()

    print(f"Migration complete!")
    print(f"  Owner email: {owner_email}")
    print(f"  Decks migrated: {decks_updated}")
    print(f"  Keywords migrated: {keywords_updated}")
    print(f"  Items migrated: {items_updated}")
    print(f"  Blocklist entries migrated: {blocklist_updated}")


if __name__ == "__main__":
    init_db()
    print("Stats:", get_stats())
