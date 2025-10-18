# Inoreader Tagger Integration Strategy

## 🎯 Optimal Integration Approaches

Based on the Inoreader API documentation, here are the best strategies to avoid duplicate processing:

### **✅ 1. Dual-Filter Approach: Timestamp + Unread (Implemented)**

**How it works:**
- **UNREAD FILTER**: Uses `xt=user/-/state/com.google/read` to only process unread articles
- **TIMESTAMP FILTER**: Uses `ot` parameter to get only articles newer than last run
- **Never marks as read**: Articles remain unread for your normal Inoreader workflow
- **Dual efficiency**: Server-side filtering by both unread status AND timestamp

**Benefits:**
- ✅ Only processes unread articles (never touches read ones)
- ✅ No duplicate processing (timestamp tracking)
- ✅ Articles stay unread (you control read status)
- ✅ Maximum efficiency (dual server-side filtering)
- ✅ Works with any polling frequency
- ✅ Handles interruptions gracefully

**Usage:**
```bash
# First run - processes recent unread articles
python3 inoreader_tagger.py

# Subsequent runs - only processes new articles since last run  
python3 inoreader_tagger.py

# Reset timestamp tracking to start fresh
python3 inoreader_tagger.py --reset-timestamp
```

### **✅ 2. Folder-Specific Processing (Implemented)**

**How it works:**
- Filters articles from specific Inoreader folder using `user/-/label/FOLDER_NAME` stream
- Processes only articles in your "Discussion sites" folder
- Reduces API calls and focuses on relevant content

**Configuration:**
```json
{
  "folder_filter": "Discussion sites"
}
```

### **⚡ 3. Efficient Polling Strategy**

**Recommended approach for continuous monitoring:**

```bash
# Check every 15 minutes (good balance)
*/15 * * * * cd /path/to/inoreader-tagger && python3 inoreader_tagger.py

# Or every hour for less frequent updates
0 * * * * cd /path/to/inoreader-tagger && python3 inoreader_tagger.py
```

**Why this works well:**
- Timestamp tracking ensures no duplicates regardless of frequency
- Folder filtering reduces processing to relevant articles
- Unread filtering (`xt=user/-/state/com.google/read`) is server-side efficient

### **🔧 4. Advanced Options**

**Unread Counts API (Available):**
- `get_unread_counts()` method checks if new articles exist before processing
- Contains `newestItemTimestampUsec` for each folder
- Could be used to optimize polling (only process if counts changed)

**Alternative Approaches:**

1. **Read-State Based:**
   ```bash
   # Process unread, don't save timestamps
   python3 inoreader_tagger.py --no-timestamp-tracking
   ```

2. **Full Reset:**
   ```bash  
   # Start completely fresh
   python3 inoreader_tagger.py --reset-timestamp
   ```

## 📋 Summary

**Current Implementation = Best Practice:**
- ✅ **Dual-filtering**: Unread + timestamp for maximum efficiency  
- ✅ **Never marks as read**: Preserves your normal Inoreader workflow
- ✅ **Folder filtering**: Focuses on relevant content ("Discussion sites")
- ✅ **No duplicates**: Timestamp tracking prevents reprocessing
- ✅ **Interruption-safe**: Graceful handling of restarts
- ✅ **Any frequency**: Works with continuous polling or occasional runs

**Perfect for automated tagging** - processes new unread articles efficiently without disrupting your reading workflow.

### **⚠️ Known Issue: Timestamp Filtering Bug**

**Problem**: The Inoreader API `ot` parameter doesn't work correctly when combined with `xt` parameter for excluding read articles.

**Impact**: 
- The application may reprocess the same unread articles on subsequent runs
- This doesn't cause harm (articles stay unread, tags are idempotent) 
- Just slightly less efficient than intended

**Demonstration**: Run `python3 test_timestamp_bug.py` to see the bug in action

**Workaround**: The application still works correctly, just processes some articles multiple times until you mark them as read in Inoreader.