"""Shared test fixtures and environment setup.

archon_bot.bot constructs hikari.GatewayBot at import time, which validates the
DISCORD_TOKEN format. Provide a syntactically valid dummy so test modules can
import the package without a real bot token in the environment.
"""

import base64
import os

_dummy_id = base64.b64encode(b"100000000000000000").decode().rstrip("=")
os.environ.setdefault("DISCORD_TOKEN", f"{_dummy_id}.AAAAAA.AAAAAAAAAAAAAAAAAAAAAAAAAAA")
