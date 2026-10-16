
class _Reg:
    def async_get_or_create(self, **kw): return kw
def async_get(hass): return _Reg()
