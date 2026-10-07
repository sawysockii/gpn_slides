"""GPN corporate presentation restyle pipeline (spec: GPN_PPTX_Restyler v1.2).

Two user workflows share one engine:
  * ``presentation`` — PPTX → corporate restyle (``from-ppt``)
  * ``document`` — PDF/Word/text/prompt → new deck (``from-document``)
"""

from __future__ import annotations

__all__ = ["ENGINE_VERSION"]

ENGINE_VERSION = "0.1.0"
