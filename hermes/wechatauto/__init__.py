# Hermes platform plugin — re-export register() so the package-level
# PluginManager probe finds it (bundled plugins use the same pattern).
from .adapter import register

__all__ = ["register"]
