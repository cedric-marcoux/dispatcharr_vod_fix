"""
VOD Fix Hooks - Monkey-patches for VOD Connection Handling
==========================================================

This module contains the core monkey-patching logic that fixes VOD playback
issues with TiviMate and similar Android IPTV clients.

Problem Background
------------------
TiviMate makes multiple simultaneous HTTP Range requests when playing MKV files:

    Request 1: GET /movie/uuid (no Range) - Probe file size
    Request 2: GET /movie/uuid (Range: bytes=X-) - Read metadata at EOF
    Request 3: GET /movie/uuid (Range: bytes=Y-) - Start playback

Each request is handled by a separate uWSGI worker, and Dispatcharr's default
behavior counts each as a separate provider connection. With max_streams=1,
requests 2 and 3 are rejected before request 1 completes.

Solution Architecture
---------------------
This module implements a "connection slot" system using Redis:

    1. SLOT CREATION: When a client requests VOD content, we create a Redis
       hash key `vod_client_slot:{ip}:{content_uuid}` with:
       - profile_id: The M3U profile being used
       - created_at: Timestamp for grace period calculation
       - active_requests: Counter of concurrent requests
       - counted: Flag indicating if we incremented profile_connections

    2. SLOT REUSE: Subsequent requests from the same client for the same
       content check for an existing slot. If found within the grace period
       (10 seconds) or with active requests > 0, the request reuses the slot.

    3. SINGLE COUNT: Only the first request that gets a slot increments the
       profile_connections counter. Subsequent requests skip the increment.

    4. SMART CLEANUP: When a request completes, we decrement active_requests.
       Only when active_requests reaches 0 do we:
       - Delete the slot from Redis
       - Decrement profile_connections (if we counted it)

Patched Functions
-----------------
This module patches the following functions:

    VODStreamView._get_m3u_profile()
        Original: Checks profile capacity and returns available profile
        Patched: First checks for existing client slot, reuses if found

    MultiWorkerVODConnectionManager._increment_profile_connections()
        Original: Always increments profile_connections counter
        Patched: Skips increment if slot already counted

    MultiWorkerVODConnectionManager._decrement_profile_connections()
        Original: Always decrements profile_connections counter
        Patched: Only decrements when all slot requests are done

    MultiWorkerVODConnectionManager.stream_content_with_session()
        Original: Streams content to client
        Patched: Stores client context for increment/decrement patches

    MultiWorkerVODConnectionManager._check_profile_limits()
        Original: Checks if profile has available slots
        Patched: Delegates to original (capacity check done in _get_m3u_profile)

Redis Key Structure
-------------------
Key: vod_client_slot:{client_ip}:{content_uuid}
Type: Hash
TTL: 300 seconds (5 minutes)
Fields:
    - profile_id (str): M3U profile ID, e.g., "3"
    - created_at (str): Unix timestamp, e.g., "1234567890.123"
    - last_activity (str): Unix timestamp of last request
    - active_requests (str): Count of concurrent requests, e.g., "2"
    - counted (str): "0" or "1" - whether profile_connections was incremented

Thread Safety
-------------
All operations use Redis atomic commands (HINCRBY, HSET) for thread safety.
Multiple uWSGI workers can safely operate on the same slot concurrently.

Author: Cedric Marcoux
Version: 1.0.0
License: MIT
"""

import time
import logging
from functools import wraps
from typing import Optional, Dict, Any, Tuple

# Configure logger for this module
# Log messages will appear as "dispatcharr_vod_fix.hooks: [VOD-Fix] ..."
logger = logging.getLogger("dispatcharr_vod_fix.hooks")

# =============================================================================
# GLOBAL STATE - Original function references for uninstall
# =============================================================================
# These variables store references to the original (unpatched) functions.
# They are set during install_hooks() and used by uninstall_hooks() to restore
# the original behavior.

_original_get_m3u_profile = None      # VODStreamView._get_m3u_profile
_original_increment = None             # MultiWorkerVODConnectionManager._increment_profile_connections
_original_decrement = None             # MultiWorkerVODConnectionManager._decrement_profile_connections
_original_stream_content = None        # MultiWorkerVODConnectionManager.stream_content_with_session
_original_xc_series_stream = None      # apps.output.views.xc_series_stream
_original_xc_get_series_info = None    # apps.output.views.xc_get_series_info

# =============================================================================
# CONFIGURATION CONSTANTS
# =============================================================================

# Grace period in seconds for slot reuse
# Requests within this window of the slot creation are allowed to reuse it,
# even if active_requests is 0 (handles race conditions)
GRACE_PERIOD_SECONDS = 10.0

# Redis key prefix for client slot tracking
# Full key format: vod_client_slot:{client_ip}:{content_uuid}
CLIENT_TRACKING_PREFIX = "vod_client_slot:"

# TTL for client slots in Redis (seconds)
# Slots are automatically cleaned up after this time if not explicitly deleted
SLOT_TTL_SECONDS = 300  # 5 minutes


# =============================================================================
# ORPHAN CLEANUP FUNCTIONS
# =============================================================================

def count_active_slots_for_profile(profile_id: int) -> int:
    """
    Count the number of active client slots for a given profile.

    This function scans all VOD client slots in Redis and counts how many
    are associated with the specified profile AND have active_requests > 0.

    This is used to detect "orphan" profile_connections counters - where the
    counter shows connections but no actual slots exist (due to abrupt client
    disconnects, crashes, etc.).

    Args:
        profile_id: The M3U profile ID to count slots for

    Returns:
        int: Number of active slots for this profile

    Example:
        >>> count = count_active_slots_for_profile(3)
        >>> if count == 0:
        ...     print("No active slots - counter may be orphaned")
    """
    redis_client = get_redis_client()
    if not redis_client:
        return -1  # Return -1 to indicate we couldn't check

    try:
        count = 0
        profile_id_str = str(profile_id)

        # Scan for all client slot keys
        # Using SCAN is safer than KEYS for large datasets
        for key in redis_client.scan_iter(f"{CLIENT_TRACKING_PREFIX}*"):
            try:
                slot_data = redis_client.hgetall(key)
                if not slot_data:
                    continue

                # Decode bytes to strings if needed
                slot_profile = slot_data.get(b'profile_id') or slot_data.get('profile_id')
                slot_active = slot_data.get(b'active_requests') or slot_data.get('active_requests')

                if slot_profile:
                    slot_profile_str = (
                        slot_profile.decode('utf-8')
                        if isinstance(slot_profile, bytes)
                        else str(slot_profile)
                    )

                    if slot_profile_str == profile_id_str:
                        # This slot is for our profile, check if it's active
                        active_requests = 0
                        if slot_active:
                            active_str = (
                                slot_active.decode('utf-8')
                                if isinstance(slot_active, bytes)
                                else str(slot_active)
                            )
                            active_requests = int(active_str)

                        if active_requests > 0:
                            count += 1

            except Exception as e:
                logger.debug(f"[VOD-Fix] Error checking slot {key}: {e}")
                continue

        return count

    except Exception as e:
        logger.error(f"[VOD-Fix] Error counting active slots: {e}")
        return -1


def cleanup_orphan_counter(profile_id: int) -> bool:
    """
    Check and reset an orphaned profile_connections counter.

    An "orphan" counter occurs when:
    - profile_connections shows a value > 0
    - But no active client slots exist for that profile

    This happens when clients disconnect abruptly (TCP reset, app crash, etc.)
    and the decrement callback never fires.

    Args:
        profile_id: The M3U profile ID to check/reset

    Returns:
        bool: True if counter was reset, False if counter is valid or couldn't check

    Example:
        >>> if cleanup_orphan_counter(3):
        ...     print("Orphan counter was reset!")
    """
    redis_client = get_redis_client()
    if not redis_client:
        return False

    try:
        profile_connections_key = f"profile_connections:{profile_id}"
        current_count = int(redis_client.get(profile_connections_key) or 0)

        if current_count <= 0:
            # Counter is already at 0, nothing to clean
            return False

        # Count actual active slots for this profile
        active_slots = count_active_slots_for_profile(profile_id)

        if active_slots == -1:
            # Couldn't check, don't modify
            return False

        if active_slots == 0 and current_count > 0:
            # ORPHAN DETECTED: Counter shows connections but no slots exist
            logger.warning(
                f"[VOD-Fix] Orphan counter detected for profile {profile_id}: "
                f"counter={current_count}, active_slots={active_slots}. Resetting to 0."
            )
            redis_client.set(profile_connections_key, "0")
            return True

        return False

    except Exception as e:
        logger.error(f"[VOD-Fix] Error in cleanup_orphan_counter: {e}")
        return False


# =============================================================================
# UTILITY FUNCTIONS
# =============================================================================

def get_redis_client():
    """
    Get the Redis client instance from Dispatcharr's core utilities.

    Dispatcharr maintains a singleton Redis connection that is shared across
    all components. This function retrieves that client for use in slot
    tracking operations.

    Returns:
        redis.Redis: The Redis client instance, or None if unavailable.

    Example:
        >>> client = get_redis_client()
        >>> if client:
        ...     client.set("key", "value")
    """
    try:
        from core.utils import RedisClient
        return RedisClient.get_client()
    except Exception as e:
        logger.error(f"[VOD-Fix] Failed to get Redis client: {e}")
        return None


def get_client_content_key(client_ip: str, content_uuid: str) -> str:
    """
    Generate a Redis key for tracking a client+content combination.

    The key uniquely identifies a specific client (by IP) requesting specific
    content (by UUID). This allows multiple requests from the same client for
    the same content to share a single connection slot.

    Args:
        client_ip: The client's IP address (e.g., "192.168.1.100")
        content_uuid: The UUID of the VOD content (e.g., "abc123-def456-...")

    Returns:
        str: Redis key in format "vod_client_slot:{ip}:{uuid}"

    Example:
        >>> get_client_content_key("192.168.1.100", "abc123")
        'vod_client_slot:192.168.1.100:abc123'
    """
    return f"{CLIENT_TRACKING_PREFIX}{client_ip}:{content_uuid}"


# =============================================================================
# HOOK INSTALLATION / UNINSTALLATION
# =============================================================================

def install_hooks() -> bool:
    """
    Install all monkey-patches for VOD connection handling.

    This function replaces key methods in Dispatcharr's VOD proxy classes
    with patched versions that implement the connection slot logic.

    The patches are applied to:
        - VODStreamView._get_m3u_profile
        - MultiWorkerVODConnectionManager._increment_profile_connections
        - MultiWorkerVODConnectionManager._decrement_profile_connections
        - MultiWorkerVODConnectionManager.stream_content_with_session
        - MultiWorkerVODConnectionManager._check_profile_limits
        - apps.output.views.xc_series_stream (fix MultipleObjectsReturned bug)

    Returns:
        bool: True if all hooks installed successfully, False otherwise.

    Side Effects:
        - Modifies class methods on VODStreamView and MultiWorkerVODConnectionManager
        - Stores original functions in global variables for later restoration
        - Logs installation progress and any errors

    Note:
        This function is idempotent - calling it multiple times is safe.
        However, each call will re-store the "original" functions, so only
        call uninstall_hooks() once after the final install_hooks() call.

    Example:
        >>> if install_hooks():
        ...     print("VOD Fix hooks installed successfully")
        ... else:
        ...     print("Failed to install VOD Fix hooks")
    """
    global _original_increment, _original_decrement, _original_stream_content
    global _original_get_m3u_profile, _original_xc_series_stream, _original_xc_get_series_info

    logger.info("[VOD-Fix] Installing VOD connection hooks...")

    try:
        # Import target classes
        from apps.proxy.vod_proxy.multi_worker_connection_manager import MultiWorkerVODConnectionManager
        from apps.proxy.vod_proxy.views import VODStreamView

        # ---------------------------------------------------------------------
        # Store references to original functions
        # ---------------------------------------------------------------------
        # These are saved so we can restore them in uninstall_hooks()

        _original_increment = MultiWorkerVODConnectionManager._increment_profile_connections
        _original_decrement = MultiWorkerVODConnectionManager._decrement_profile_connections
        _original_stream_content = MultiWorkerVODConnectionManager.stream_content_with_session
        _original_get_m3u_profile = VODStreamView._get_m3u_profile

        # Store _check_profile_limits original on the class itself
        # (needed because we reference it in the patched version)
        original_check = MultiWorkerVODConnectionManager._check_profile_limits
        MultiWorkerVODConnectionManager._original_check_profile_limits = original_check

        # ---------------------------------------------------------------------
        # Apply patches
        # ---------------------------------------------------------------------
        # Replace the original methods with our patched versions

        MultiWorkerVODConnectionManager._increment_profile_connections = patched_increment_profile_connections
        MultiWorkerVODConnectionManager._decrement_profile_connections = patched_decrement_profile_connections
        MultiWorkerVODConnectionManager.stream_content_with_session = patched_stream_content_with_session
        MultiWorkerVODConnectionManager._check_profile_limits = patched_check_profile_limits
        VODStreamView._get_m3u_profile = patched_get_m3u_profile

        # ---------------------------------------------------------------------
        # Patch xc_series_stream to fix MultipleObjectsReturned bug
        # ---------------------------------------------------------------------
        try:
            import apps.output.views as output_views
            _original_xc_series_stream = output_views.xc_series_stream
            output_views.xc_series_stream = patched_xc_series_stream

            # Also patch URL patterns that reference xc_series_stream
            from django.urls import get_resolver
            resolver = get_resolver()
            _patch_url_patterns_for_xc_series_stream(resolver.url_patterns, output_views)

            logger.info("[VOD-Fix] Patched xc_series_stream for MultipleObjectsReturned fix")
        except Exception as e:
            logger.warning(f"[VOD-Fix] Could not patch xc_series_stream: {e}")

        # ---------------------------------------------------------------------
        # Patch xc_get_series_info to fix null values causing iPlayTV crash
        # ---------------------------------------------------------------------
        try:
            import apps.output.views as output_views
            _original_xc_get_series_info = output_views.xc_get_series_info
            output_views.xc_get_series_info = patched_xc_get_series_info
            logger.info("[VOD-Fix] Patched xc_get_series_info for null value fix")
        except Exception as e:
            logger.warning(f"[VOD-Fix] Could not patch xc_get_series_info: {e}")

        logger.info("[VOD-Fix] All hooks installed successfully")
        return True

    except Exception as e:
        logger.exception(f"[VOD-Fix] Failed to install hooks: {e}")
        return False


def _patch_url_patterns_for_xc_series_stream(urlpatterns, output_views):
    """
    Recursively patch URL patterns that reference xc_series_stream.

    Django caches function references at import time, so we need to update
    the pattern.callback directly.
    """
    global _original_xc_series_stream

    for pattern in urlpatterns:
        if hasattr(pattern, 'url_patterns'):
            # This is a URLResolver, recurse into it
            _patch_url_patterns_for_xc_series_stream(pattern.url_patterns, output_views)
        elif hasattr(pattern, 'callback'):
            # This is a URLPattern
            if pattern.callback == _original_xc_series_stream:
                pattern.callback = patched_xc_series_stream
                logger.debug(f"[VOD-Fix] Patched URL pattern: {pattern.name}")


def uninstall_hooks() -> bool:
    """
    Restore original functions, removing all monkey-patches.

    This function reverses the changes made by install_hooks(), restoring
    Dispatcharr's original VOD proxy behavior.

    Returns:
        bool: True if all hooks uninstalled successfully, False otherwise.

    Side Effects:
        - Restores original class methods on VODStreamView and MultiWorkerVODConnectionManager
        - Logs uninstallation progress and any errors

    Note:
        Only call this after install_hooks() has been called. Calling it
        without prior installation may cause errors.

    Example:
        >>> uninstall_hooks()
        True
    """
    global _original_get_m3u_profile, _original_increment
    global _original_decrement, _original_stream_content, _original_xc_series_stream

    try:
        from apps.proxy.vod_proxy.views import VODStreamView
        from apps.proxy.vod_proxy.multi_worker_connection_manager import MultiWorkerVODConnectionManager

        # Restore original functions if we have them
        if _original_get_m3u_profile:
            VODStreamView._get_m3u_profile = _original_get_m3u_profile

        if _original_increment:
            MultiWorkerVODConnectionManager._increment_profile_connections = _original_increment

        if _original_decrement:
            MultiWorkerVODConnectionManager._decrement_profile_connections = _original_decrement

        if _original_stream_content:
            MultiWorkerVODConnectionManager.stream_content_with_session = _original_stream_content

        if hasattr(MultiWorkerVODConnectionManager, '_original_check_profile_limits'):
            MultiWorkerVODConnectionManager._check_profile_limits = (
                MultiWorkerVODConnectionManager._original_check_profile_limits
            )

        # Restore xc_series_stream
        if _original_xc_series_stream:
            import apps.output.views as output_views
            output_views.xc_series_stream = _original_xc_series_stream

        logger.info("[VOD-Fix] Hooks uninstalled successfully")
        return True

    except Exception as e:
        logger.error(f"[VOD-Fix] Failed to uninstall hooks: {e}")
        return False


# =============================================================================
# SLOT MANAGEMENT FUNCTIONS
# =============================================================================

def get_client_slot(client_ip: str, content_uuid: str) -> Optional[Dict[str, str]]:
    """
    Retrieve an existing client slot from Redis.

    Checks if a slot exists for the given client+content combination and
    returns its data if found.

    Args:
        client_ip: The client's IP address
        content_uuid: The UUID of the VOD content

    Returns:
        dict: Slot data with keys (profile_id, created_at, active_requests, counted)
              or None if no slot exists.

    Example:
        >>> slot = get_client_slot("192.168.1.100", "abc123")
        >>> if slot:
        ...     print(f"Using profile {slot['profile_id']}")
        ... else:
        ...     print("No existing slot")
    """
    redis_client = get_redis_client()
    if not redis_client:
        return None

    client_key = get_client_content_key(client_ip, content_uuid)

    try:
        # Retrieve all fields from the hash
        slot_data = redis_client.hgetall(client_key)
        if not slot_data:
            return None

        # Decode Redis bytes to strings
        # Redis may return bytes or strings depending on connection settings
        decoded_data = {}
        for k, v in slot_data.items():
            k_str = k.decode('utf-8') if isinstance(k, bytes) else k
            v_str = v.decode('utf-8') if isinstance(v, bytes) else v
            decoded_data[k_str] = v_str

        return decoded_data

    except Exception as e:
        logger.error(f"[VOD-Fix] Error getting client slot: {e}")
        return None


def create_or_update_slot(
    client_ip: str,
    content_uuid: str,
    profile_id: int,
    increment_active: bool = True
) -> bool:
    """
    Create a new client slot or update an existing one in Redis.

    If the slot doesn't exist, creates it with initial values.
    If it exists, optionally increments active_requests and updates last_activity.

    Args:
        client_ip: The client's IP address
        content_uuid: The UUID of the VOD content
        profile_id: The M3U profile ID to associate with this slot
        increment_active: If True, increment active_requests counter.
                         Set to False when just updating metadata.

    Returns:
        bool: True if operation succeeded, False otherwise.

    Redis Operations:
        - EXISTS to check if slot exists
        - HSET to create/update fields
        - HINCRBY to atomically increment active_requests
        - EXPIRE to set/refresh TTL

    Example:
        >>> create_or_update_slot("192.168.1.100", "abc123", profile_id=3)
        True
    """
    redis_client = get_redis_client()
    if not redis_client:
        return False

    client_key = get_client_content_key(client_ip, content_uuid)

    try:
        existing = redis_client.exists(client_key)

        if existing:
            # Update existing slot
            if increment_active:
                redis_client.hincrby(client_key, 'active_requests', 1)
            redis_client.hset(client_key, 'last_activity', str(time.time()))
            redis_client.expire(client_key, SLOT_TTL_SECONDS)
            logger.info(f"[VOD-Fix] Updated slot for {client_ip}/{content_uuid}")
        else:
            # Create new slot with initial values
            redis_client.hset(client_key, mapping={
                'profile_id': str(profile_id),
                'created_at': str(time.time()),
                'last_activity': str(time.time()),
                'active_requests': '1',
                'counted': '0',  # Will be set to '1' when profile_connections is incremented
            })
            redis_client.expire(client_key, SLOT_TTL_SECONDS)
            logger.info(f"[VOD-Fix] Created slot for {client_ip}/{content_uuid}, profile {profile_id}")

        return True

    except Exception as e:
        logger.error(f"[VOD-Fix] Error creating/updating slot: {e}")
        return False


# =============================================================================
# PATCHED FUNCTIONS
# =============================================================================

def patched_get_m3u_profile(self, m3u_account, profile_id, session_id=None):
    """
    Patched version of VODStreamView._get_m3u_profile().

    This is the main entry point for the VOD Fix logic. It intercepts profile
    selection and checks for existing client slots before falling back to
    the original capacity-checking logic.

    Flow:
        1. Extract client_ip and content_uuid from the request
        2. Check Redis for existing slot for this client+content
        3. If slot exists and is valid (within grace period or has active requests):
           - Increment slot's active_requests
           - Return the profile from the slot (bypassing capacity check)
        4. If no valid slot exists:
           - Call original _get_m3u_profile() for capacity check
           - If successful, create a new slot for this client+content
           - Return the result

    Args:
        self: VODStreamView instance
        m3u_account: The M3U account to get a profile for
        profile_id: Optional specific profile ID requested
        session_id: Optional session ID for existing connections

    Returns:
        tuple: (M3UAccountProfile, current_connections) or None if no profile available

    Note:
        This function is called from VODStreamView.get() with `self` being
        the view instance. The request object is available as self.request.
    """
    global _original_get_m3u_profile

    # Try to get request context from the view
    # Django's View.dispatch() sets self.request before calling get()
    request = getattr(self, 'request', None)

    if not request:
        # No request context available, fall back to original behavior
        logger.debug("[VOD-Fix] No request context in _get_m3u_profile, using original")
        return _original_get_m3u_profile(self, m3u_account, profile_id, session_id)

    try:
        # ---------------------------------------------------------------------
        # Extract client information from request
        # ---------------------------------------------------------------------

        # Get client IP using Dispatcharr's utility function
        # This handles X-Forwarded-For and other proxy headers
        from apps.proxy.vod_proxy.utils import get_client_info
        client_ip, _ = get_client_info(request)

        # Extract content UUID from the request path
        # Expected path formats:
        #   /proxy/vod/movie/{uuid}
        #   /proxy/vod/movie/{uuid}/{session_id}
        #   /proxy/vod/episode/{uuid}
        path = request.path
        parts = path.strip('/').split('/')

        content_uuid = None
        for i, part in enumerate(parts):
            if part in ('movie', 'episode', 'series') and i + 1 < len(parts):
                content_uuid = parts[i + 1]
                break

        # Validate extracted values
        if not client_ip or not content_uuid:
            logger.debug(
                f"[VOD-Fix] Could not extract client_ip={client_ip} "
                f"or content_uuid={content_uuid}"
            )
            return _original_get_m3u_profile(self, m3u_account, profile_id, session_id)

        # ---------------------------------------------------------------------
        # Check for existing slot
        # ---------------------------------------------------------------------

        slot = get_client_slot(client_ip, content_uuid)

        if slot:
            slot_profile_id = slot.get('profile_id')
            created_at = float(slot.get('created_at', 0))
            active_requests = int(slot.get('active_requests', 0))
            time_since_creation = time.time() - created_at

            # Determine if slot is still valid for reuse
            # Valid if: within grace period OR has active requests
            slot_is_valid = (
                time_since_creation < GRACE_PERIOD_SECONDS or
                active_requests > 0
            )

            if slot_profile_id and slot_is_valid:
                try:
                    # Load the profile from database
                    from apps.m3u.models import M3UAccountProfile
                    existing_profile = M3UAccountProfile.objects.get(
                        id=int(slot_profile_id),
                        m3u_account=m3u_account,
                        is_active=True
                    )

                    # Increment active requests counter in Redis
                    redis_client = get_redis_client()
                    if redis_client:
                        client_key = get_client_content_key(client_ip, content_uuid)
                        redis_client.hincrby(client_key, 'active_requests', 1)
                        redis_client.expire(client_key, SLOT_TTL_SECONDS)

                    # Get current connection count for logging (only in DEBUG)
                    profile_connections_key = f"profile_connections:{existing_profile.id}"
                    current_connections = (
                        int(redis_client.get(profile_connections_key) or 0)
                        if redis_client else 0
                    )

                    # High frequency log - slot reuse happens multiple times per VOD request
                    logger.debug(
                        f"[VOD-Fix] Client {client_ip} reusing slot for "
                        f"{content_uuid[:8]}... - profile {slot_profile_id}, "
                        f"active: {active_requests + 1}"
                    )

                    # Return profile WITHOUT triggering capacity check
                    return (existing_profile, current_connections)

                except Exception as e:
                    logger.warning(f"[VOD-Fix] Error reusing slot: {e}")
                    # Fall through to original logic

        # ---------------------------------------------------------------------
        # No valid slot - use original logic
        # ---------------------------------------------------------------------

        result = _original_get_m3u_profile(self, m3u_account, profile_id, session_id)

        if result and result[0]:
            profile = result[0]
            # Create new slot for this client+content
            create_or_update_slot(client_ip, content_uuid, profile.id, increment_active=True)

        elif result is None:
            # ---------------------------------------------------------------------
            # Profile returned None - possibly "at capacity"
            # Check for orphan counters and retry if found
            # ---------------------------------------------------------------------
            # Get all profiles for this account and check each for orphans
            try:
                from apps.m3u.models import M3UAccountProfile
                profiles = M3UAccountProfile.objects.filter(
                    m3u_account=m3u_account,
                    is_active=True
                )

                orphan_cleaned = False
                for profile in profiles:
                    if cleanup_orphan_counter(profile.id):
                        orphan_cleaned = True

                if orphan_cleaned:
                    # Orphan counter was reset - retry profile selection
                    logger.info("[VOD-Fix] Orphan counter cleaned, retrying profile selection")
                    result = _original_get_m3u_profile(self, m3u_account, profile_id, session_id)

                    if result and result[0]:
                        profile = result[0]
                        # Create new slot for this client+content
                        create_or_update_slot(client_ip, content_uuid, profile.id, increment_active=True)

            except Exception as e:
                logger.error(f"[VOD-Fix] Error checking for orphan counters: {e}")

        return result

    except Exception as e:
        logger.exception(f"[VOD-Fix] Error in patched_get_m3u_profile: {e}")
        return _original_get_m3u_profile(self, m3u_account, profile_id, session_id)


def patched_check_profile_limits(self, m3u_profile) -> bool:
    """
    Patched version of MultiWorkerVODConnectionManager._check_profile_limits().

    This patch delegates to the original implementation. The main capacity
    checking is handled in patched_get_m3u_profile() where we have access
    to the client slot information.

    Args:
        self: MultiWorkerVODConnectionManager instance
        m3u_profile: The M3U profile to check

    Returns:
        bool: True if profile has available capacity, False otherwise
    """
    # Delegate to original implementation
    original = getattr(self.__class__, '_original_check_profile_limits', None)
    if original:
        return original(self, m3u_profile)
    return True


def patched_stream_content_with_session(
    self,
    session_id,
    content_obj,
    stream_url,
    m3u_profile,
    client_ip,
    client_user_agent,
    request,
    utc_start=None,
    utc_end=None,
    offset=None,
    range_header=None
):
    """
    Patched version of MultiWorkerVODConnectionManager.stream_content_with_session().

    This patch stores the client context (IP, content UUID) on the connection
    manager instance so that the increment/decrement patches can access it.

    The context is stored as instance attributes:
        - self._current_client_ip
        - self._current_content_uuid
        - self._current_session_id

    These are cleaned up in the finally block to prevent memory leaks.

    Args:
        self: MultiWorkerVODConnectionManager instance
        session_id: The session ID for this stream
        content_obj: The Movie or Episode object being streamed
        stream_url: The provider URL to stream from
        m3u_profile: The M3U profile being used
        client_ip: The client's IP address
        client_user_agent: The client's User-Agent string
        request: The Django request object
        utc_start: Optional timeshift start time
        utc_end: Optional timeshift end time
        offset: Optional seek offset
        range_header: Optional Range header value

    Returns:
        StreamingHttpResponse: The streaming response from the original function
    """
    global _original_stream_content

    # Extract content UUID from the content object
    content_uuid = (
        str(content_obj.uuid) if hasattr(content_obj, 'uuid')
        else str(content_obj.id)
    )

    # Store context on manager instance for increment/decrement patches
    self._current_client_ip = client_ip
    self._current_content_uuid = content_uuid
    self._current_session_id = session_id

    logger.debug(f"[VOD-Fix] stream_content_with_session: {client_ip}/{content_uuid[:8]}...")

    try:
        # Call original function
        return _original_stream_content(
            self, session_id, content_obj, stream_url, m3u_profile,
            client_ip, client_user_agent, request,
            utc_start, utc_end, offset, range_header
        )
    finally:
        # Clean up context to prevent memory leaks
        self._current_client_ip = None
        self._current_content_uuid = None
        self._current_session_id = None


def patched_increment_profile_connections(self, m3u_profile):
    """
    Patched version of MultiWorkerVODConnectionManager._increment_profile_connections().

    This patch ensures that profile_connections is only incremented ONCE per
    client slot, regardless of how many concurrent requests are made.

    Flow:
        1. Get client context from manager instance (set by stream_content_with_session)
        2. If no context, fall back to original behavior
        3. Check slot's 'counted' flag in Redis
        4. If already counted ('1'), skip increment
        5. If not counted ('0'), set flag to '1' and increment

    Args:
        self: MultiWorkerVODConnectionManager instance
        m3u_profile: The M3U profile to increment connection count for

    Returns:
        int: The new connection count after increment (or current count if skipped)
    """
    global _original_increment

    # Get client context stored by stream_content_with_session
    client_ip = getattr(self, '_current_client_ip', None)
    content_uuid = getattr(self, '_current_content_uuid', None)

    if not client_ip or not content_uuid:
        # No context available, use original behavior
        logger.debug("[VOD-Fix] No client context in increment, using original")
        return _original_increment(self, m3u_profile)

    redis_client = get_redis_client()
    if not redis_client:
        return _original_increment(self, m3u_profile)

    client_key = get_client_content_key(client_ip, content_uuid)

    try:
        # Check if this slot has already been counted
        counted = redis_client.hget(client_key, 'counted')

        if counted:
            counted_str = counted.decode('utf-8') if isinstance(counted, bytes) else str(counted)

            if counted_str == '1':
                # Already counted - skip increment
                profile_connections_key = f"profile_connections:{m3u_profile.id}"
                current_count = int(redis_client.get(profile_connections_key) or 0)

                # High frequency log - only in DEBUG mode
                logger.debug(
                    f"[VOD-Fix] Skipping increment for {client_ip}/{content_uuid[:8]}... "
                    f"- already counted, current: {current_count}"
                )
                return current_count

        # Not yet counted - mark as counted and increment
        redis_client.hset(client_key, 'counted', '1')
        redis_client.expire(client_key, SLOT_TTL_SECONDS)

        result = _original_increment(self, m3u_profile)
        logger.info(
            f"[VOD-Fix] Incremented profile {m3u_profile.id} for slot "
            f"{client_ip}/{content_uuid[:8]}..."
        )
        return result

    except Exception as e:
        logger.error(f"[VOD-Fix] Error in patched_increment: {e}")
        return _original_increment(self, m3u_profile)


def patched_decrement_profile_connections(self, m3u_profile_id: int):
    """
    Patched version of MultiWorkerVODConnectionManager._decrement_profile_connections().

    This patch ensures that profile_connections is only decremented when ALL
    concurrent requests for a client slot have completed.

    Flow:
        1. Get client context from manager instance
        2. If no context, fall back to original behavior
        3. Decrement slot's active_requests counter in Redis
        4. If active_requests <= 0:
           - Check if slot was counted
           - Delete slot from Redis
           - If counted, decrement profile_connections
        5. If active_requests > 0:
           - Keep slot alive, don't decrement profile_connections

    Args:
        self: MultiWorkerVODConnectionManager instance
        m3u_profile_id: The M3U profile ID to decrement connection count for

    Returns:
        int: The new connection count after decrement (or current count if skipped)
    """
    global _original_decrement

    # Get client context stored by stream_content_with_session
    client_ip = getattr(self, '_current_client_ip', None)
    content_uuid = getattr(self, '_current_content_uuid', None)

    if not client_ip or not content_uuid:
        # No context available, use original behavior
        logger.debug("[VOD-Fix] No client context in decrement, using original")
        return _original_decrement(self, m3u_profile_id)

    redis_client = get_redis_client()
    if not redis_client:
        return _original_decrement(self, m3u_profile_id)

    client_key = get_client_content_key(client_ip, content_uuid)

    try:
        # Atomically decrement active requests counter
        new_active = redis_client.hincrby(client_key, 'active_requests', -1)

        if new_active <= 0:
            # All requests for this slot have completed
            # Check if we counted this slot for profile_connections
            counted = redis_client.hget(client_key, 'counted')
            counted_str = (
                counted.decode('utf-8') if isinstance(counted, bytes)
                else str(counted) if counted else '0'
            )

            # Clean up the slot from Redis
            redis_client.delete(client_key)

            if counted_str == '1':
                # We incremented profile_connections, so we must decrement it
                result = _original_decrement(self, m3u_profile_id)
                logger.info(
                    f"[VOD-Fix] All requests done for {client_ip}/{content_uuid[:8]}..., "
                    f"decremented profile {m3u_profile_id}"
                )
                return result
            else:
                # Slot was never counted (e.g., request failed before increment)
                logger.info(
                    f"[VOD-Fix] Slot {client_ip}/{content_uuid[:8]}... "
                    f"cleaned up (was not counted)"
                )
                return 0
        else:
            # Still have active requests - don't decrement yet
            # High frequency log - only in DEBUG mode
            logger.debug(
                f"[VOD-Fix] Slot {client_ip}/{content_uuid[:8]}... "
                f"still has {new_active} active requests"
            )

            # Return current count without decrementing
            profile_connections_key = f"profile_connections:{m3u_profile_id}"
            return int(redis_client.get(profile_connections_key) or 0)

    except Exception as e:
        logger.error(f"[VOD-Fix] Error in patched_decrement: {e}")
        return _original_decrement(self, m3u_profile_id)


def patched_xc_series_stream(request, username, password, stream_id, extension):
    """
    Patched version of xc_series_stream that fixes MultipleObjectsReturned bug.

    The original function uses .get() which fails when an episode exists in
    multiple M3U accounts. This patched version uses .first() instead and
    orders by M3U account priority to get the best source.

    Bug fixed:
        apps.vod.models.M3UEpisodeRelation.MultipleObjectsReturned:
        get() returned more than one M3UEpisodeRelation -- it returned 2!

    Args:
        request: Django HttpRequest object
        username: XC API username
        password: XC API password (xc_password from user's custom_properties)
        stream_id: Provider's episode/stream ID
        extension: File extension (e.g., 'mkv', 'mp4')

    Returns:
        HttpResponseRedirect to VOD proxy endpoint, or JsonResponse with error
    """
    from django.shortcuts import get_object_or_404
    from django.http import JsonResponse, HttpResponseRedirect
    from django.urls import reverse
    from django.contrib.auth import get_user_model
    from apps.vod.models import M3UEpisodeRelation

    User = get_user_model()
    user = get_object_or_404(User, username=username)

    custom_properties = user.custom_properties or {}

    if "xc_password" not in custom_properties:
        logger.warning(f"[VOD-Fix] xc_series_stream: User {username} has no xc_password")
        return JsonResponse({"error": "Invalid credentials"}, status=401)

    if custom_properties["xc_password"] != password:
        logger.warning(f"[VOD-Fix] xc_series_stream: Invalid password for user {username}")
        return JsonResponse({"error": "Invalid credentials"}, status=401)

    # FIX: Look up by stream_id (provider's ID) instead of episode_id (internal ID)
    # Also use .first() instead of .get() to handle multiple relations
    # Order by M3U account priority (descending) to get the best source

    try:
        # First try to find by stream_id (provider's ID)
        episode_relation = (
            M3UEpisodeRelation.objects
            .select_related('episode', 'm3u_account')
            .filter(stream_id=stream_id, m3u_account__is_active=True)
            .order_by('-m3u_account__priority', 'id')
            .first()
        )

        # Fallback: try by internal episode_id
        if not episode_relation:
            episode_relation = (
                M3UEpisodeRelation.objects
                .select_related('episode', 'm3u_account')
                .filter(episode_id=stream_id, m3u_account__is_active=True)
                .order_by('-m3u_account__priority', 'id')
                .first()
            )

        if not episode_relation:
            logger.warning(
                f"[VOD-Fix] xc_series_stream: Episode not found for stream_id={stream_id}, "
                f"user={username}. Checked: stream_id lookup, episode_id fallback."
            )
            return JsonResponse({"error": "Episode not found"}, status=404)

        # Log which account was selected when there are multiple
        relation_count = M3UEpisodeRelation.objects.filter(
            stream_id=stream_id, m3u_account__is_active=True
        ).count()
        if relation_count > 1:
            logger.info(
                f"[VOD-Fix] xc_series_stream: Found {relation_count} relations for "
                f"stream_id={stream_id}, using account '{episode_relation.m3u_account.name}' "
                f"(priority: {episode_relation.m3u_account.priority})"
            )

        # Redirect to the VOD proxy endpoint
        vod_url = reverse('proxy:vod_proxy:vod_stream', kwargs={
            'content_type': 'episode',
            'content_id': episode_relation.episode.uuid
        })

        logger.debug(
            f"[VOD-Fix] xc_series_stream: Redirecting stream_id={stream_id} "
            f"to {vod_url}"
        )

        return HttpResponseRedirect(vod_url)

    except Exception as e:
        logger.exception(f"[VOD-Fix] xc_series_stream error: {e}")
        return JsonResponse({"error": "Internal server error"}, status=500)


def patched_xc_get_series_info(request, user, series_id):
    """
    Patched version of xc_get_series_info that:
    1. Converts null values to empty strings (fixes iPlayTV crash)
    2. Replaces internal episode IDs with provider stream_ids (fixes playback)

    Args:
        request: Django HttpRequest object
        user: Django User object
        series_id: Series ID to get info for

    Returns:
        dict with sanitized data
    """
    from apps.vod.models import M3UEpisodeRelation

    # DEBUG: Log the request
    user_agent = request.META.get('HTTP_USER_AGENT', 'unknown')
    logger.info(f"[VOD-Fix] get_series_info called: series_id={series_id}, user={user.username}, UA={user_agent[:30]}")

    # Call the original function (returns a dict, not a Response)
    data = _original_xc_get_series_info(request, user, series_id)

    # DEBUG: Log response structure
    episodes_dict = data.get('episodes', {})
    total_eps = sum(len(v) for v in episodes_dict.values()) if isinstance(episodes_dict, dict) else 0
    seasons_count = len(data.get('seasons', []))
    logger.info(f"[VOD-Fix] get_series_info response: {seasons_count} seasons, {total_eps} episodes")

    try:
        nulls_fixed = 0
        ids_fixed = 0

        # Build a mapping of internal episode ID -> provider stream_id
        episode_ids = []
        if 'episodes' in data and isinstance(data['episodes'], dict):
            for season, episodes in data['episodes'].items():
                if isinstance(episodes, list):
                    for episode in episodes:
                        if isinstance(episode, dict) and 'id' in episode:
                            episode_ids.append(episode['id'])

        # Query for stream_ids
        id_to_stream_id = {}
        if episode_ids:
            relations = M3UEpisodeRelation.objects.filter(
                episode_id__in=episode_ids,
                m3u_account__is_active=True
            ).values('episode_id', 'stream_id')
            for rel in relations:
                # Use first found stream_id for each episode
                if rel['episode_id'] not in id_to_stream_id:
                    id_to_stream_id[rel['episode_id']] = rel['stream_id']

        # Sanitize and fix IDs
        if 'episodes' in data and isinstance(data['episodes'], dict):
            for season, episodes in data['episodes'].items():
                if isinstance(episodes, list):
                    for episode in episodes:
                        if isinstance(episode, dict):
                            # Fix ID: replace internal ID with provider stream_id
                            internal_id = episode.get('id')
                            if internal_id and internal_id in id_to_stream_id:
                                provider_id = id_to_stream_id[internal_id]
                                episode['id'] = provider_id
                                ids_fixed += 1
                                # Also fix nested info.id
                                if 'info' in episode and isinstance(episode['info'], dict):
                                    episode['info']['id'] = provider_id

                            # Convert null values to empty strings for string fields
                            for key in ['custom_sid', 'direct_source', 'title', 'container_extension']:
                                if key in episode and episode[key] is None:
                                    episode[key] = ""
                                    nulls_fixed += 1

                            # Also sanitize nested 'info' dict
                            if 'info' in episode and isinstance(episode['info'], dict):
                                for key, value in episode['info'].items():
                                    if value is None:
                                        episode['info'][key] = ""
                                        nulls_fixed += 1

        logger.info(f"[VOD-Fix] get_series_info: fixed {ids_fixed} IDs, sanitized {nulls_fixed} nulls")
        return data

    except Exception as e:
        logger.exception(f"[VOD-Fix] Could not process series info response: {e}")
        return data
