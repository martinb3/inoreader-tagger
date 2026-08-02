"""Starter tagging rules given to a newly connected account.

Deliberately small and obviously editable — the point is that a new user sees
something working immediately and then edits it, not that this list is good.
"""

DEFAULT_RULES = [
    {
        "pattern": "github.com",
        "match_type": "domain",
        "tags": ["GitHub", "Development"],
        "description": "Tag all GitHub articles",
    },
    {
        "pattern": "news.ycombinator.com",
        "match_type": "domain",
        "tags": ["HackerNews", "Tech News"],
        "description": "Tag Hacker News articles",
    },
    {
        "pattern": "youtube\\.com|youtu\\.be",
        "match_type": "regex",
        "tags": ["Video", "YouTube"],
        "description": "Tag YouTube videos",
    },
    {
        "pattern": "reddit\\.com/r/([^/]+)",
        "match_type": "regex",
        "tags": ["Reddit", "r/{1}"],
        "description": "Tag Reddit posts with their subreddit",
    },
    {
        "pattern": "/blog/",
        "match_type": "path",
        "tags": ["Blog"],
        "description": "Tag any URL with /blog/ in the path",
    },
]
