"""
Dispatcharr VOD Fix Plugin

Fixes VOD playback for TiviMate and other Android clients by properly
handling multiple Range requests for the same content.
"""

from .plugin import Plugin

__all__ = ['Plugin']
__version__ = '1.0.0'
