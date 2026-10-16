
class _Marker:
    def __init__(self, schema, default=None, description=None):
        self.schema=schema; self.default=default; self.description=description
    def __hash__(self): return hash(self.schema)
    def __eq__(self,o): return getattr(o,"schema",o)==self.schema
    def __repr__(self): return f"{type(self).__name__}({self.schema!r})"
class Optional(_Marker): pass
class Required(_Marker): pass
class Schema:
    def __init__(self, schema): self.schema=schema
