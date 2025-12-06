# Dispatcharr VOD Fix

A plugin for [Dispatcharr](https://github.com/Dispatcharr/Dispatcharr) that fixes VOD (Video on Demand) playback issues with TiviMate and other Android IPTV clients.

**GitHub Repository:** https://github.com/cedric-marcoux/dispatcharr_vod_fix

---

## ⚠️ IMPORTANT: Installation from GitHub

When downloading this plugin from GitHub (via "Download ZIP"), the file will be named `dispatcharr_vod_fix-main.zip` (GitHub adds `-main` suffix for the main branch).

### Recommended Method: WebUI Import

1. **Rename the ZIP file** from `dispatcharr_vod_fix-main.zip` to `dispatcharr_vod_fix.zip`
2. Go to Dispatcharr **Settings → Plugins**
3. Click **Import** and select your renamed ZIP file
4. **Enable** the plugin after import

The WebUI import handles file permissions automatically.

### Manual Method (Advanced)

If you extract files directly to the plugins directory, you **MUST** manage file permissions yourself:

```bash
cd /path/to/dispatcharr/data/plugins/
mv dispatcharr_vod_fix-main dispatcharr_vod_fix
chmod 644 dispatcharr_vod_fix/*.py
chown 1000:1000 dispatcharr_vod_fix/*
docker compose restart dispatcharr
```

If you don't rename the folder, the plugin **will not load** and you'll still see "All profiles at capacity" errors.

---

## Related Issues

This plugin addresses the following Dispatcharr issues:

- **[#451 - VOD not working with TiviMate](https://github.com/Dispatcharr/Dispatcharr/issues/451)** - TiviMate makes multiple simultaneous Range requests, causing "All profiles at capacity" errors
- **[#533 - Connection counting for VOD streams](https://github.com/Dispatcharr/Dispatcharr/issues/533)** - Provider connections are counted per HTTP request instead of per actual stream

## The Problem

TiviMate and similar Android clients make **multiple simultaneous HTTP Range requests** when playing MKV/VOD files:

```
Request 1: GET /movie/uuid           (no Range)      → Probe file size/metadata
Request 2: GET /movie/uuid           (Range: bytes=X-) → Read metadata at EOF
Request 3: GET /movie/uuid           (Range: bytes=Y-) → Actual playback start
```

Dispatcharr counts **each HTTP request** as a separate provider connection, incrementing the `profile_connections` counter in Redis. With `max_streams=1`, the 2nd and 3rd requests are rejected with "All profiles at capacity" before the first request even completes.

**Symptoms:**
- VOD/Movies work on Apple TV (iPlayTV) but fail on Android (TiviMate)
- Error in logs: `[PROFILE-SELECTION] All profiles at capacity for M3U account`
- Movies start loading then immediately fail
- Connection counter shows more connections than actual streams

## The Solution

This plugin tracks VOD connections by **client IP + content UUID** instead of per-HTTP-request. Multiple Range requests from the same client for the same content share a single connection slot.

### How It Works

```
┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│  TiviMate   │     │ Dispatcharr │     │   Provider  │
│  (Client)   │     │   (Proxy)   │     │  (900900.eu)│
└──────┬──────┘     └──────┬──────┘     └──────┬──────┘
       │                   │                   │
       │ Request 1 (probe) │                   │
       │──────────────────>│                   │
       │                   │ Create slot       │
       │                   │ profile_conn: 1   │
       │                   │──────────────────>│
       │                   │                   │
       │ Request 2 (meta)  │                   │
       │──────────────────>│                   │
       │                   │ REUSE slot        │
       │                   │ (skip increment)  │
       │                   │──────────────────>│
       │                   │                   │
       │ Request 3 (play)  │                   │
       │──────────────────>│                   │
       │                   │ REUSE slot        │
       │                   │ (skip increment)  │
       │                   │<─────────────────>│
       │<─────────────────>│   Stream data     │
       │   Stream data     │                   │
```

1. **Slot Creation**: When a client requests VOD content, a "slot" is created in Redis with a 10-second grace period
2. **Slot Reuse**: Subsequent requests from the same client for the same content reuse the existing slot
3. **Single Count**: Only the first request increments the provider connection counter
4. **Smart Cleanup**: The connection is only decremented when all requests for that slot are complete

## Installation

1. Copy the `dispatcharr_vod_fix` folder to your Dispatcharr plugins directory:
   ```
   /data/plugins/dispatcharr_vod_fix/
   ```

2. Set correct permissions (if needed):
   ```bash
   chmod 644 /data/plugins/dispatcharr_vod_fix/*.py
   chown 1000:1000 /data/plugins/dispatcharr_vod_fix/*
   ```

3. Restart Dispatcharr:
   ```bash
   docker compose restart dispatcharr
   ```

4. The plugin auto-installs on startup. Check logs for:
   ```
   [VOD-Fix] Installing VOD connection hooks...
   [VOD-Fix] All hooks installed successfully
   [VOD-Fix] Hooks installed (will check enabled state at runtime)
   ```

## Configuration

No configuration required. The plugin works automatically once installed.

## Compatibility

- **Dispatcharr**: Tested with v0.12 and v0.13
- **Clients**: TiviMate (Android), iPlayTV (Apple TV), UHF, Snappier iOS, and other Xtream Codes compatible clients
- **Content Types**: Movies (VOD) via `/proxy/vod/movie/` endpoints, Series via Xtream Codes API

## Technical Details

### Redis Keys

| Key | Purpose | TTL |
|-----|---------|-----|
| `vod_client_slot:{ip}:{content_uuid}` | Tracks client+content → profile mapping | 300s |

### Slot Data Structure

```json
{
  "profile_id": "3",
  "created_at": "1234567890.123",
  "last_activity": "1234567890.456",
  "active_requests": "2",
  "counted": "1"
}
```

### Patched Functions

| Function | Original Behavior | Patched Behavior |
|----------|------------------|------------------|
| `VODStreamView._get_m3u_profile()` | Checks profile capacity | First checks for existing client slot |
| `MultiWorkerVODConnectionManager._increment_profile_connections()` | Always increments counter | Skips if slot already counted |
| `MultiWorkerVODConnectionManager._decrement_profile_connections()` | Always decrements counter | Only decrements when all requests done |
| `MultiWorkerVODConnectionManager.stream_content_with_session()` | Streams content | Stores client context for patches |
| `apps.output.views.xc_series_stream()` | Uses `.get()` (fails with multiple) | Looks up by `stream_id` first, fallback to `episode_id` |
| `apps.output.views.xc_get_series_info()` | Returns internal IDs, allows nulls | Replaces IDs with provider stream_ids, sanitizes nulls |

### Grace Period

The plugin uses a 10-second grace period (`GRACE_PERIOD_SECONDS = 10.0`). Requests within this window of slot creation are allowed to reuse the slot, even if `active_requests` is temporarily 0 (handles race conditions).

## Logs

The plugin logs with prefix `[VOD-Fix]`:

```
# Slot creation
[VOD-Fix] Created slot for 192.168.1.100/abc123..., profile 3

# Slot reuse (subsequent requests)
[VOD-Fix] Client 192.168.1.100 reusing slot for abc123... - profile 3, active: 2

# Skip duplicate increment
[VOD-Fix] Skipping increment for 192.168.1.100/abc123... - already counted, current: 1

# Cleanup when all requests complete
[VOD-Fix] All requests done for 192.168.1.100/abc123..., decremented profile 3

# Orphan counter detection and automatic cleanup (v1.1.0+)
[VOD-Fix] Orphan counter detected for profile 3: counter=1, active_slots=0. Resetting to 0.
[VOD-Fix] Orphan counter cleaned, retrying profile selection
```

## Troubleshooting

### Plugin not loading

Check file permissions:
```bash
chmod 644 /data/plugins/dispatcharr_vod_fix/*.py
chown 1000:1000 /data/plugins/dispatcharr_vod_fix/*
docker compose restart dispatcharr
```

### VOD still failing

1. Check if plugin is loaded in logs:
   ```bash
   docker compose logs dispatcharr | grep "VOD-Fix"
   ```

2. Reset stuck connection counter:
   ```bash
   docker exec dispatcharr redis-cli SET "profile_connections:3" "0"
   ```

3. Verify the content uses `/proxy/vod/movie/` endpoint (Series use different endpoints)

### Ghost connections

**As of v1.1.0**, the plugin automatically detects and resets orphan counters. If a profile appears "at capacity" but no active slots exist, the counter is automatically reset before rejecting the request.

If you still experience issues, you can manually check and reset:
```bash
# Check current value
docker exec dispatcharr redis-cli GET "profile_connections:3"

# Manual reset (usually not needed with v1.1.0+)
docker exec dispatcharr redis-cli SET "profile_connections:3" "0"

# Check for orphaned slots
docker exec dispatcharr redis-cli KEYS "vod_client_slot:*"

# Delete orphaned slots (if any)
docker exec dispatcharr redis-cli DEL "vod_client_slot:192.168.1.100:abc123"
```

### Debug logging

To see detailed debug logs, set Dispatcharr's log level to DEBUG in your configuration.

## Known Limitations

- Grace period is fixed at 10 seconds (not configurable via UI)
- Slot TTL is fixed at 300 seconds (5 minutes)

## File Structure

```
dispatcharr_vod_fix/
├── __init__.py      # Package marker and exports
├── plugin.py        # Plugin metadata and auto-install logic
├── hooks.py         # Monkey-patches for VOD connection handling
└── README.md        # This documentation
```

## Version History

### 1.3.0 (2025-12-06)
- **Fix iPlayTV series playback crash**: iPlayTV crashed when loading episode lists due to two issues in Dispatcharr v0.13:
  1. **Episode ID mismatch**: Dispatcharr returns internal episode IDs (e.g., `774`) but clients expect the provider's stream_id (e.g., `1259857`). When iPlayTV builds the playback URL `/series/user/pass/1259857.mkv`, Dispatcharr couldn't find the episode.
  2. **Null values in JSON**: Fields like `custom_sid`, `direct_source` returned `null` instead of empty strings, causing strict JSON parsers (iPlayTV) to crash.
- Added `patched_xc_get_series_info()` function:
  - Replaces internal episode IDs with provider's `stream_id` from `M3UEpisodeRelation`
  - Sanitizes `null` values to empty strings for iPlayTV compatibility
- Modified `patched_xc_series_stream()` to lookup episodes by `stream_id` field first, with fallback to internal `episode_id`
- Series now work on all clients: iPlayTV, UHF, Snappier iOS, TiviMate

### 1.2.0 (2025-12-06)
- **Fix series streaming with multiple M3U accounts**: Fixes `MultipleObjectsReturned` error when an episode exists in multiple M3U accounts
  - Error: `apps.vod.models.M3UEpisodeRelation.MultipleObjectsReturned: get() returned more than one M3UEpisodeRelation -- it returned 2!`
  - This is a bug in Dispatcharr v0.13's `xc_series_stream` function which uses `.get()` instead of `.first()`
  - The patched version uses `.first()` ordered by M3U account priority to select the best source
  - Now series episodes play correctly even when available from multiple providers
- Added `patched_xc_series_stream()` function to fix the bug
- Improved logging when selecting from multiple sources

### 1.1.0 (2025-11-30)
- **Automatic orphan counter cleanup**: When a profile appears "at capacity" but no active slots exist, the plugin now automatically detects and resets stuck counters
  - This fixes issues where abrupt client disconnects (app crash, network failure, TCP reset) leave the `profile_connections` counter stuck at a non-zero value
  - Previously required manual intervention via `redis-cli SET profile_connections:X 0`
  - Now the plugin scans Redis for active slots and resets orphan counters automatically before rejecting requests
- Added `count_active_slots_for_profile()` function to scan Redis slots
- Added `cleanup_orphan_counter()` function for automatic counter recovery

### 1.0.0 (2025-11-30)
- Initial release
- Fixes TiviMate VOD playback with multi-Range request handling
- Tracks connections by client+content instead of per-request
- Implements connection slot system with Redis
- Auto-installs on Dispatcharr startup

## Contributing

Issues and pull requests are welcome at:
https://github.com/cedric-marcoux/dispatcharr_vod_fix

## Author

**Cedric Marcoux**
- GitHub: https://github.com/cedric-marcoux

## License

MIT License - Feel free to use and modify.

## Acknowledgments

- [Dispatcharr](https://github.com/Dispatcharr/Dispatcharr) - The IPTV proxy this plugin extends
- Related issues: [#451](https://github.com/Dispatcharr/Dispatcharr/issues/451), [#533](https://github.com/Dispatcharr/Dispatcharr/issues/533)
