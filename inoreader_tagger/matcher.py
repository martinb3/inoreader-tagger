"""URL pattern matching — decides which tags an article's URL earns.

Match semantics are unchanged from the original script so existing rule sets
keep working; the only addition is validate_rules(), used by the web UI to
reject a bad rule before it is saved rather than at the next scheduled run.
"""

import logging
import re
from typing import List
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

MATCH_TYPES = ("domain", "path", "full", "regex")


class URLPatternMatcher:
    """Matches URL patterns to determine tags.

    Rules format:
        [
            {
                "pattern": "github.com",
                "match_type": "domain",   # or "path", "full", "regex"
                "tags": ["GitHub", "Development"]
            }
        ]
    """

    def __init__(self, rules: List[dict]):
        self.rules = rules or []

    def match_url(self, url: str) -> List[str]:
        """Return the tags applicable to `url`, in rule order, deduplicated."""
        tags: List[str] = []
        parsed_url = urlparse(url)

        for rule in self.rules:
            pattern = rule.get("pattern", "")
            match_type = rule.get("match_type", "domain")
            rule_tags = rule.get("tags", [])

            matched = False

            if match_type == "domain":
                matched = pattern.lower() in parsed_url.netloc.lower()

            elif match_type == "path":
                matched = pattern.lower() in parsed_url.path.lower()

            elif match_type == "full":
                matched = pattern.lower() in url.lower()

            elif match_type == "regex":
                try:
                    match_obj = re.search(pattern, url, re.IGNORECASE)
                except re.error:
                    logger.warning("Invalid regex pattern in rule: %s", pattern)
                    continue

                if match_obj:
                    matched = True
                    rule_tags = [
                        self._substitute_capture_groups(tag, match_obj) for tag in rule_tags
                    ]

            if matched:
                tags.extend(rule_tags)

        # Deduplicate while preserving the order rules were written in.
        return list(dict.fromkeys(tag for tag in tags if tag))

    @staticmethod
    def _substitute_capture_groups(tag_template: str, match_obj) -> str:
        """Expand {0} (whole match) and {1}, {2}... (capture groups) in a tag."""
        if "{" not in tag_template:
            return tag_template

        try:
            result = tag_template.replace("{0}", match_obj.group(0) or "")
            for index, group in enumerate(match_obj.groups(), 1):
                result = result.replace(f"{{{index}}}", group or "")
            return result
        except (IndexError, AttributeError) as exc:
            logger.warning(
                "Could not substitute capture groups in tag template %r: %s", tag_template, exc
            )
            return tag_template


def validate_rules(rules) -> List[str]:
    """Return a list of human-readable problems with a rule set. Empty means valid."""
    problems: List[str] = []

    if not isinstance(rules, list):
        return ["Rules must be a JSON array."]

    for index, rule in enumerate(rules):
        where = f"Rule {index + 1}"

        if not isinstance(rule, dict):
            problems.append(f"{where}: must be an object.")
            continue

        pattern = rule.get("pattern")
        if not pattern or not isinstance(pattern, str):
            problems.append(f"{where}: needs a non-empty string 'pattern'.")

        match_type = rule.get("match_type", "domain")
        if match_type not in MATCH_TYPES:
            problems.append(
                f"{where}: 'match_type' must be one of {', '.join(MATCH_TYPES)} (got {match_type!r})."
            )

        tags = rule.get("tags")
        if not isinstance(tags, list) or not tags:
            problems.append(f"{where}: needs a non-empty 'tags' array.")
        elif not all(isinstance(tag, str) and tag for tag in tags):
            problems.append(f"{where}: every entry in 'tags' must be a non-empty string.")

        if match_type == "regex" and isinstance(pattern, str) and pattern:
            try:
                re.compile(pattern)
            except re.error as exc:
                problems.append(f"{where}: invalid regex — {exc}.")

    return problems
