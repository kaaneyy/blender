"""Asset builders. Importing this package registers every builder."""
from . import street_light  # noqa: F401

# Future builders (T3.2) register themselves the same way:
# from . import pedestrian_lamp, bench, bollard, traffic_sign, ...
