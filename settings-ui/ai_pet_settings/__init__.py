"""AI Pet GTK4 / Libadwaita Settings UI package."""

from .api_client import ApiResult, DEFAULT_CONFIG, DEFAULT_SYSTEM_PROMPT, DaemonClient
from .window import PetPreferencesWindow, SettingsApplication

__all__ = [
    "ApiResult",
    "DEFAULT_CONFIG",
    "DEFAULT_SYSTEM_PROMPT",
    "DaemonClient",
    "PetPreferencesWindow",
    "SettingsApplication",
]
