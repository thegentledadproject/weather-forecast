"""
clients/

PURPOSE
-------
One module per external data source (Tier 1 and Tier 2 from the
framework doc). Each client is responsible ONLY for fetching and
lightly parsing its source into a PointForecast/ObservedReading -- no
calibration or probability logic lives here. This isolation means a
source going down or changing its API only breaks one file.

DEPENDENCIES
------------
See each client module individually.
"""
