#!/usr/bin/env python3
"""Fake chrome-devtools-mcp: prints its arguments — the launcher must replace itself with it."""
import json
import sys

print(json.dumps(sys.argv[1:]))
