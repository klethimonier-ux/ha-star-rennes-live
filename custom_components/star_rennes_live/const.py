DOMAIN = "star_rennes_live"

CONF_STATIONS = "stations"
CONF_LINE = "line"
CONF_DIRECTION = "direction"
CONF_STOP = "stop"

# Clé d'API STAR (facultative), saisie dans le config flow / les options.
CONF_API_KEY = "api_key"

LINE_KEY = "line_id"
LINE_NAME_KEY = "line_code"
STOP_NAME_KEY = "stop_name"
DESTINATION_KEY = "dest_name"
SENS_KEY = "sens"
ICON_URL_KEY = "icon_url"


def get_entry_config(entry) -> dict:
    """Configuration effective d'une entrée : options si présentes, sinon data."""
    return dict(entry.options or entry.data)
