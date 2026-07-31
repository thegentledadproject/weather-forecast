"""
clients/official/

PURPOSE
-------
One adapter class per national meteorological source (NEA for
Singapore, MET Malaysia for Malaysia, etc). Each adapter implements
the same interface (see base.py) so pipeline.py can call whichever
one a station's config points to without caring which country it is.

This is the extension point for adding a new station whose official
source isn't covered yet: subclass OfficialClient, implement what you
can, register it in registry.py, and point the new StationConfig's
official_client_key at it.

DEPENDENCIES
------------
See each adapter module individually.
"""
