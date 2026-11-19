"""The core's modules; an embed's modules/ folder joins them (core/embed.py)."""
from core import embed as _embed

__path__ = _embed.extend(__path__, "modules")
