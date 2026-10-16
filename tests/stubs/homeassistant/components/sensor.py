
from enum import Enum
class SensorDeviceClass(str, Enum):
    TIMESTAMP="timestamp"
class SensorEntity:
    hass=None
    def async_on_remove(self, fn): pass
    def async_write_ha_state(self): pass
