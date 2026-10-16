from dataclasses import dataclass
@dataclass
class StaticPathConfig:
    url_path: str
    path: str
    cache_headers: bool = True
