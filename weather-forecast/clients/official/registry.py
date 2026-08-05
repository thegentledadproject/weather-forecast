"""
clients/official/registry.py

PURPOSE
-------
Maps StationConfig.official_client_key -> an OfficialClient instance.
pipeline.py calls get_official_client(station.official_client_key) and
never needs to know which country/adapter it's actually talking to.

To add a new source: implement an OfficialClient subclass (see
nea.py or met_malaysia.py as templates), import it here, and add one
line to _CLIENTS.

DEPENDENCIES
------------
clients/official/nea.py, clients/official/met_malaysia.py,
clients/official/wwis.py, clients/official/hko.py (local)
"""

from clients.official.nea import NEAClient
from clients.official.met_malaysia import METMalaysiaClient
from clients.official.wwis import WWISClient
from clients.official.hko import HKOClient

_CLIENTS = {
    "nea": NEAClient(),
    "met_malaysia": METMalaysiaClient(),
    "wwis": WWISClient(),
    "hko": HKOClient(),
}


def get_official_client(key: str):
    """Look up a registered OfficialClient instance by key. Raises KeyError if unregistered."""
    if key not in _CLIENTS:
        raise KeyError(
            f"No official client registered for key '{key}'. "
            f"Registered keys: {list(_CLIENTS)}. Add one in clients/official/registry.py."
        )
    return _CLIENTS[key]
