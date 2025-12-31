#!/usr/bin/env python3
"""
CLI for Shopping Helper.
"""

import argparse
import sys


def cmd_init(args):
    """Initialize database."""
    from database import init_db
    init_db()


def cmd_serve(args):
    """Start the web server."""
    import uvicorn
    print(f"Starting server at http://localhost:{args.port}")
    uvicorn.run("app:app", host="0.0.0.0", port=args.port, reload=args.reload)


def cmd_scrape(args):
    """Scrape all keywords or a specific one."""
    from database import get_keywords, add_keyword
    from scraper import scrape_keyword, scrape_all_keywords

    if args.keyword:
        # Add keyword if it doesn't exist, then scrape
        keyword_id = add_keyword(args.keyword, args.source)
        print(f"Scraping keyword: {args.keyword}")
        result = scrape_keyword(keyword_id, args.keyword, args.source, args.max_items)
        print(f"Done! Scraped {result['scraped']}, saved {result['saved']} new items.")
    else:
        # Scrape all existing keywords
        result = scrape_all_keywords(args.max_items)
        print(f"Total: {result['total_scraped']} scraped, {result['total_saved']} new items.")


def cmd_keywords(args):
    """Manage keywords."""
    from database import get_keywords, add_keyword, delete_keyword

    if args.action == 'list':
        keywords = get_keywords()
        if not keywords:
            print("No keywords. Add one with: python cli.py keywords add 'keyword'")
            return
        print(f"{'ID':<4} {'Keyword':<30} {'Source':<10} {'Items':<8}")
        print("-" * 56)
        for kw in keywords:
            print(f"{kw['id']:<4} {kw['keyword']:<30} {kw['source']:<10} {kw['actual_item_count']:<8}")

    elif args.action == 'add':
        if not args.keyword:
            print("Usage: python cli.py keywords add 'keyword' [--source both|mercari|yahoo]")
            return
        keyword_id = add_keyword(args.keyword, args.source)
        print(f"Added keyword '{args.keyword}' (id: {keyword_id})")

    elif args.action == 'remove':
        if not args.id:
            print("Usage: python cli.py keywords remove --id <keyword_id>")
            return
        delete_keyword(args.id)
        print(f"Removed keyword id {args.id}")


def cmd_stats(args):
    """Show database statistics."""
    from database import get_stats
    stats = get_stats()
    print("Database Statistics")
    print("-" * 30)
    print(f"Total items:    {stats['total_items']}")
    print(f"Unseen items:   {stats['unseen_items']}")
    print(f"Saved items:    {stats['saved_items']}")
    print(f"Rated items:    {stats['rated_items']}")
    print(f"Keywords:       {stats['total_keywords']}")


def cmd_mock(args):
    """Add mock data for testing."""
    from database import add_keyword, save_scraped_items
    from scraper import generate_mock_items

    keyword_id = add_keyword("mock", "both")
    items = generate_mock_items("mock", args.count)
    saved = save_scraped_items(items, keyword_id)
    print(f"Added {saved} mock items")


def main():
    parser = argparse.ArgumentParser(description="Shopping Helper CLI")
    subparsers = parser.add_subparsers(dest='command', help='Commands')

    # init
    subparsers.add_parser('init', help='Initialize database')

    # serve
    serve_parser = subparsers.add_parser('serve', help='Start web server')
    serve_parser.add_argument('-p', '--port', type=int, default=8000, help='Port (default: 8000)')
    serve_parser.add_argument('--reload', action='store_true', help='Enable auto-reload')

    # scrape
    scrape_parser = subparsers.add_parser('scrape', help='Scrape items')
    scrape_parser.add_argument('keyword', nargs='?', help='Keyword to scrape (optional)')
    scrape_parser.add_argument('--source', choices=['mercari', 'yahoo', 'both'], default='both')
    scrape_parser.add_argument('-n', '--max-items', type=int, default=300, help='Max items per source')

    # keywords
    keywords_parser = subparsers.add_parser('keywords', help='Manage keywords')
    keywords_parser.add_argument('action', choices=['list', 'add', 'remove'])
    keywords_parser.add_argument('keyword', nargs='?', help='Keyword to add')
    keywords_parser.add_argument('--source', choices=['mercari', 'yahoo', 'both'], default='both')
    keywords_parser.add_argument('--id', type=int, help='Keyword ID to remove')

    # stats
    subparsers.add_parser('stats', help='Show database statistics')

    # mock
    mock_parser = subparsers.add_parser('mock', help='Add mock data for testing')
    mock_parser.add_argument('-n', '--count', type=int, default=50, help='Number of mock items')

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    commands = {
        'init': cmd_init,
        'serve': cmd_serve,
        'scrape': cmd_scrape,
        'keywords': cmd_keywords,
        'stats': cmd_stats,
        'mock': cmd_mock,
    }

    commands[args.command](args)


if __name__ == '__main__':
    main()
