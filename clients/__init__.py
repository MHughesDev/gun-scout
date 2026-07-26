"""Import every client module so @register fills the per-vertical registries."""
from .base import REGISTRIES, SiteClient, ClientBlocked, StructureError  # noqa: F401
# --- guns clients ---
from . import gunscom     # noqa: F401
from . import gunbroker   # noqa: F401
from . import gunsamerica  # noqa: F401
from . import sportsmans  # noqa: F401
from . import gundeals    # noqa: F401
from . import cabelas     # noqa: F401
from . import gunmade     # noqa: F401
from . import psa         # noqa: F401
# --- parts clients ---   (REGISTRIES['parts'])
from . import gunmagwarehouse  # noqa: F401
# --- ammo clients ---    (REGISTRIES['ammo'])
from . import ammo_com    # noqa: F401
