"""The voice; an embed's modules/voice/ folder joins it (core/embed.py)."""
from core import embed as _embed

__path__ = _embed.extend(__path__, "modules/voice")
