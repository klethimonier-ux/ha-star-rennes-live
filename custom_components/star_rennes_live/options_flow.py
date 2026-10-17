import voluptuous as vol
import homeassistant.helpers.config_validation as cv

from homeassistant import config_entries
from homeassistant.helpers.selector import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    CONF_API_KEY,
    LINE_KEY,
    LINE_NAME_KEY,
    STOP_NAME_KEY,
    DESTINATION_KEY,
    SENS_KEY,
    ICON_URL_KEY,
)

from .api.client import StarClient, async_validate_api_key
from .api.bike_api import StarBikeAPI
from .api.bus_api import StarBusAPI
from .api.metro_api import StarMetroAPI
from .api.parking_api import StarParkingAPI

SERVICE_TYPES = ("bus", "metro", "bike", "parking")


class StarRennesOptionsFlow(config_entries.OptionsFlow):
    """Options flow for STAR Rennes.

    Les options sont enregistrées via async_create_entry ; __init__.py
    recharge alors l'intégration, donc les changements s'appliquent
    immédiatement.
    """

    def __init__(self, config_entry):
        self.entry = config_entry
        self.data = dict(config_entry.options or config_entry.data)

        self.selected_types = {
            "bus": bool(self.data.get("bus_entries")),
            "metro": bool(
                self.data.get("metro_lines")
                or self.data.get("metro_stations")
                or self.data.get("metro_rames")
            ),
            "bike": bool(self.data.get("bike_stations")),
            "parking": bool(self.data.get("parking_ids")),
        }

        # Options en cours d'édition, conservées pendant l'ajout d'un bus
        self._pending = None

        self._bus_line_map = {}
        self._bus_dest_map = {}
        self._bus_user_inputs = {}

    def _client(self) -> StarClient:
        return StarClient(
            async_get_clientsession(self.hass), self.data.get(CONF_API_KEY) or None
        )

    # -------------------------------------------------------------------------
    # PAGE 1 : CHOIX DES TYPES + CLÉ D'API
    # -------------------------------------------------------------------------
    async def async_step_init(self, user_input=None):
        errors = {}

        if user_input is not None:
            api_key = (user_input.get(CONF_API_KEY) or "").strip()
            if api_key and api_key != self.data.get(CONF_API_KEY):
                error = await async_validate_api_key(
                    async_get_clientsession(self.hass), api_key
                )
                if error:
                    errors[CONF_API_KEY] = error

            if not errors:
                self.data[CONF_API_KEY] = api_key
                self.selected_types = {
                    key: bool(user_input.get(key)) for key in SERVICE_TYPES
                }
                return await self.async_step_configure()

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional("bus", default=self.selected_types["bus"]): bool,
                    vol.Optional("metro", default=self.selected_types["metro"]): bool,
                    vol.Optional("bike", default=self.selected_types["bike"]): bool,
                    vol.Optional("parking", default=self.selected_types["parking"]): bool,
                    # suggested_value (et non default) : vider le champ supprime la clé
                    vol.Optional(
                        CONF_API_KEY,
                        description={"suggested_value": self.data.get(CONF_API_KEY, "")},
                    ): selector({"text": {"type": "password"}}),
                }
            ),
            errors=errors,
        )

    # -------------------------------------------------------------------------
    # PAGE 2 : CONFIGURATION DÉTAILLÉE
    # -------------------------------------------------------------------------
    async def async_step_configure(self, user_input=None):
        if user_input is None:
            schema = await self._async_configure_schema(self._client())
            return self.async_show_form(step_id="configure", data_schema=vol.Schema(schema))

        new_data = self._configured_data(user_input)

        # Ajout d'un bus : on garde les modifications ci-dessus en attente
        if self.selected_types["bus"] and user_input.get("add_bus"):
            self._pending = new_data
            return await self.async_step_bus_line()

        return self.async_create_entry(title="", data=new_data)

    async def _async_configure_schema(self, client) -> dict:
        """Champs de la page de configuration, pour les services cochés."""
        schema = {}

        # BUS
        if self.selected_types["bus"]:
            existing_bus = {
                str(idx): f"{b[LINE_NAME_KEY]} → {b[DESTINATION_KEY]} ({b[STOP_NAME_KEY]})"
                for idx, b in enumerate(self.data.get("bus_entries", []))
            }

            schema[vol.Optional("bus_existing", default=list(existing_bus.keys()))] = cv.multi_select(existing_bus)
            schema[vol.Optional("add_bus", default=False)] = bool

        # METRO
        if self.selected_types["metro"]:
            metro_lines = {"A": "Ligne A", "B": "Ligne B"}
            schema[vol.Optional("metro_lines", default=self.data.get("metro_lines", []))] = cv.multi_select(metro_lines)

            raw = await StarMetroAPI(client).async_get_stations_topology()
            stations = {str(sid): st["name"] for sid, st in raw.items()}
            schema[vol.Optional("metro_stations", default=self.data.get("metro_stations", []))] = cv.multi_select(stations)
            # Schéma des lignes avec la position des rames (GTFS ~8 Mo / jour)
            schema[vol.Optional("metro_rames", default=bool(self.data.get("metro_rames", False)))] = bool

        # VELO
        if self.selected_types["bike"]:
            raw = await StarBikeAPI(client).async_get_stations()
            stations = {sid: st["name"] for sid, st in raw.items()}
            schema[vol.Optional("bike_stations", default=self.data.get("bike_stations", []))] = cv.multi_select(stations)

        # PARKING
        if self.selected_types["parking"]:
            raw = await StarParkingAPI(client).async_get_all()
            parking_list = {pid: p["name"] for pid, p in raw.items()}
            schema[vol.Optional("parking_ids", default=self.data.get("parking_ids", []))] = cv.multi_select(parking_list)

        return schema

    def _configured_data(self, user_input: dict) -> dict:
        """Nouvelle configuration : services décochés vidés, sélections reprises du formulaire."""
        new_data = dict(self.data)
        selected = self.selected_types

        # Drapeaux de service tenus à jour (lus par sensor.py)
        for key in SERVICE_TYPES:
            new_data[key] = selected[key]

        keep_indices = {int(i) for i in user_input.get("bus_existing", [])} if selected["bus"] else set()
        new_data["bus_entries"] = [
            b for idx, b in enumerate(self.data.get("bus_entries", [])) if idx in keep_indices
        ]
        new_data["metro_lines"] = user_input.get("metro_lines", []) if selected["metro"] else []
        new_data["metro_stations"] = user_input.get("metro_stations", []) if selected["metro"] else []
        new_data["metro_rames"] = bool(user_input.get("metro_rames", False)) if selected["metro"] else False
        new_data["bike_stations"] = user_input.get("bike_stations", []) if selected["bike"] else []
        new_data["parking_ids"] = user_input.get("parking_ids", []) if selected["parking"] else []
        return new_data

    # -------------------------------------------------------------------------
    # AJOUT D’UN BUS — ÉTAPE 1 : CHOIX DE LA LIGNE
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
    # AJOUT D’UN BUS — ÉTAPE 2 : CHOIX DE L’ARRÊT
    # -------------------------------------------------------------------------
    async def async_step_bus_stop(self, user_input=None):
        if user_input is None:
            line_code = self._bus_user_inputs["bus_line"]
            stop_list = await self._bus_get_stop_list(line_code)

            schema = vol.Schema({
                vol.Required("bus_stop"): selector({
                    "select": {"options": stop_list, "mode": "dropdown"}
                })
            })

            return self.async_show_form(step_id="bus_stop", data_schema=schema)

        self._bus_user_inputs["bus_stop"] = user_input["bus_stop"]
        return await self.async_step_bus_destination()

    # -------------------------------------------------------------------------
    # AJOUT D’UN BUS — ÉTAPE 3 : DESTINATION
    # -------------------------------------------------------------------------
    async def async_step_bus_destination(self, user_input=None):
        if user_input is None:
            line_code = self._bus_user_inputs["bus_line"]
            dest_list = await self._bus_get_destination_list(line_code)

            schema = vol.Schema({
                vol.Required("bus_destination"): selector({
                    "select": {"options": dest_list, "mode": "dropdown"}
                })
            })

            return self.async_show_form(step_id="bus_destination", data_schema=schema)

        dest_name = user_input["bus_destination"]
        sens = self._bus_dest_map.get(dest_name, 0)

        line_code = self._bus_user_inputs["bus_line"]
        line_id = self._bus_line_map[line_code]
        stop_name = self._bus_user_inputs["bus_stop"]

        icon_url = await self._bus_get_icon_url(line_code)

        new_bus_entry = {
            LINE_KEY: line_id,
            LINE_NAME_KEY: line_code,
            STOP_NAME_KEY: stop_name,
            DESTINATION_KEY: dest_name,
            SENS_KEY: sens,
            ICON_URL_KEY: icon_url,
        }

        # On repart des modifications faites sur la page précédente
        new_data = dict(self._pending if self._pending is not None else self.data)
        new_data["bus_entries"] = [*new_data.get("bus_entries", []), new_bus_entry]
        new_data["bus"] = True

        return self.async_create_entry(title="", data=new_data)

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
