
from typing import Generic, TypeVar
T=TypeVar("T")
class UpdateFailed(Exception): pass
class ConfigEntryNotReady(Exception): pass
class DataUpdateCoordinator(Generic[T]):
    def __init__(self, hass, logger, *, config_entry=None, name=None, update_interval=None, update_method=None):
        self.hass=hass; self.logger=logger; self.config_entry=config_entry; self.name=name
        self.update_interval=update_interval; self.data=None; self.last_update_success=True; self._listeners=[]
    async def async_config_entry_first_refresh(self):
        try:
            self.data = await self._async_update_data()
        except UpdateFailed as err:
            raise ConfigEntryNotReady(str(err)) from err
    async def async_refresh(self):
        try:
            self.data = await self._async_update_data(); self.last_update_success=True
        except UpdateFailed:
            self.last_update_success=False
        for l in list(self._listeners): l()
    def async_add_listener(self, cb):
        self._listeners.append(cb); return lambda: self._listeners.remove(cb)
class CoordinatorEntity(Generic[T]):
    def __init__(self, coordinator): self.coordinator=coordinator
    @property
    def available(self): return self.coordinator.last_update_success
    should_poll=False
