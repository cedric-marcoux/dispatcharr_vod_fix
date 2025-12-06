"""
Dispatcharr VOD Fix Plugin
==========================

This plugin fixes VOD (Video on Demand) playback issues with TiviMate and other
Android IPTV clients by properly handling multiple simultaneous HTTP Range requests.

Problem Description
-------------------
TiviMate and similar Android clients make multiple simultaneous HTTP Range requests
when playing MKV/VOD files:

    1. Initial request (no Range header) - to probe file size and metadata
    2. Range: bytes=X- (request for metadata at end of file, e.g., MKV seek index)
    3. Range: bytes=Y- (actual playback start position)

Dispatcharr's default behavior counts each HTTP request as a separate provider
connection, incrementing the `profile_connections:{profile_id}` counter in Redis.
With `max_streams=1`, the 2nd and 3rd requests are rejected with
"All profiles at capacity" before the first request even completes.

Solution
--------
This plugin tracks VOD connections by **client IP + content UUID** instead of
per-HTTP-request. Multiple Range requests from the same client for the same
content share a single "connection slot" in Redis.

Architecture
------------
The plugin uses monkey-patching to intercept key functions in Dispatcharr's
VOD proxy system:

    - VODStreamView._get_m3u_profile() - Check for existing client slots
    - MultiWorkerVODConnectionManager._increment_profile_connections() - Skip if already counted
    - MultiWorkerVODConnectionManager._decrement_profile_connections() - Smart cleanup
    - MultiWorkerVODConnectionManager.stream_content_with_session() - Context tracking

Auto-Install on Startup
-----------------------
This module auto-installs hooks when loaded. Dispatcharr's PluginManager imports
this module on startup, triggering the auto-install code at the bottom of this file.

IMPORTANT - uWSGI Multi-Worker Architecture:
    Dispatcharr runs with multiple uWSGI workers (separate processes).
    Each worker has its own memory space, so hooks must be installed
    in EACH worker independently. The auto-install mechanism handles this
    by installing hooks when Django signals that a worker is ready.

Usage
-----
Simply place this plugin in the Dispatcharr plugins directory and restart:

    /data/plugins/dispatcharr_vod_fix/

The plugin will auto-install and start working immediately.

Author: Cedric Marcoux
Version: 1.3.0
License: MIT
"""

import logging

# Configure logger with plugin namespace for easy filtering in logs
# All log messages will be prefixed with "plugins.dispatcharr_vod_fix"
logger = logging.getLogger("plugins.dispatcharr_vod_fix")

# Track if hooks are installed in THIS worker process
# Each uWSGI worker is a separate Python process with its own memory space,
# so this flag is worker-specific and prevents double-installation
_hooks_installed = False


def _auto_install_hooks():
    """
    Install hooks automatically when Django starts up.

    This function is called either:
    1. Immediately if Django apps are already ready (django.apps.apps.ready == True)
    2. On the first HTTP request via Django's request_finished signal

    Hooks are ALWAYS installed regardless of plugin enabled state in the database.
    The hooks themselves check the enabled state at runtime, allowing the plugin
    to be enabled/disabled without requiring a restart.

    Returns:
        None

    Side Effects:
        - Sets global _hooks_installed to True on success
        - Logs success/failure messages
        - Installs monkey-patches on Dispatcharr's VOD proxy classes
    """
    global _hooks_installed

    # Prevent double-installation in this worker
    if _hooks_installed:
        return

    try:
        # Import and execute hook installation
        from .hooks import install_hooks

        if install_hooks():
            _hooks_installed = True
            logger.info("[VOD-Fix] Hooks installed (will check enabled state at runtime)")
        else:
            logger.error("[VOD-Fix] Hook installation returned False")

    except Exception as e:
        logger.error(f"[VOD-Fix] Auto-install error: {e}")
        import traceback
        traceback.print_exc()


class Plugin:
    """
    Main plugin class for Dispatcharr VOD Fix.

    This class follows Dispatcharr's plugin interface specification.
    The PluginManager instantiates this class and calls run() with
    various actions based on user interaction in the UI.

    Attributes:
        name (str): Human-readable plugin name displayed in UI
        version (str): Semantic version string (MAJOR.MINOR.PATCH)
        description (str): Brief description shown in plugin list
        author (str): Plugin author name
        fields (list): Configuration fields (empty - no config needed)
        actions (list): Custom action buttons (empty - no custom actions)

    Example:
        >>> plugin = Plugin()
        >>> plugin.run("enable")
        {'status': 'ok', 'message': 'VOD Fix plugin enabled'}
    """

    def __init__(self):
        """
        Initialize plugin metadata.

        All attributes are used by Dispatcharr's PluginManager to display
        plugin information in the admin UI and handle plugin lifecycle.
        """
        # Display name shown in the Dispatcharr plugins list
        self.name = "Dispatcharr VOD Fix"

        # Semantic version: MAJOR.MINOR.PATCH
        # - MAJOR: Breaking changes
        # - MINOR: New features, backwards compatible
        # - PATCH: Bug fixes
        self.version = "1.3.0"

        # Description shown in plugin details
        self.description = (
            "Fixes VOD playback for TiviMate and series playback for iPlayTV. "
            "Tracks connections by client+content instead of per-request. "
            "Fixes series episode IDs and null values for iPlayTV compatibility."
        )

        # Plugin author
        self.author = "Cedric Marcoux"

        # Configuration fields - empty list means no configuration UI
        # If fields were needed, they would be dicts with:
        # {"id": "field_name", "type": "string|select|boolean", "label": "...", "default": "..."}
        self.fields = []

        # Custom action buttons - empty list means no custom actions
        # If actions were needed, they would be dicts with:
        # {"id": "action_name", "label": "Button Text", "confirm": "Are you sure?"}
        self.actions = []

    def run(self, action=None, params=None, context=None):
        """
        Execute a plugin action.

        This method is the main entry point called by Dispatcharr's PluginManager
        when the user interacts with the plugin (enable, disable, custom actions).

        Args:
            action (str, optional): The action to perform. Standard actions:
                - "enable": User enabled the plugin in UI
                - "disable": User disabled the plugin in UI
                - Custom action IDs defined in self.actions
            params (dict, optional): Parameters passed with the action.
                For custom actions, contains user input from action form.
            context (dict, optional): Execution context including:
                - "user": The Django user who triggered the action
                - "request": The HTTP request object

        Returns:
            dict: Result dictionary with keys:
                - "status": "ok" or "error"
                - "message": Human-readable result message

        Example:
            >>> plugin.run("enable")
            {'status': 'ok', 'message': 'VOD Fix plugin enabled'}

            >>> plugin.run("disable")
            {'status': 'ok', 'message': 'VOD Fix plugin disabled'}

            >>> plugin.run("unknown")
            {'status': 'error', 'message': 'Unknown action: unknown'}
        """
        # Ensure context is always a dict
        context = context or {}

        if action == "enable":
            # User enabled the plugin via UI
            logger.info("[VOD-Fix] Enabling plugin...")

            from .hooks import install_hooks
            if install_hooks():
                return {"status": "ok", "message": "VOD Fix plugin enabled"}
            return {"status": "error", "message": "Failed to install hooks"}

        elif action == "disable":
            # User disabled the plugin via UI
            logger.info("[VOD-Fix] Disabling plugin...")

            from .hooks import uninstall_hooks
            uninstall_hooks()
            return {"status": "ok", "message": "VOD Fix plugin disabled"}

        # Unknown action - return error
        return {"status": "error", "message": f"Unknown action: {action}"}


# =============================================================================
# AUTO-INSTALL ON MODULE IMPORT
# =============================================================================
# This code runs when Python imports this module. Since Dispatcharr's
# PluginManager imports all discovered plugins on startup, this effectively
# runs on application startup.
#
# The try/except ensures that any import errors don't crash Dispatcharr.
# =============================================================================

try:
    import django

    # Check if Django apps registry is ready
    # This is True after Django has finished loading all apps
    if django.apps.apps.ready:
        # Django is ready, install hooks immediately
        _auto_install_hooks()
    else:
        # Django is still starting up, defer hook installation
        # Use Django's request_finished signal to install on first request
        from django.core.signals import request_finished

        def _on_first_request(sender, **kwargs):
            """
            Signal handler to install hooks on first HTTP request.

            This is a one-shot handler that disconnects itself after running.
            It's used when Django isn't fully ready at module import time.

            Args:
                sender: The signal sender (usually the WSGI handler)
                **kwargs: Additional signal arguments (ignored)
            """
            _auto_install_hooks()
            # Disconnect to prevent running on every request
            request_finished.disconnect(_on_first_request)

        # Connect the signal handler
        request_finished.connect(_on_first_request)

except Exception:
    # Silently ignore any errors during auto-install setup
    # The plugin will still work if manually enabled via UI
    pass
