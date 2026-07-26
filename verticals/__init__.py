"""Vertical descriptors — import each so it registers into VERTICALS.

Import order matters only in that guns must exist for get()'s default. Each
module does `from clients import …` at load, so `clients` must be importable
first (it is — nothing in clients imports verticals at module load; the two
hot-path uses in clients.base.passes and models.Listing are deferred).
"""
from .base import (  # noqa: F401
    Vertical, InputField, Column, VERTICALS, register, get, all_verticals,
    first_int,
)
from . import guns   # noqa: F401
from . import parts  # noqa: F401
from . import ammo   # noqa: F401
