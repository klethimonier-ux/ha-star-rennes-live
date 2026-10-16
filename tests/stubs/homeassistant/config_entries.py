
class ConfigEntry:
    def __init__(self, entry_id="e1", data=None, options=None):
        self.entry_id=entry_id; self.data=data or {}; self.options=options or {}
        self.update_listeners=[]; self.on_unload=[]
    def add_update_listener(self, fn):
        self.update_listeners.append(fn); return lambda: None
    def async_on_unload(self, fn): self.on_unload.append(fn)
    def async_create_background_task(self, hass, coro, name):
        import asyncio
        self.bg = getattr(self, "bg", []); t = asyncio.ensure_future(coro); self.bg.append(t); return t
class _Flow:
    hass=None
    def async_show_form(self, step_id, data_schema=None, errors=None):
        return {"type":"form","step_id":step_id,"data_schema":data_schema,"errors":errors or {}}
    def async_create_entry(self, title, data):
        return {"type":"create_entry","title":title,"data":data}
    def async_abort(self, reason): return {"type":"abort","reason":reason}
class ConfigFlow(_Flow):
    def __init_subclass__(cls, domain=None, **kw): super().__init_subclass__(**kw)
    def _async_current_entries(self): return []
class OptionsFlow(_Flow): pass
