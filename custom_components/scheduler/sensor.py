"""Scheduler sensor platform: one sensor listing every scheduled entity.

`sensor.scheduled_entities` lets stock and template-based cards check
whether an entity is acted on by a schedule, without websocket access:

    {{ 'light.kitchen' in state_attr('sensor.scheduled_entities', 'entities') }}

State: number of entities with at least one schedule.
Attribute `entities`: {entity_id: {"schedules": n, "enabled": m}}.

The `entities` attribute is excluded from the recorder: it is derived from
the schedules, can grow to tens of kB on large sites, and history of it has
no use. Membership comes from entity_schedules.async_get_scheduled_entities,
the same resolution the websocket commands and schedule execution use.
"""
import logging

from homeassistant.components.sensor import SensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback

from . import const
from .entity_schedules import (
    async_get_scheduled_entities,
    async_track_membership_changes,
)

_LOGGER = logging.getLogger(__name__)

ATTR_ENTITIES = "entities"


async def async_setup_entry(hass: HomeAssistant, _config_entry, async_add_entities):
    """Set up the scheduled-entities sensor."""
    coordinator = hass.data[const.DOMAIN]["coordinator"]
    async_add_entities([ScheduledEntitiesSensor(coordinator)])


class ScheduledEntitiesSensor(SensorEntity):
    """Lists all entities that some schedule acts on."""

    _attr_should_poll = False
    _attr_has_entity_name = False
    _attr_name = "Scheduled entities"
    _attr_icon = "mdi:calendar-check"
    # diagnostic: keeps it out of auto-generated dashboards, and out of
    # area/floor/label targets (resolve_target skips categorised entities)
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _unrecorded_attributes = frozenset({ATTR_ENTITIES})

    def __init__(self, coordinator) -> None:
        self.entity_id = "sensor.scheduled_entities"
        self._attr_unique_id = f"{coordinator.id}_scheduled_entities"
        # deliberately not attached to the Scheduler device: since HA 2026.9
        # the friendly name of a device-attached entity is always prefixed
        # with the device name ("Scheduler Scheduled entities")
        self._entities: dict = {}

    @property
    def native_value(self) -> int:
        return len(self._entities)

    @property
    def extra_state_attributes(self) -> dict:
        return {ATTR_ENTITIES: self._entities}

    @callback
    def _async_refresh(self) -> None:
        """recompute; write state only when the result actually changed"""
        entities = async_get_scheduled_entities(self.hass)
        if entities == self._entities:
            return
        self._entities = entities
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        self._entities = async_get_scheduled_entities(self.hass)
        # next_trigger is not part of this sensor, so timer updates are
        # ignored: the sensor only changes when membership/enabled change
        self.async_on_remove(
            async_track_membership_changes(
                self.hass, self._async_refresh, include_timer=False
            )
        )
