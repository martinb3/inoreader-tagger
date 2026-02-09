"""
Inoreader Dynamic Tagging Script
Automatically applies tags to articles based on URL patterns
"""

import requests
import json
import re
import os
from typing import List, Dict, Optional
from urllib.parse import urlparse, parse_qs
import time
import secrets


class InoreaderAPI:
    """Wrapper for Inoreader API operations"""
    
    BASE_URL = "https://www.inoreader.com/reader/api/0"
    AUTH_URL = "https://www.inoreader.com/oauth2/token"
    
    def __init__(self, app_id: str, app_key: str, refresh_token: Optional[str] = None):
        self.app_id = app_id
        self.app_key = app_key
        self.access_token = None
        self.refresh_token = refresh_token
        self.oauth_state = None  # Store state for OAuth flow
        
        if refresh_token:
            self.refresh_access_token()
    
    def get_authorization_url(self) -> str:
        """Get the URL for user authorization"""
        # Generate a secure random state parameter
        self.oauth_state = secrets.token_urlsafe(32)
        return f"https://www.inoreader.com/oauth2/auth?client_id={self.app_id}&redirect_uri=http://localhost&response_type=code&scope=read+write&state={self.oauth_state}"
    
    def exchange_code_for_token(self, auth_code: str, state: Optional[str] = None) -> Dict:
        """Exchange authorization code for access and refresh tokens"""
        # Validate state parameter if provided
        if state and self.oauth_state and state != self.oauth_state:
            raise ValueError("Invalid state parameter - possible CSRF attack")
        
        data = {
            'code': auth_code,
            'redirect_uri': 'http://localhost',
            'client_id': self.app_id,
            'client_secret': self.app_key,
            'grant_type': 'authorization_code'
        }
        
        response = requests.post(self.AUTH_URL, data=data)
        response.raise_for_status()
        
        token_data = response.json()
        self.access_token = token_data['access_token']
        self.refresh_token = token_data['refresh_token']
        
        return token_data
    
    def refresh_access_token(self):
        """Refresh the access token using refresh token"""
        if not self.refresh_token:
            raise ValueError("No refresh token available")
        
        data = {
            'refresh_token': self.refresh_token,
            'client_id': self.app_id,
            'client_secret': self.app_key,
            'grant_type': 'refresh_token'
        }
        
        response = requests.post(self.AUTH_URL, data=data)
        
        if response.status_code != 200:
            try:
                error_data = response.json()
                error_msg = error_data.get('error_description', error_data.get('error', 'Unknown error'))
                print(f"Token refresh failed: {error_msg}")
                print(f"This usually means the refresh token has expired and you need to re-authenticate.")
            except:
                print(f"Token refresh failed with HTTP {response.status_code}")
                print(f"Response: {response.text}")
            
            # Clear the invalid refresh token so user gets guided through re-auth
            self.refresh_token = None
            raise ValueError("Refresh token expired or invalid - re-authentication required")
        
        token_data = response.json()
        self.access_token = token_data['access_token']
        
        return token_data
    
    def _get_headers(self) -> Dict:
        """Get headers for API requests"""
        return {
            'Authorization': f'Bearer {self.access_token}',
            'Content-Type': 'application/json'
        }
    
    def get_stream_contents(self, stream_id: str = "user/-/state/com.google/reading-list", 
                           count: int = 100, continuation: Optional[str] = None) -> Dict:
        """Get articles from a stream"""
        params = {
            'n': count,
            'output': 'json'
        }
        
        if continuation:
            params['c'] = continuation
        
        url = f"{self.BASE_URL}/stream/contents/{stream_id}"
        response = requests.get(url, headers=self._get_headers(), params=params)
        response.raise_for_status()
        
        return response.json()
    
    def get_unread_articles(self, count: int = 100, folder_name: Optional[str] = None, since_timestamp: Optional[str] = None) -> List[Dict]:
        """Get unread articles using Inoreader API xt parameter to exclude read items"""
        
        if folder_name:
            # Use folder-specific stream instead of reading-list
            # URL encode the folder name for the stream ID
            import urllib.parse
            encoded_folder = urllib.parse.quote(folder_name, safe='')
            stream_id = f"user/-/label/{encoded_folder}"
            print(f"Filtering articles from folder: '{folder_name}'")
        else:
            # Use default reading-list stream for all articles
            stream_id = "user/-/state/com.google/reading-list"
        
        # Use the xt parameter to exclude read articles as per Inoreader API docs
        # This ensures we ONLY process unread articles, never touching read ones
        params = {
            'n': count,
            'output': 'json',
            'xt': 'user/-/state/com.google/read'  # Exclude read items (server-side filtering)
        }
        
        # Add timestamp filter if provided (using microsecond timestamp)
        # The 'ot' parameter expects microsecond timestamp (same format as timestampUsec)
        client_filter_timestamp = None
        if since_timestamp:
            # Use microsecond timestamp directly for ot parameter
            timestamp_microseconds = int(since_timestamp)
            params['ot'] = str(timestamp_microseconds)
            # Keep same microsecond timestamp for client-side filtering
            client_filter_timestamp = int(since_timestamp)
            print(f"Getting articles since microsecond timestamp: {timestamp_microseconds}")
            print("Using server-side ot parameter with microsecond timestamp")
        
        url = f"{self.BASE_URL}/stream/contents/{stream_id}"
        response = requests.get(url, headers=self._get_headers(), params=params)
        response.raise_for_status()
        
        data = response.json()
        articles = data.get('items', [])
        
        # Client-side timestamp filtering as additional safety check
        if client_filter_timestamp:
            original_count = len(articles)
            # Filter using microsecond timestamp (same format as timestampUsec)
            def safe_int(value, default=0):
                try:
                    return int(value) if value else default
                except (ValueError, TypeError):
                    return default
            
            articles = [
                article for article in articles 
                if safe_int(article.get('timestampUsec', 0)) >= client_filter_timestamp
            ]
            filtered_count = len(articles)
            if filtered_count < original_count:
                print(f"Client-side filtering: kept {filtered_count}/{original_count} articles (filtered out {original_count - filtered_count} older articles based on microsecond timestamp)")
        
        return articles
    
    def get_tags(self) -> List[Dict]:
        """Get all user tags"""
        url = f"{self.BASE_URL}/tag/list"
        response = requests.get(url, headers=self._get_headers(), params={'output': 'json'})
        response.raise_for_status()
        
        data = response.json()
        return [tag for tag in data.get('tags', []) if '/label/' in tag.get('id', '')]
    
    def add_tag_to_article(self, article_id: str, tag_name: str) -> tuple[bool, str]:
        """Add a tag to an article
        
        Returns:
            tuple: (success: bool, error_message: str)
        """
        # Ensure tag name is properly formatted
        if not tag_name.startswith('user/-/label/'):
            tag_name = f'user/-/label/{tag_name}'
        
        url = f"{self.BASE_URL}/edit-tag"
        data = {
            'i': article_id,
            'a': tag_name,
            'ac': 'edit-tags'
        }
        
        response = requests.post(url, headers=self._get_headers(), data=data)
        
        if response.status_code == 200:
            return True, ""
        else:
            error_msg = f"HTTP {response.status_code}"
            try:
                # Try to get more details from response
                response_text = response.text.strip()
                if response_text:
                    error_msg += f": {response_text}"
            except:
                pass
            return False, error_msg
    
    def add_tag_to_articles_batch(self, article_ids: List[str], tag_name: str) -> tuple[bool, str]:
        """Add a tag to multiple articles in one API call
        
        Args:
            article_ids: List of article IDs to tag
            tag_name: Tag to apply
            
        Returns:
            tuple: (success: bool, error_message: str)
        """
        if not article_ids:
            return True, ""
            
        # Ensure tag name is properly formatted
        if not tag_name.startswith('user/-/label/'):
            tag_name = f'user/-/label/{tag_name}'
        
        url = f"{self.BASE_URL}/edit-tag"
        # Build data with multiple 'i' parameters for batch operation
        data = [('a', tag_name), ('ac', 'edit-tags')]
        for article_id in article_ids:
            data.append(('i', article_id))
        
        response = requests.post(url, headers=self._get_headers(), data=data)
        
        if response.status_code == 200:
            return True, ""
        else:
            error_msg = f"HTTP {response.status_code}"
            try:
                response_text = response.text.strip()
                if response_text:
                    error_msg += f": {response_text}"
            except:
                pass
            return False, error_msg
    
    def remove_tag_from_article(self, article_id: str, tag_name: str) -> bool:
        """Remove a tag from an article"""
        if not tag_name.startswith('user/-/label/'):
            tag_name = f'user/-/label/{tag_name}'
        
        url = f"{self.BASE_URL}/edit-tag"
        data = {
            'i': article_id,
            'r': tag_name,
            'ac': 'edit-tags'
        }
        
        response = requests.post(url, headers=self._get_headers(), data=data)
        return response.status_code == 200
    
    def remove_tag_from_articles_batch(self, article_ids: List[str], tag_name: str) -> tuple[bool, str]:
        """Remove a tag from multiple articles in one API call
        
        Args:
            article_ids: List of article IDs to untag
            tag_name: Tag to remove
            
        Returns:
            tuple: (success: bool, error_message: str)
        """
        if not article_ids:
            return True, ""
            
        if not tag_name.startswith('user/-/label/'):
            tag_name = f'user/-/label/{tag_name}'
        
        url = f"{self.BASE_URL}/edit-tag"
        # Build data with multiple 'i' parameters for batch operation
        data = [('r', tag_name), ('ac', 'edit-tags')]
        for article_id in article_ids:
            data.append(('i', article_id))
        
        response = requests.post(url, headers=self._get_headers(), data=data)
        
        if response.status_code == 200:
            return True, ""
        else:
            error_msg = f"HTTP {response.status_code}"
            try:
                response_text = response.text.strip()
                if response_text:
                    error_msg += f": {response_text}"
            except:
                pass
            return False, error_msg
    
    def get_unread_counts(self) -> Dict:
        """Get unread counts for all folders and feeds"""
        url = f"{self.BASE_URL}/unread-count"
        response = requests.get(url, headers=self._get_headers())
        response.raise_for_status()
        
        return response.json()


class URLPatternMatcher:
    """Matches URL patterns to determine tags"""
    
    def __init__(self, rules: List[Dict]):
        """
        Initialize with tagging rules
        
        Rules format:
        [
            {
                "pattern": "github.com",
                "match_type": "domain",  # or "path", "full", "regex"
                "tags": ["GitHub", "Development"]
            },
            {
                "pattern": "/blog/",
                "match_type": "path",
                "tags": ["Blog"]
            }
        ]
        """
        self.rules = rules
    
    def match_url(self, url: str) -> List[str]:
        """Match URL against rules and return applicable tags"""
        tags = []
        parsed_url = urlparse(url)
        
        for rule in self.rules:
            pattern = rule.get('pattern', '')
            match_type = rule.get('match_type', 'domain')
            rule_tags = rule.get('tags', [])
            
            matched = False
            
            if match_type == 'domain':
                # Match domain or subdomain
                matched = pattern.lower() in parsed_url.netloc.lower()
            
            elif match_type == 'path':
                # Match path
                matched = pattern.lower() in parsed_url.path.lower()
            
            elif match_type == 'full':
                # Match full URL
                matched = pattern.lower() in url.lower()
            
            elif match_type == 'regex':
                # Match using regex and extract capture groups
                try:
                    match_obj = re.search(pattern, url, re.IGNORECASE)
                    if match_obj:
                        matched = True
                        # Process tags with capture group substitution
                        processed_tags = []
                        for tag in rule_tags:
                            # Substitute capture groups in tag templates
                            processed_tag = self._substitute_capture_groups(tag, match_obj)
                            processed_tags.append(processed_tag)
                        rule_tags = processed_tags
                    else:
                        matched = False
                except re.error:
                    print(f"Invalid regex pattern: {pattern}")
                    matched = False
            
            if matched:
                tags.extend(rule_tags)
        
        # Remove duplicates while preserving order
        return list(dict.fromkeys(tags))
    
    def _substitute_capture_groups(self, tag_template: str, match_obj) -> str:
        """
        Substitute capture groups in tag templates.
        
        Supports placeholders like {1}, {2}, etc. for capture groups
        and {0} for the entire match.
        
        Args:
            tag_template: Template string with placeholders like "r/{1}"
            match_obj: regex match object containing capture groups
            
        Returns:
            Processed tag string with substitutions
        """
        if '{' not in tag_template:
            # No substitutions needed
            return tag_template
        
        try:
            result = tag_template
            
            # Replace {0} with the entire match
            entire_match = match_obj.group(0) or ''
            result = result.replace('{0}', entire_match)
            
            # Replace {1}, {2}, etc. with capture groups
            for i, group in enumerate(match_obj.groups(), 1):
                placeholder = f'{{{i}}}'
                group_value = group or ''
                result = result.replace(placeholder, group_value)
            
            return result
        
        except (IndexError, AttributeError) as e:
            print(f"Warning: Could not substitute capture groups in tag template '{tag_template}': {e}")
            return tag_template


class InoreaderTagger:
    """Main class for tagging Inoreader articles"""
    
    def __init__(self, api: InoreaderAPI, matcher: URLPatternMatcher, timestamp_file: str = ".last_processed_timestamp", verbose: bool = False):
        self.api = api
        self.matcher = matcher
        self.timestamp_file = timestamp_file
        self.verbose = verbose
        self.stats = {
            'processed': 0,
            'tagged': 0,
            'skipped': 0,
            'errors': 0
        }
        self.newest_successful_timestamp = None  # Track newest timestamp from successfully processed articles
    
    def process_articles(self, max_articles: int = 200, batch_size: int = 100, dry_run: bool = False, folder_name: Optional[str] = None, use_timestamp_tracking: bool = True, force_timestamp_update: bool = False):
        """Process articles and apply tags based on URL patterns
        
        Args:
            max_articles: Maximum total articles to process
            batch_size: Number of articles to fetch and process per batch
            dry_run: If True, don't actually apply tags
            folder_name: Optional folder to filter articles
            use_timestamp_tracking: Track timestamps between runs
            force_timestamp_update: Update timestamp even when hitting max_articles limit
        """
        
        # Load last processed timestamp if using timestamp tracking
        # This becomes the 'ot' parameter (only show articles newer than this)
        since_timestamp = None
        if use_timestamp_tracking:
            since_timestamp = self._load_last_timestamp()
            if since_timestamp:
                print(f"Using microsecond timestamp from last run: {since_timestamp}")
            else:
                print("No previous timestamp found, processing recent unread articles")
        
        if folder_name:
            print(f"Fetching up to {max_articles} unread articles (batches of {batch_size}) from folder '{folder_name}'...")
        else:
            print(f"Fetching up to {max_articles} unread articles (batches of {batch_size}) from all folders...")
        
        try:
            total_processed = 0
            all_articles_processed = []
            
            while total_processed < max_articles:
                # Fetch next batch
                remaining = max_articles - total_processed
                fetch_count = min(batch_size, remaining)
                
                articles = self.api.get_unread_articles(fetch_count, folder_name, since_timestamp)
                
                if not articles:
                    if total_processed == 0:
                        print("No new articles to process")
                        print("Keeping existing timestamp for next run")
                    else:
                        print(f"\nNo more articles found (processed {total_processed} total)")
                    break
                
                print(f"\n{'='*50}")
                print(f"Processing batch: {len(articles)} articles (total so far: {total_processed})")
                print(f"{'='*50}")
                
                # Process batch and collect articles that need tagging
                batch_results = self._process_article_batch(articles, dry_run)
                
                # Track only successfully processed articles (no errors) for timestamp tracking
                for idx, result in enumerate(batch_results):
                    if result['errors'] == 0:  # Only track articles without errors
                        all_articles_processed.append(articles[idx])
                
                total_processed += len(articles)
                
                # Update statistics
                for result in batch_results:
                    if result['success']:
                        self.stats['tagged'] += 1
                    elif result['skipped']:
                        self.stats['skipped'] += 1
                    if result['errors'] > 0:
                        self.stats['errors'] += result['errors']
                    self.stats['processed'] += 1
                
                # If we got fewer articles than requested, we've processed everything
                if len(articles) < fetch_count:
                    print(f"\nReached end of unread articles (processed {total_processed} total)")
                    break
                
                # Small delay between batches
                if total_processed < max_articles:
                    time.sleep(0.5)
            
            # Track newest timestamp from all successfully processed articles
            if all_articles_processed:
                for article in all_articles_processed:
                    timestamp_usec = article.get('timestampUsec')
                    if timestamp_usec:
                        try:
                            timestamp_int = int(timestamp_usec)
                            if self.newest_successful_timestamp is None or timestamp_int > int(self.newest_successful_timestamp):
                                self.newest_successful_timestamp = str(timestamp_int)
                        except (ValueError, TypeError):
                            pass
            
            # Use the newest timestamp from successfully processed articles
            newest_timestamp = self.newest_successful_timestamp
            
            # Only save timestamp if we processed ALL available articles AND made progress
            made_progress = True
            if since_timestamp and newest_timestamp:
                try:
                    made_progress = int(newest_timestamp) > int(since_timestamp)
                except (ValueError, TypeError):
                    made_progress = True
            
            should_save_timestamp = (
                use_timestamp_tracking and 
                newest_timestamp and 
                not dry_run and
                made_progress and
                (total_processed < max_articles or force_timestamp_update)
            )
            
            if should_save_timestamp:
                self._save_last_timestamp(newest_timestamp)
                print(f"\nSaved newest microsecond timestamp for next run: {newest_timestamp}")
                if force_timestamp_update and total_processed >= max_articles:
                    print("WARNING: Timestamp updated despite hitting max-articles limit")
                    print("Some articles may have been skipped on next run")
            elif use_timestamp_tracking and not newest_timestamp and self.stats['errors'] > 0:
                print(f"\nTimestamp not updated - all articles had errors during tagging")
                print("Fix the errors and run again to process these articles")
            elif use_timestamp_tracking and newest_timestamp and not made_progress:
                print("\nNo progress made - keeping existing timestamp")
            elif total_processed >= max_articles and use_timestamp_tracking:
                print(f"\nProcessed {max_articles} articles (limit reached)")
                print("Timestamp not updated - there may be more unprocessed articles")
                print("Run again without --max-articles to process remaining articles")
                print("Or use --force-timestamp-update to update timestamp anyway (may skip articles)")
            
            self._print_stats()
            
        except Exception as e:
            print(f"Error processing articles: {e}")
            self.stats['errors'] += 1
    
    def _process_article_batch(self, articles: List[Dict], dry_run: bool) -> List[Dict]:
        """Process a batch of articles and apply tags using batch API calls
        
        Args:
            articles: List of articles to process
            dry_run: If True, don't actually apply tags
            
        Returns:
            List of result dicts with keys: success, skipped, errors
        """
        # Group articles by tags they need
        tag_to_articles = {}  # tag -> list of (article_id, article_title)
        results = []
        
        print("\nAnalyzing articles...")
        
        for article in articles:
            title = article.get('title', 'Untitled')
            article_id = article.get('id', '')
            
            # Get canonical URL
            canonical = article.get('canonical', [])
            url = canonical[0].get('href') if canonical else article.get('alternate', [{}])[0].get('href', '')
            
            if not url:
                print(f"  - Skipping '{title}': No URL found")
                results.append({'success': False, 'skipped': True, 'errors': 0})
                continue
            
            # Match URL against patterns
            tags_to_apply = self.matcher.match_url(url)
            
            if not tags_to_apply:
                if self.verbose:
                    print(f"  - No tags matched for '{title}' ({url})")
                results.append({'success': False, 'skipped': True, 'errors': 0})
                continue
            
            # Get existing tags from article categories
            existing_tags = set()
            categories = article.get('categories', [])
            for category in categories:
                if '/label/' in category:
                    tag_name = category.split('/label/')[-1]
                    existing_tags.add(tag_name)
            
            # Filter out tags that are already applied
            tags_to_add = [tag for tag in tags_to_apply if tag not in existing_tags]
            
            if not tags_to_add:
                if self.verbose:
                    print(f"  - '{title[:40]}...' already has all tags")
                results.append({'success': True, 'skipped': True, 'errors': 0})
                continue
            
            # Group by tag for batch operations
            for tag in tags_to_add:
                if tag not in tag_to_articles:
                    tag_to_articles[tag] = []
                tag_to_articles[tag].append((article_id, title, url))
            
            results.append({'success': False, 'skipped': False, 'errors': 0})  # Will update after tagging
        
        if not tag_to_articles:
            print("  No articles need tagging")
            return results
        
        # Display what we're about to do
        print(f"\nFound {len([r for r in results if not r['skipped']])} articles needing tags:")
        for tag, article_list in sorted(tag_to_articles.items()):
            print(f"  Tag '{tag}': {len(article_list)} articles")
            if self.verbose:
                for _, title, url in article_list[:3]:
                    print(f"    - {title[:50]}...")
                if len(article_list) > 3:
                    print(f"    ... and {len(article_list) - 3} more")
        
        if dry_run:
            print(f"\n[DRY RUN] Would make {len(tag_to_articles)} batch API calls")
            # Mark all as success in dry run
            for i, result in enumerate(results):
                if not result['skipped']:
                    results[i] = {'success': True, 'skipped': False, 'errors': 0}
            return results
        
        # Apply tags in batches (one API call per tag)
        print(f"\nApplying tags ({len(tag_to_articles)} batch API calls)...")
        result_index = 0
        for tag, article_list in sorted(tag_to_articles.items()):
            article_ids = [article_id for article_id, _, _ in article_list]
            
            try:
                success, error_msg = self.api.add_tag_to_articles_batch(article_ids, tag)
                if success:
                    print(f"  ✓ Applied tag '{tag}' to {len(article_ids)} articles")
                    # Mark all these articles as successfully tagged
                    for i, result in enumerate(results):
                        if not result['skipped'] and not result['success']:
                            results[i] = {'success': True, 'skipped': False, 'errors': 0}
                else:
                    print(f"  ✗ Failed to apply tag '{tag}' to {len(article_ids)} articles")
                    if error_msg:
                        print(f"    Error: {error_msg}")
                    # Mark these articles as having errors
                    for i, result in enumerate(results):
                        if not result['skipped'] and not result['success']:
                            results[i]['errors'] += 1
            except Exception as e:
                print(f"  ✗ Error applying tag '{tag}': {e}")
                if self.verbose:
                    import traceback
                    print(f"    Full traceback: {traceback.format_exc()}")
                # Mark these articles as having errors
                for i, result in enumerate(results):
                    if not result['skipped'] and not result['success']:
                        results[i]['errors'] += 1
        
        return results
    
    def _load_last_timestamp(self) -> Optional[str]:
        """Load the last processed microsecond timestamp from file (used as 'ot' parameter for next run)"""
        try:
            if os.path.exists(self.timestamp_file):
                with open(self.timestamp_file, 'r') as f:
                    return f.read().strip()
        except Exception as e:
            print(f"Warning: Could not load last timestamp: {e}")
        return None
    
    def _save_last_timestamp(self, timestamp_microseconds: str):
        """Save the newest article microsecond timestamp to file (becomes 'ot' parameter for next run)"""
        try:
            with open(self.timestamp_file, 'w') as f:
                f.write(timestamp_microseconds)
        except Exception as e:
            print(f"Warning: Could not save timestamp: {e}")
    
    def _get_newest_timestamp(self, articles: List[Dict]) -> Optional[str]:
        """Get the newest microsecond timestamp from a list of articles"""
        if not articles:
            return None
        
        newest = None
        for article in articles:
            timestamp_usec = article.get('timestampUsec')
            if timestamp_usec:
                try:
                    # Use microsecond timestamp directly
                    timestamp_int = int(timestamp_usec)
                    if newest is None or timestamp_int > int(newest):
                        newest = str(timestamp_int)
                except (ValueError, TypeError):
                    # Skip invalid timestamps
                    continue
        
        return newest
    
    def _print_stats(self):
        """Print processing statistics"""
        print("\n" + "="*50)
        print("Processing Summary:")
        print(f"  Processed: {self.stats['processed']}")
        print(f"  Tagged: {self.stats['tagged']}")
        print(f"  Skipped: {self.stats['skipped']}")
        print(f"  Errors: {self.stats['errors']}")
        print("="*50)


def main():
    """Main function"""
    import argparse
    
    parser = argparse.ArgumentParser(description='Tag Inoreader articles based on URL patterns')
    parser.add_argument('--config', default='config.json', help='Configuration file path')
    parser.add_argument('--dry-run', action='store_true', help='Run without actually applying tags')
    parser.add_argument('--max-articles', type=int, default=200, help='Maximum number of articles to process')
    parser.add_argument('--batch-size', type=int, default=100, help='Number of articles to fetch and process per batch')
    parser.add_argument('--force-timestamp-update', action='store_true', help='Update timestamp even when hitting max-articles limit (may skip articles)')
    parser.add_argument('--no-timestamp-tracking', action='store_true', help='Disable timestamp tracking (process all unread articles)')
    parser.add_argument('--reset-timestamp', action='store_true', help='Reset timestamp tracking (start fresh)')
    parser.add_argument('--verbose', '-v', action='store_true', help='Enable verbose output for debugging')
    
    args = parser.parse_args()
    
    # Load configuration
    try:
        with open(args.config, 'r') as f:
            config = json.load(f)
    except FileNotFoundError:
        print(f"Configuration file '{args.config}' not found.")
        print("Please create a config.json file. See config.example.json for reference.")
        return
    except json.JSONDecodeError as e:
        print(f"Error parsing configuration file: {e}")
        return
    
    # Initialize API
    try:
        api = InoreaderAPI(
            app_id=config['app_id'],
            app_key=config['app_key'],
            refresh_token=config.get('refresh_token')
        )
    except ValueError as e:
        if "re-authentication required" in str(e):
            print(f"\n⚠️  {e}")
            # Clear the invalid refresh token from config
            if 'refresh_token' in config:
                config.pop('refresh_token')
                with open(args.config, 'w') as f:
                    json.dump(config, f, indent=2)
                print(f"✓ Cleared invalid refresh token from {args.config}")
            # Initialize API without refresh token to trigger re-auth flow
            api = InoreaderAPI(
                app_id=config['app_id'],
                app_key=config['app_key']
            )
        else:
            raise
    
    # If no refresh token, guide user through authentication
    if not api.refresh_token:
        print("\n=== First Time Setup ===")
        print("1. Visit this URL to authorize the application:")
        print(f"\n{api.get_authorization_url()}\n")
        print("2. After authorizing, you'll be redirected to a URL like:")
        print("   http://localhost/?code=AUTHORIZATION_CODE&state=STATE_VALUE")
        print("3. Copy the ENTIRE redirect URL from your browser's address bar")
        
        redirect_url = input("\nPaste the full redirect URL here: ").strip()
        
        try:
            # Parse the redirect URL to extract code and state
            parsed_url = urlparse(redirect_url)
            params = parse_qs(parsed_url.query)
            
            if 'error' in params:
                error = params['error'][0]
                error_desc = params.get('error_description', ['Unknown error'])[0]
                print(f"\n✗ Authorization failed: {error}")
                print(f"   Error description: {error_desc}")
                return
            
            if 'code' not in params:
                print("\n✗ No authorization code found in the URL")
                print("   Make sure you copied the complete redirect URL")
                return
            
            auth_code = params['code'][0]
            state = params.get('state', [None])[0]
            
            token_data = api.exchange_code_for_token(auth_code, state)
            print("\n✓ Authentication successful!")
            print(f"\nAdd this to your {args.config}:")
            print(f'"refresh_token": "{token_data["refresh_token"]}"')
            
            # Update config file
            config['refresh_token'] = token_data['refresh_token']
            with open(args.config, 'w') as f:
                json.dump(config, f, indent=2)
            print(f"\n✓ Configuration updated in {args.config}")
            
        except Exception as e:
            print(f"\n✗ Authentication failed: {e}")
            return
    
    # Initialize matcher and tagger
    matcher = URLPatternMatcher(config['tagging_rules'])
    tagger = InoreaderTagger(api, matcher, verbose=args.verbose)
    
    # Handle timestamp reset
    if args.reset_timestamp:
        if os.path.exists(tagger.timestamp_file):
            os.remove(tagger.timestamp_file)
            print("✓ Timestamp tracking reset")
        else:
            print("No timestamp file found to reset")
        return
    
    # Get folder filter from config
    folder_name = config.get('folder_filter')
    
    # Determine timestamp tracking mode
    use_timestamp_tracking = not args.no_timestamp_tracking
    
    # Process articles
    print(f"\n{'='*50}")
    print("Starting article processing...")
    print(f"Dry run: {args.dry_run}")
    print(f"Timestamp tracking: {'Enabled' if use_timestamp_tracking else 'Disabled'}")
    if folder_name:
        print(f"Folder filter: '{folder_name}'")
    else:
        print("Folder filter: None (processing all folders)")
    print(f"{'='*50}\n")
    
    tagger.process_articles(
        max_articles=args.max_articles,
        batch_size=args.batch_size,
        dry_run=args.dry_run, 
        folder_name=folder_name,
        use_timestamp_tracking=use_timestamp_tracking,
        force_timestamp_update=args.force_timestamp_update
    )


if __name__ == '__main__':
    main()
