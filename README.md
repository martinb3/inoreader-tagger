# Inoreader Dynamic Tagging

Automatically apply tags to your Inoreader articles based on URL patterns. This script helps you organize your reading list by automatically tagging articles from specific domains, paths, or URL patterns.

## Features

- 🏷️ **Dynamic Tagging**: Automatically apply tags based on URL patterns
- 🎯 **Multiple Match Types**: Domain, path, full URL, or regex matching
- 🔄 **OAuth2 Authentication**: Secure authentication with Inoreader
- 🧪 **Dry Run Mode**: Test your rules before applying them
- 📊 **Statistics**: See how many articles were processed and tagged
- ⚙️ **Configurable**: Easy-to-edit JSON configuration file

## Prerequisites

1. **Python 3.7+**
2. **Inoreader Account** (free or paid)
3. **Inoreader API Credentials**

## Getting API Credentials

1. Go to [Inoreader Developer Portal](https://www.inoreader.com/developers/)
2. Log in with your Inoreader account
3. Create a new application
4. Note down your **App ID** and **App Key**

## Installation

1. Clone or download this repository

2. Install required dependencies:
```bash
pip install requests
```

3. Copy the example configuration:
```bash
copy config.example.json config.json
```

4. Edit `config.json` and add your API credentials:
```json
{
  "app_id": "YOUR_APP_ID",
  "app_key": "YOUR_APP_KEY",
  "refresh_token": "",
  "tagging_rules": [...]
}
```

## First Time Setup

Run the script for the first time:

```bash
python inoreader_tagger.py
```

The script will:
1. Provide you with an authorization URL
2. Ask you to authorize the application in your browser
3. Request the authorization code from the redirect URL
4. Save the refresh token to your config file

After this one-time setup, the script will automatically refresh tokens as needed.

## Configuration

### Tagging Rules

Define your tagging rules in `config.json`. Each rule has the following format:

```json
{
  "pattern": "github.com",
  "match_type": "domain",
  "tags": ["GitHub", "Development"],
  "description": "Optional description"
}
```

### Match Types

- **`domain`**: Match the domain or subdomain
  - Example: `"github.com"` matches `github.com`, `www.github.com`, `gist.github.com`

- **`path`**: Match the URL path
  - Example: `"/blog/"` matches `https://example.com/blog/post-1`

- **`full`**: Match anywhere in the full URL
  - Example: `"python"` matches `https://example.com/python-tutorial`

- **`regex`**: Match using regular expressions
  - Example: `"youtube\\.com|youtu\\.be"` matches both YouTube domains

### Example Rules

```json
{
  "tagging_rules": [
    {
      "pattern": "github.com",
      "match_type": "domain",
      "tags": ["GitHub", "Development"]
    },
    {
      "pattern": "/api/",
      "match_type": "path",
      "tags": ["API", "Documentation"]
    },
    {
      "pattern": "python|django|flask",
      "match_type": "regex",
      "tags": ["Python"]
    },
    {
      "pattern": "reddit.com/r/programming",
      "match_type": "full",
      "tags": ["Reddit", "Programming"]
    }
  ]
}
```

## Usage

### Basic Usage

Process and tag articles:
```bash
python inoreader_tagger.py
```

### Dry Run

Test your rules without actually applying tags:
```bash
python inoreader_tagger.py --dry-run
```

### Limit Articles

Process only a specific number of articles:
```bash
python inoreader_tagger.py --max-articles 50
```

### Custom Config File

Use a different configuration file:
```bash
python inoreader_tagger.py --config my-config.json
```

## Command Line Options

```
--config          Path to configuration file (default: config.json)
--dry-run         Run without actually applying tags
--max-articles    Maximum number of articles to process (default: 100)
```

## How It Works

1. **Fetch Articles**: Retrieves unread articles from your Inoreader account
2. **Match Patterns**: Compares each article's URL against your tagging rules
3. **Apply Tags**: Automatically applies matching tags to articles
4. **Report**: Shows statistics about processed articles

## Scheduling

You can schedule this script to run automatically:

### Windows (Task Scheduler)

1. Open Task Scheduler
2. Create a new task
3. Set trigger (e.g., daily at 9 AM)
4. Set action: `python "C:\path\to\inoreader_tagger.py"`

### Linux/Mac (Cron)

Add to crontab:
```bash
0 9 * * * /usr/bin/python3 /path/to/inoreader_tagger.py
```

## Tips

1. **Start with Dry Run**: Always test new rules with `--dry-run` first
2. **Be Specific**: More specific patterns reduce false matches
3. **Multiple Tags**: One URL can match multiple rules and receive multiple tags
4. **Regex Testing**: Test complex regex patterns at [regex101.com](https://regex101.com)
5. **Rate Limiting**: The script includes small delays to avoid API rate limits

## Troubleshooting

### Authentication Errors
- Make sure your App ID and App Key are correct
- Try deleting the refresh_token and re-authenticating

### No Articles Found
- Check that you have unread articles in Inoreader
- Verify your API credentials have proper permissions

### Tags Not Applying
- Run with `--dry-run` to see if tags are being matched
- Check that tag names don't have special characters
- Verify your Inoreader account can create tags

## Advanced Usage

### Extending the Script

You can modify `inoreader_tagger.py` to add custom functionality:

- **Custom matching logic**: Edit the `URLPatternMatcher` class
- **Different article streams**: Modify `get_unread_articles()` to use different streams
- **Additional metadata**: Use article title, summary, or feed source for matching
- **Tag management**: Add functionality to create, rename, or delete tags

### API Methods

The `InoreaderAPI` class provides these methods:

- `get_stream_contents()` - Get articles from any stream
- `get_unread_articles()` - Get unread articles
- `get_tags()` - List all user tags
- `add_tag_to_article()` - Add a tag to an article
- `remove_tag_from_article()` - Remove a tag from an article

## License

MIT License - Feel free to modify and use as needed!

## Support

For issues or questions:
- Check [Inoreader API Documentation](https://www.inoreader.com/developers/)
- Review your configuration file for syntax errors
- Run with `--dry-run` to debug tagging rules

## Changelog

### v1.0.0
- Initial release
- Support for domain, path, full, and regex matching
- OAuth2 authentication
- Dry run mode
- Processing statistics
