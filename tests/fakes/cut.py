"""Tests of the built-in code the open flavor cuts out of the tree (core/cutout.py, app spec §3.2): on the factory's run of
the whole suite on the cut tree they skip themselves, the rest of the suite runs as everywhere."""
import os
import unittest

from core import cutout, embed

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# the pin of the modules that once carried these functions: the open cut drops it too (core/appcode.OPEN_CUT)
PIN = embed.file("modules/private.json", REPO) is not None   # the core's own or an embed's (core/embed.py)


def needs(*names):
    """Skip a test or a class when the built-in code of any of names (core/cutout.FILES) is not in this tree."""
    return unittest.skipUnless(all(cutout.present(n) for n in names),
                               f"the built-in {', '.join(names)} is cut out of this tree (the open flavor)")


def needs_pin():
    return unittest.skipUnless(PIN, "modules/private.json is cut out of this tree (the open flavor)")
