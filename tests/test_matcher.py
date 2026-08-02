from inoreader_tagger.matcher import URLPatternMatcher, validate_rules


def test_domain_match_is_case_insensitive_and_covers_subdomains():
    matcher = URLPatternMatcher([
        {"pattern": "github.com", "match_type": "domain", "tags": ["GitHub"]}
    ])
    assert matcher.match_url("https://GIST.GitHub.com/x") == ["GitHub"]
    assert matcher.match_url("https://example.com/github.com") == []


def test_path_match_only_looks_at_the_path():
    matcher = URLPatternMatcher([
        {"pattern": "/blog/", "match_type": "path", "tags": ["Blog"]}
    ])
    assert matcher.match_url("https://example.com/blog/post") == ["Blog"]
    assert matcher.match_url("https://blog.example.com/x") == []


def test_regex_capture_groups_expand_into_tags():
    matcher = URLPatternMatcher([
        {
            "pattern": r"reddit\.com/r/([^/]+)",
            "match_type": "regex",
            "tags": ["Reddit", "r/{1}"],
        }
    ])
    assert matcher.match_url("https://reddit.com/r/programming/comments/x") == [
        "Reddit",
        "r/programming",
    ]


def test_tags_are_deduplicated_across_rules_preserving_order():
    matcher = URLPatternMatcher([
        {"pattern": "example.com", "match_type": "domain", "tags": ["News", "Tech"]},
        {"pattern": "/x/", "match_type": "path", "tags": ["Tech", "Deep"]},
    ])
    assert matcher.match_url("https://example.com/x/y") == ["News", "Tech", "Deep"]


def test_invalid_regex_is_skipped_not_fatal():
    matcher = URLPatternMatcher([
        {"pattern": "([unclosed", "match_type": "regex", "tags": ["Nope"]},
        {"pattern": "example.com", "match_type": "domain", "tags": ["Yes"]},
    ])
    assert matcher.match_url("https://example.com/") == ["Yes"]


def test_validate_rules_flags_the_common_mistakes():
    problems = validate_rules([
        {"pattern": "", "match_type": "domain", "tags": ["A"]},
        {"pattern": "x", "match_type": "nonsense", "tags": ["A"]},
        {"pattern": "x", "match_type": "domain", "tags": []},
        {"pattern": "([bad", "match_type": "regex", "tags": ["A"]},
    ])
    assert len(problems) == 4
    assert validate_rules([]) == []
    assert validate_rules({"not": "a list"}) == ["Rules must be a JSON array."]
