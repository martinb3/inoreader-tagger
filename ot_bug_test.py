#!/usr/bin/env python3
"""
Inoreader API 'ot' parameter behavior investigation

We've observed that the 'ot' parameter seems to behave differently 
depending on the timestamp value used:
- Recent timestamps (6 hours): Returns older articles  
- Medium timestamps (1 day): Returns articles from both sides
- Older timestamps (3+ days): Returns newer articles

We're not sure if this is intended behavior or our misunderstanding 
of how the parameter should work. This test helps investigate the 
behavior to better understand the API.
"""

import json
import time
import requests
from datetime import datetime
from inoreader_tagger import InoreaderAPI

def test_ot_bug():
    """Minimal test to demonstrate the 'ot' parameter bug"""
    
    # Load API credentials
    try:
        with open('config.json', 'r') as f:
            config = json.load(f)
    except FileNotFoundError:
        print("❌ Error: config.json not found")
        return
        
    api = InoreaderAPI(config['app_id'], config['app_key'], config.get('refresh_token'))
    
    print("� Investigating Inoreader 'ot' parameter behavior")
    print("=" * 60)
    print("Testing with only 'ot' parameter (no other filters)")
    print()
    
    # Use reading-list to ensure we have articles
    stream_id = "user/-/state/com.google/reading-list"
    url = f"{api.BASE_URL}/stream/contents/{stream_id}"
    now = int(time.time())
    
    # Test 3 key timestamps that show different behaviors
    tests = [
        ("6 hours ago", now - (6 * 3600)),
        ("1 day ago", now - (24 * 3600)),  
        ("1 week ago", now - (7 * 24 * 3600))
    ]
    
    results = []
    
    for test_name, timestamp in tests:
        readable_time = datetime.fromtimestamp(timestamp).strftime('%m-%d %H:%M')
        print(f"🕒 Testing ot={test_name} ({readable_time})")
        
        # Make API call with ONLY ot parameter
        params = {'ot': str(timestamp), 'n': 20, 'output': 'json'}
        
        try:
            response = requests.get(url, headers=api._get_headers(), params=params)
            if response.status_code == 429:
                print("  ⚠️  Rate limited - wait and try again")
                continue
            response.raise_for_status()
            
            articles = response.json().get('items', [])
            
            if not articles:
                print("  No articles returned")
                continue
                
            # Analyze what we got
            newer = sum(1 for a in articles if a.get('published', 0) > timestamp)
            older = sum(1 for a in articles if a.get('published', 0) < timestamp)
            
            print(f"  Returned: {len(articles)} articles")
            print(f"  - {newer} NEWER than timestamp")
            print(f"  - {older} OLDER than timestamp")
            
            # Determine behavior
            if newer > 0 and older == 0:
                behavior = "newer articles only"
                print(f"  → Returns: {behavior}")
            elif older > 0 and newer == 0:
                behavior = "older articles only"
                print(f"  → Returns: {behavior}")
            elif newer > 0 and older > 0:
                behavior = "articles from both sides"
                print(f"  → Returns: {behavior} (unexpected)")
            else:
                behavior = "unclear pattern"
                print(f"  → Returns: {behavior}")
            
            results.append((test_name, behavior))
            
        except Exception as e:
            print(f"  ❌ Error: {e}")
        
        print()
    
    # Summary
    print("� Summary")
    print("=" * 60)
    
    if len(set(r[1] for r in results)) > 1:
        print("The 'ot' parameter behavior appears to vary by timestamp:")
        print()
        for test_name, behavior in results:
            print(f"  {test_name:12s} → {behavior}")
        print()
        print("This seems inconsistent - we'd expect the same behavior")
        print("regardless of which timestamp is used.")
        print()
        print("Could you clarify the intended behavior of the 'ot' parameter?")
    else:
        print("Behavior appears consistent in this test run.")
        print("You might try running with different timestamps or data.")

if __name__ == "__main__":
    print("This script investigates Inoreader API 'ot' parameter behavior.")
    print("We're trying to understand how it's supposed to work.")
    print()
    test_ot_bug()