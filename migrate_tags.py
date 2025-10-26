#!/usr/bin/env python3
"""
Tag Migration Script for Inoreader
Migrates old tag formats to new standardized format
"""

import json
import argparse
import time
import re
import requests
from typing import Dict, List, Set, Tuple
from inoreader_tagger import InoreaderAPI, URLPatternMatcher


class TagMigrator:
    """Handles migration of old tags to new format"""
    
    def __init__(self, api: InoreaderAPI, url_matcher: URLPatternMatcher):
        self.api = api
        self.url_matcher = url_matcher
        self.stats = {
            'articles_processed': 0,
            'articles_updated': 0,
            'tags_added': 0,
            'tags_removed': 0,
            'errors': 0
        }
        
        # Define old tag patterns that should be removed
        self.old_tag_patterns = [
            r'^reddit-(?!r/).+$',  # Match reddit-* but NOT reddit-r/*
            r'^reddit$',           # Match plain 'reddit' tag
        ]
    
    def process_articles_incrementally(self, batch_size: int = 100, max_articles: int = None, folder_filter: str = None, feed_filter: str = None, dry_run: bool = True):
        """Process articles incrementally as we fetch them - no giant lists"""
        
        # Determine which stream to use
        if folder_filter:
            import urllib.parse
            encoded_folder = urllib.parse.quote(folder_filter, safe='')
            stream_id = f"user/-/label/{encoded_folder}"
            print(f"Processing articles from folder '{folder_filter}' (batch size: {batch_size})...")
        else:
            stream_id = "user/-/state/com.google/reading-list"
            print(f"Processing articles from all folders (batch size: {batch_size})...")
        
        continuation = None
        processed_count = 0
        migrated_count = 0
        
        while True:
            # Get articles from the specified stream
            params = {
                'n': batch_size,
                'output': 'json'
            }
            
            if continuation:
                params['c'] = continuation
            
            try:
                response = self.api.get_stream_contents(
                    stream_id=stream_id,
                    count=batch_size,
                    continuation=continuation
                )
                
                articles = response.get('items', [])
                continuation = response.get('continuation')
                
                if not articles:
                    print(f"  No more articles found")
                    break
                
                # Process each article immediately
                for article in articles:
                    processed_count += 1
                    
                    # Apply feed filter if specified
                    if feed_filter:
                        origin = article.get('origin', {})
                        feed_title = origin.get('title', '').lower()
                        if feed_filter.lower() not in feed_title:
                            continue
                    
                    # Check if this article has old tags we need to migrate
                    article_tags = self._extract_tags_from_article(article)
                    if any(self._should_remove_old_tag(tag) for tag in article_tags):
                        migrated_count += 1
                        
                        print(f"\n[{migrated_count}] Processing article with old tags...")
                        
                        try:
                            self.migrate_article_tags(article, dry_run=dry_run)
                        except Exception as e:
                            print(f"\n💥 FATAL ERROR processing article {migrated_count}: {e}")
                            print(f"Article: {article.get('title', 'Unknown')}")
                            print(f"URL: {self._get_article_url(article)}")
                            print(f"\n🛑 Stopping migration due to error")
                            print(f"📊 Progress before error: {migrated_count} articles with old tags processed out of {processed_count} total articles scanned")
                            self.stats['errors'] += 1
                            raise
                        
                        # Stop if we've reached max articles to migrate
                        if max_articles and migrated_count >= max_articles:
                            print(f"\n✅ Reached maximum of {max_articles} articles to migrate")
                            break
                
                print(f"  📊 Batch complete: {processed_count} total articles scanned, {migrated_count} with old tags processed")
                
                # Stop if we've reached max articles or no continuation
                if not continuation or (max_articles and migrated_count >= max_articles):
                    break
                    
                # Small delay to avoid rate limiting
                time.sleep(0.5)
                
            except Exception as e:
                print(f"💥 Error fetching articles: {e}")
                if hasattr(e, 'response') and e.response is not None:
                    print(f"   HTTP {e.response.status_code}: {e.response.text}")
                raise Exception(f"Failed to fetch articles: {e}")
        
        print(f"\n🏁 Final results: Processed {migrated_count} articles with old tags out of {processed_count} total articles scanned")
    
    def _extract_tags_from_article(self, article: Dict) -> Set[str]:
        """Extract user tags from article categories"""
        tags = set()
        categories = article.get('categories', [])
        
        for category in categories:
            if '/label/' in category and not category.endswith('/state/com.google/reading-list') and not category.endswith('/state/com.google/fresh'):
                # Extract tag name from category like "user/1005421489/label/Reddit"
                tag_name = category.split('/label/')[-1]
                tags.add(tag_name)
        
        return tags
    
    def _should_remove_old_tag(self, tag: str) -> bool:
        """Check if a tag should be removed (old reddit tags)"""
        for pattern in self.old_tag_patterns:
            if re.match(pattern, tag):
                return True
        return False
    
    def _get_article_url(self, article: Dict) -> str:
        """Extract the canonical URL from an article"""
        canonical = article.get('canonical', [])
        if canonical:
            return canonical[0].get('href', '')
        
        alternate = article.get('alternate', [])
        if alternate:
            return alternate[0].get('href', '')
        
        return ''
    
    def migrate_article_tags(self, article: Dict, dry_run: bool = True) -> bool:
        """Migrate tags for a single article"""
        article_id = article.get('id', '')
        title = article.get('title', 'Untitled')
        
        if not article_id:
            print(f"  ⚠️  Skipping article with no ID: {title}")
            return False
        
        # Get current tags
        current_tags = self._extract_tags_from_article(article)
        
        # Get the article URL and determine correct tags based on URL patterns
        article_url = self._get_article_url(article)
        if not article_url:
            print(f"  ⚠️  No URL found for article: {title}")
            return False
        
        # Use the URL matcher to get correct tags for this article
        correct_tags = set(self.url_matcher.match_url(article_url))
        
        # Find old reddit tags to remove
        tags_to_remove = []
        for tag in current_tags:
            if self._should_remove_old_tag(tag):
                tags_to_remove.append(tag)
        
        # Find new tags to add (that aren't already present)
        tags_to_add = []
        for tag in correct_tags:
            if tag not in current_tags:
                tags_to_add.append(tag)
        
        if not tags_to_remove and not tags_to_add:
            print(f"      No migration needed - skipping")
            return False
        
        print(f"\n  📄 Article: {title[:60]}...")
        print(f"      URL: {article_url}")
        print(f"      Current tags: {', '.join(sorted(current_tags))}")
        print(f"      Correct tags from URL: {', '.join(sorted(correct_tags))}")
        
        if tags_to_remove:
            print(f"      Remove old tags: {', '.join(tags_to_remove)}")
        if tags_to_add:
            print(f"      Add new tags: {', '.join(tags_to_add)}")
        
        if dry_run:
            print(f"      [DRY RUN] Would migrate tags")
            self.stats['articles_updated'] += 1
            self.stats['tags_removed'] += len(tags_to_remove)
            self.stats['tags_added'] += len(tags_to_add)
            return True
        
        # Actually perform the migration
        success = True
        
        # Remove old tags
        for tag in tags_to_remove:
            try:
                response = requests.post(
                    f"{self.api.BASE_URL}/edit-tag",
                    headers=self.api._get_headers(),
                    data={
                        'i': article_id,
                        'r': f'user/-/label/{tag}',
                        'ac': 'edit-tags'
                    }
                )
                
                if response.status_code == 200:
                    print(f"        ✓ Removed: {tag}")
                    self.stats['tags_removed'] += 1
                else:
                    print(f"        ✗ Failed to remove: {tag}")
                    print(f"            HTTP {response.status_code}: {response.text}")
                    raise Exception(f"API returned {response.status_code} for remove tag operation")
                    
            except Exception as e:
                print(f"        ✗ Error removing {tag}: {e}")
                self.stats['errors'] += 1
                raise Exception(f"Failed to remove tag '{tag}' from article '{title}': {e}")
            
            time.sleep(0.1)  # Rate limiting
        
        # Add new tags
        for tag in tags_to_add:
            try:
                response = requests.post(
                    f"{self.api.BASE_URL}/edit-tag",
                    headers=self.api._get_headers(),
                    data={
                        'i': article_id,
                        'a': f'user/-/label/{tag}',
                        'ac': 'edit-tags'
                    }
                )
                
                if response.status_code == 200:
                    print(f"        ✓ Added: {tag}")
                    self.stats['tags_added'] += 1
                else:
                    print(f"        ✗ Failed to add: {tag}")
                    print(f"            HTTP {response.status_code}: {response.text}")
                    raise Exception(f"API returned {response.status_code} for add tag operation")
                    
            except Exception as e:
                print(f"        ✗ Error adding {tag}: {e}")
                self.stats['errors'] += 1
                raise Exception(f"Failed to add tag '{tag}' to article '{title}': {e}")
            
            time.sleep(0.1)  # Rate limiting
        
        if success:
            self.stats['articles_updated'] += 1
        
        return success
    
    def migrate_all_tags(self, dry_run: bool = True, max_articles: int = None, batch_size: int = 100, folder_filter: str = None, feed_filter: str = None):
        """Migrate tags for all relevant articles"""
        print("="*60)
        print("INOREADER TAG MIGRATION")
        print("="*60)
        print(f"Mode: {'DRY RUN' if dry_run else 'LIVE MIGRATION'}")
        print(f"Max articles: {max_articles or 'No limit'}")
        if folder_filter:
            print(f"Folder filter: '{folder_filter}'")
        if feed_filter:
            print(f"Feed filter: '{feed_filter}'")
        print()
        
        # Show migration approach
        print("Migration approach:")
        print("  - Remove old reddit-* tags")
        print("  - Extract correct tags from article URLs using current tagging rules")
        print("  - Apply correct tags based on actual subreddit")
        print()
        
        # Process articles incrementally - no giant lists
        self.process_articles_incrementally(
            batch_size=batch_size,
            max_articles=max_articles,
            folder_filter=folder_filter,
            feed_filter=feed_filter,
            dry_run=dry_run
        )
        
        # Print final statistics
        self._print_stats(dry_run)
    
    def _print_stats(self, dry_run: bool):
        """Print migration statistics"""
        print("\n" + "="*60)
        print(f"MIGRATION {'PREVIEW' if dry_run else 'RESULTS'}")
        print("="*60)
        print(f"Articles processed: {self.stats['articles_processed']}")
        print(f"Articles updated: {self.stats['articles_updated']}")
        print(f"Tags removed: {self.stats['tags_removed']}")
        print(f"Tags added: {self.stats['tags_added']}")
        print(f"Errors: {self.stats['errors']}")
        print("="*60)
        
        if dry_run:
            print("\nThis was a DRY RUN. No changes were made.")
            print("Run with --live to actually perform the migration.")
        else:
            print("\nMigration completed!")


def main():
    """Main function"""
    parser = argparse.ArgumentParser(description='Migrate old Inoreader tags to new format')
    parser.add_argument('--config', default='config.json', help='Configuration file path')
    parser.add_argument('--live', action='store_true', help='Actually perform migration (default is dry run)')
    parser.add_argument('--max-articles', type=int, help='Maximum number of articles to process')
    parser.add_argument('--batch-size', type=int, default=100, help='Batch size for fetching articles')
    parser.add_argument('--folder', default='Discussion sites', help='Folder to process (default: Discussion sites)')
    parser.add_argument('--feed', help='Filter by feed name (e.g., "reddit" for Reddit posts only)')
    
    args = parser.parse_args()
    
    # Load configuration
    try:
        with open(args.config, 'r') as f:
            config = json.load(f)
    except FileNotFoundError:
        print(f"Configuration file '{args.config}' not found.")
        return
    except json.JSONDecodeError as e:
        print(f"Error parsing configuration file: {e}")
        return
    
    # Initialize API
    api = InoreaderAPI(
        app_id=config['app_id'],
        app_key=config['app_key'],
        refresh_token=config.get('refresh_token')
    )
    
    if not api.refresh_token:
        print("No refresh token found. Please run inoreader_tagger.py first to authenticate.")
        return
    
    # Initialize URL matcher with the same rules as the main tagger
    url_matcher = URLPatternMatcher(config['tagging_rules'])
    
    # Initialize migrator and run
    migrator = TagMigrator(api, url_matcher)
    
    try:
        migrator.migrate_all_tags(
            dry_run=not args.live,
            max_articles=args.max_articles,
            batch_size=args.batch_size,
            folder_filter=args.folder,
            feed_filter=args.feed
        )
    except KeyboardInterrupt:
        print("\n\n⚠️  Migration interrupted by user")
        migrator._print_stats(not args.live)
    except Exception as e:
        print(f"\n💥 Migration failed with error: {e}")
        print(f"   Error type: {type(e).__name__}")
        if hasattr(e, '__cause__') and e.__cause__:
            print(f"   Caused by: {e.__cause__}")
        migrator._print_stats(not args.live)
        exit(1)


if __name__ == '__main__':
    main()