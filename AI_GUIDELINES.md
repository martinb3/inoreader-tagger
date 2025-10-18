# Agent Rules for Inoreader Tagger Project

This file contains guidelines and context for AI assistants (like Claude Sonnet 4.x) working on the Inoreader Tagger project in VS Code.

## Project Overview

**Inoreader Tagger** is a Python script that automatically applies tags to Inoreader articles based on URL patterns. It supports multiple matching types including domain, path, full URL, and regex with capture groups.

### Key Files
- `inoreader_tagger.py` - Main application with API wrapper and tagging logic
- `config.json` - User configuration (gitignored, use `config.example.json` as template)
- `config.example.json` - Configuration template
- `requirements.txt` - Python dependencies
- `README.md` - Comprehensive documentation

## Code Style and Standards

### Python Guidelines
- Follow PEP 8 style guidelines
- Use type hints where appropriate (already implemented in most functions)
- Maintain existing class structure:
  - `InoreaderAPI` - API operations and OAuth2 handling
  - `URLPatternMatcher` - URL pattern matching and regex capture groups
  - `InoreaderTagger` - Main application logic
- Preserve error handling patterns and user-friendly error messages
- Keep the `--dry-run` functionality intact for testing

### Documentation Standards
- Maintain comprehensive docstrings for all classes and methods
- Update README.md when adding new features or changing configuration options
- Include practical examples in documentation
- Keep the configuration examples in sync with actual functionality

## Architecture Principles

### OAuth2 Flow
- Preserve the secure OAuth2 implementation with state parameter validation
- Handle token refresh automatically
- Store tokens securely in the config file
- Provide clear user instructions for first-time setup

### Pattern Matching System
- Support all existing match types: `domain`, `path`, `full`, `regex`
- Maintain regex capture group functionality with `{0}`, `{1}`, `{2}` placeholders
- Preserve case-insensitive matching where appropriate
- Keep the tag deduplication logic

### Configuration Management
- Maintain backwards compatibility with existing config files
- Validate configuration on startup
- Provide helpful error messages for configuration issues
- Keep sensitive data (API keys, tokens) in config.json (gitignored)

## Feature Development Guidelines

### Adding New Match Types
1. Extend the `URLPatternMatcher.get_tags_for_url()` method
2. Update the README.md documentation with examples
3. Add validation in the configuration loading
4. Consider backwards compatibility

### API Enhancements
1. Follow Inoreader API rate limiting best practices
2. Handle API errors gracefully with user-friendly messages
3. Maintain the existing retry logic for network issues
4. Preserve OAuth2 token management

### New Configuration Options
1. Add to both `config.example.json` and document in README.md
2. Provide sensible defaults
3. Validate new options on startup
4. Consider migration path for existing users

## Testing Guidelines

### Manual Testing
- Always test with `--dry-run` mode first
- Test OAuth2 flow with invalid/expired tokens
- Verify regex patterns with various URL formats
- Test with different article volumes using `--max-articles`

### Edge Cases to Consider
- Invalid regex patterns in configuration
- Network connectivity issues
- API rate limiting
- Empty or malformed article data
- Special characters in tag names
- Large numbers of articles

## Security Considerations

### API Credentials
- Never commit actual API credentials to the repository
- Keep `config.json` in `.gitignore`
- Use secure token storage practices
- Validate OAuth2 state parameters

### Input Validation
- Validate regex patterns before compilation
- Sanitize tag names and descriptions
- Handle malformed URLs gracefully
- Validate configuration file structure

## Common Tasks and Patterns

### Adding a New CLI Option
1. Update the argument parser in `main()`
2. Pass the option through to relevant classes
3. Document in README.md under "Command Line Options"
4. Test with both short and long argument forms

### Extending Tag Processing
1. Modify `URLPatternMatcher._substitute_capture_groups()` for new placeholder types
2. Update regex documentation in README.md
3. Add examples showing the new functionality
4. Consider backwards compatibility with existing tag templates

### API Method Extensions
1. Follow the existing pattern in `InoreaderAPI` class
2. Handle authentication and rate limiting consistently
3. Return structured data that matches existing patterns
4. Add appropriate error handling and logging

## VS Code Integration

### Recommended Extensions
- Python extension for syntax highlighting and debugging
- Python Docstring Generator for maintaining documentation
- GitLens for git integration and history
- Regex Previewer for testing regex patterns

### Debug Configuration
The project can be debugged using VS Code's Python debugger:
- Set breakpoints in `inoreader_tagger.py`
- Use `--dry-run` for safe debugging
- Test with a limited number of articles using `--max-articles 10`

### Workspace Settings
Consider these workspace settings for consistent development:
```json
{
    "python.defaultInterpreterPath": "./venv/bin/python",
    "python.linting.enabled": true,
    "python.linting.pylintEnabled": true,
    "files.exclude": {
        "**/__pycache__": true,
        "config.json": true
    }
}
```

## Version Control Guidelines

### Commit Messages
- Use conventional commit format: `type(scope): description`
- Types: `feat`, `fix`, `docs`, `style`, `refactor`, `test`, `chore`
- Include context about what changed and why
- Reference issues if applicable

### Branch Strategy
- Use descriptive branch names: `feature/oauth-improvements`, `fix/regex-escaping`
- Keep commits focused and atomic
- Update documentation in the same commit as code changes

### File Management
- Keep `config.json` in `.gitignore` (already configured)
- Include `config.example.json` with sanitized examples
- Update requirements.txt when adding dependencies

This document should be updated as the project evolves to maintain accurate guidance for AI assistants and human developers.