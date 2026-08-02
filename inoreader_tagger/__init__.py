"""Inoreader Tagger.

Automatically applies tags to Inoreader articles based on URL patterns, either
as a one-shot CLI run or as a scheduled multi-account service with a status
page.

The names re-exported here are the ones the standalone scripts import.
"""

from .api import InoreaderAPI, InoreaderAuthError, InoreaderError
from .matcher import URLPatternMatcher, validate_rules
from .tagger import RunOutcome, RunParameters, TaggerEngine

__all__ = [
    "InoreaderAPI",
    "InoreaderAuthError",
    "InoreaderError",
    "URLPatternMatcher",
    "validate_rules",
    "TaggerEngine",
    "RunParameters",
    "RunOutcome",
]

__version__ = "2.0.0"
