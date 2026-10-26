import logging

import voluptuous as vol
import homeassistant.helpers.config_validation as cv

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.helpers.selector import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    CONF_API_KEY,
    DOMAIN,
    LINE_KEY,
    LINE_NAME_KEY,
    DESTINATION_KEY,
    ICON_URL_KEY,
    SENS_KEY,
    STOP_NAME_KEY,
)

from .api.client import StarClient, async_validate_api_key
from .api.metro_api import StarMetroAPI
from .api.bike_api import StarBikeAPI
from .api.parking_api import StarParkingAPI
from .api.bus_api import StarBusAPI

_LOGGER = logging.getLogger(__name__)

SERVICE_TYPES = ("metro", "bus", "bike", "parking")


class StarRennesConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self):
        self.selected_types = {}
        self._api_key = ""
        self._bus_user_inputs = {}
        self._bus_entries = []

        # maps internes
        self._bus_line_map = {}        # line_code → idligne
        self._bus_stop_map = {}        # stop_name → stop_id
        self._bus_dest_map = {}        # destination → sens

    # -------------------------------------------------------------------------
    # PAGE 1 : TYPES
    # -------------------------------------------------------------------------
    async def async_step_user(self, user_input=None):
        # Une seule entrée (« single_config_entry » dans manifest.json) : Home
        # Assistant refuse lui-même une deuxième ; on modifie via « Configurer ».
        errors = {}

        if user_input is not None:
            api_key = (user_input.get(CONF_API_KEY) or "").strip()
            if api_key:
                error = await async_validate_api_key(
                    async_get_clientsession(self.hass), api_key
                )
                if error:
                    errors[CONF_API_KEY] = error

            if not errors:
                self._api_key = api_key
                self.selected_types = {
                    key: bool(user_input.get(key)) for key in SERVICE_TYPES
                }

                if self.selected_types.get("bus"):
                    return await self.async_step_bus_line()

                return await self.async_step_select_equipment()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Optional("metro", default=False): bool,
                vol.Optional("bus", default=False): bool,
                vol.Optional("bike", default=False): bool,
                vol.Optional("parking", default=False): bool,
                # Clé d'API STAR facultative (quota plus élevé)
                vol.Optional(CONF_API_KEY): selector({"text": {"type": "password"}}),
            }),
            errors=errors,
        )

    def _client(self) -> StarClient:
        return StarClient(async_get_clientsession(self.hass), self._api_key or None)

    # -------------------------------------------------------------------------
    # BUS — ÉTAPE 1 : LIGNE
    # -------------------------------------------------------------------------
    async def async_step_bus_line(self, user_input=None):
        if user_input is None:
            line_options = await self._bus_get_line_list()

            schema = vol.Schema({
                vol.Required("bus_line"): selector({
                    "select": {
                        "options": line_options,
                        "mode": "dropdown",
                    }
                })
            })
            return self.async_show_form(step_id="bus_line", data_schema=schema)

        self._bus_user_inputs["bus_line"] = user_input["bus_line"]
        return await self.async_step_bus_stop()

    # -------------------------------------------------------------------------
    # BUS — ÉTAPE 2 : ARRÊT
    # -------------------------------------------------------------------------
    async def async_step_bus_stop(self, user_input=None):
        if user_input is None:
            line_code = self._bus_user_inputs["bus_line"]
            stop_list = await self._bus_get_stop_list(line_code)

            schema = vol.Schema({
                vol.Required("bus_stop"): selector({
                    "select": {
                        "options": stop_list,
                        "mode": "dropdown",
                    }
                })
            })
            return self.async_show_form(step_id="bus_stop", data_schema=schema)

        self._bus_user_inputs["bus_stop"] = user_input["bus_stop"]
        return await self.async_step_bus_destination()

    # -------------------------------------------------------------------------
    # BUS — ÉTAPE 3 : DESTINATION
    # -------------------------------------------------------------------------
    async def async_step_bus_destination(self, user_input=None):
        if user_input is None:
            line_code = self._bus_user_inputs["bus_line"]

            dest_list = await self._bus_get_destination_list(line_code)

            schema = vol.Schema({
                vol.Required("bus_destination"): selector({
                    "select": {
                        "options": dest_list,
                        "mode": "dropdown",
                    }
                })
            })
            return self.async_show_form(step_id="bus_destination", data_schema=schema)

        self._bus_user_inputs["bus_destination"] = user_input["bus_destination"]

        # Construction du bus_entry
        line_code = self._bus_user_inputs["bus_line"]      # nomcourt
        line_id = self._bus_line_map[line_code]            # idligne
        stop_name = self._bus_user_inputs["bus_stop"]
        dest_name = self._bus_user_inputs["bus_destination"]
        sens = self._bus_dest_map.get(dest_name, 0)
        icon_url = await self._bus_get_icon_url(line_code)

        bus_entry = {
            LINE_KEY: line_id,          # identifiant technique
            LINE_NAME_KEY: line_code,   # nomcourt
            STOP_NAME_KEY: stop_name,
            DESTINATION_KEY: dest_name,
            SENS_KEY: sens,
            ICON_URL_KEY: icon_url,
        }


        self._bus_entries = [bus_entry]

        return await self.async_step_select_equipment()

    # -------------------------------------------------------------------------
    # PAGE 2 : AUTRES ÉQUIPEMENTS
    # -------------------------------------------------------------------------
    async def async_step_select_equipment(self, user_input=None):
        client = self._client()

        schema = {}

        # VELO
        if self.selected_types.get("bike"):
            raw = await StarBikeAPI(client).async_get_stations()
            stations = {sid: st["name"] for sid, st in raw.items()}
            schema[vol.Optional("bike_stations", default=[])]=cv.multi_select(stations)

        # PARKING
        if self.selected_types.get("parking"):
            raw = await StarParkingAPI(client).async_get_all()
            parking_list = {pid: p["name"] for pid, p in raw.items()}
            schema[vol.Optional("parking_ids", default=[])]=cv.multi_select(parking_list)

        # METRO
        if self.selected_types.get("metro"):
            metro_lines = {"A": "Ligne A", "B": "Ligne B"}
            schema[vol.Optional("metro_lines", default=[])]=cv.multi_select(metro_lines)

            raw = await StarMetroAPI(client).async_get_stations_topology()
            stations = {str(sid): st["name"] for sid, st in raw.items()}
            schema[vol.Optional("metro_stations", default=[])]=cv.multi_select(stations)

            # Schéma des lignes avec la position des rames (GTFS ~8 Mo / jour)
            schema[vol.Optional("metro_rames", default=False)] = bool

        if user_input is not None:
            data = {
                CONF_API_KEY: self._api_key,
                "metro": self.selected_types.get("metro", False),
                "bus": self.selected_types.get("bus", False),
                "bike": self.selected_types.get("bike", False),
                "parking": self.selected_types.get("parking", False),
                "bike_stations": user_input.get("bike_stations", []),
                "parking_ids": user_input.get("parking_ids", []),
                "metro_lines": user_input.get("metro_lines", []),
                "metro_stations": user_input.get("metro_stations", []),
                "metro_rames": bool(user_input.get("metro_rames", False)),
                "bus_entries": self._bus_entries,
            }

            return self.async_create_entry(title="STAR Rennes Live", data=data)

        return self.async_show_form(step_id="select_equipment", data_schema=vol.Schema(schema))

    # -------------------------------------------------------------------------
    # BUS — MÉTHODES BASÉES SUR LA TOPOLOGIE
    # -------------------------------------------------------------------------

    async def _bus_get_line_list(self):
        api = StarBusAPI(self._client())

        # dict nomcourt -> id
        lines = await api.async_get_lines()

        # mapping nomcourt -> idligne
        self._bus_line_map = lines

        return [
            {"label": code, "value": code}
            for code in sorted(lines.keys())
        ]


    async def _bus_get_stop_list(self, line_code):
        api = StarBusAPI(self._client())

        line_id = self._bus_line_map[line_code]
        stops = await api.async_get_stops(line_id)  # nomarret -> idarret

        self._bus_stop_map = stops
        return sorted(stops.keys())


    async def _bus_get_destination_list(self, line_code):
        api = StarBusAPI(self._client())

        line_id = self._bus_line_map[line_code]
        dests = await api.async_get_destinations(line_id)  # nomarretarrivee -> sens

        self._bus_dest_map = dests
        return sorted(dests.keys())


    async def _bus_get_icon_url(self, line_code):
        api = StarBusAPI(self._client())

        line_id = self._bus_line_map[line_code]
        return await api.async_get_icon_url(line_id)

    @staticmethod
    def async_get_options_flow(config_entry):
        from .options_flow import StarRennesOptionsFlow
        return StarRennesOptionsFlow(config_entry)
