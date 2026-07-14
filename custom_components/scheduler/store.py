import logging
import secrets
from collections import OrderedDict
from typing import MutableMapping, cast

import attr
from homeassistant.core import callback, HomeAssistant
from homeassistant.loader import bind_hass
from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_NAME,
    CONF_CONDITIONS,
)
from homeassistant.helpers.storage import Store
from . import const

_LOGGER = logging.getLogger(__name__)

DATA_REGISTRY = f"{const.DOMAIN}_storage"
STORAGE_KEY = f"{const.DOMAIN}.storage"
STORAGE_VERSION = 5
SAVE_DELAY = 10


@attr.s(slots=True, frozen=True)
class ActionEntry:
    """Action storage Entry."""

    service = attr.ib(type=str, default="")
    # legacy single-entity target, retained only so pre-v5 payloads
    # (scheduler.add / scheduler.copy service calls, old card versions)
    # can still be parsed; normalized into `target` before storage
    entity_id = attr.ib(type=str, default=None)
    service_data = attr.ib(type=dict, default={})
    # HA-style target object:
    # { entity_id: [], device_id: [], area_id: [], floor_id: [], label_id: [] }
    # entity membership of device/area/floor/label references is resolved at
    # execution time (actions.resolve_target), not at save time
    target = attr.ib(type=dict, default=None)
    # optional include/exclude entity patterns constraining what the
    # dynamic parts of `target` may resolve to (see const.ATTR_TARGET_FILTER)
    target_filter = attr.ib(type=dict, default=None)


def normalize_action_target(action: dict) -> dict:
    """normalize an action dict to carry a canonical target object.

    - wraps a legacy flat `entity_id` (str) into target.entity_id ([str])
    - coerces scalar target values into lists, drops empty keys
    - drops the legacy entity_id key once merged into target
    """
    action = dict(action)
    target = dict(action.get(const.ATTR_TARGET) or {})

    # coerce all target values to lists of str, drop empties / unknown keys
    normalized = {}
    for key in const.TARGET_KEYS:
        value = target.get(key)
        if value is None:
            continue
        if isinstance(value, str):
            value = [value]
        value = [str(e) for e in value if e]
        if value:
            normalized[key] = sorted(set(value))

    # merge legacy flat entity_id into the target object
    legacy_entity = action.pop(ATTR_ENTITY_ID, None)
    if legacy_entity:
        legacy_list = [legacy_entity] if isinstance(legacy_entity, str) else list(legacy_entity)
        merged = sorted(set(normalized.get(ATTR_ENTITY_ID, []) + legacy_list))
        normalized[ATTR_ENTITY_ID] = merged

    action[const.ATTR_TARGET] = normalized if normalized else None
    action[ATTR_ENTITY_ID] = None

    # normalize the target filter: list values, drop empty keys
    target_filter = dict(action.get(const.ATTR_TARGET_FILTER) or {})
    normalized_filter = {}
    for key in [const.ATTR_INCLUDE, const.ATTR_EXCLUDE]:
        value = target_filter.get(key)
        if value is None:
            continue
        if isinstance(value, str):
            value = [value]
        value = [str(e) for e in value if e]
        if value:
            normalized_filter[key] = value
    action[const.ATTR_TARGET_FILTER] = normalized_filter if normalized_filter else None
    return action


@attr.s(slots=True, frozen=True)
class ConditionEntry:
    """Condition storage Entry."""

    entity_id = attr.ib(type=str, default=None)
    attribute = attr.ib(type=str, default=None)
    value = attr.ib(type=str, default=None)
    match_type = attr.ib(type=str, default=None)


@attr.s(slots=True, frozen=True)
class TimeslotEntry:
    """Timeslot storage Entry."""

    start = attr.ib(type=str, default=None)
    stop = attr.ib(type=str, default=None)
    conditions = attr.ib(type=[ConditionEntry], default=[])
    condition_type = attr.ib(type=str, default=None)
    track_conditions = attr.ib(type=bool, default=False)
    actions = attr.ib(type=[ActionEntry], default=[])


@attr.s(slots=True, frozen=True)
class ScheduleEntry:
    """Schedule storage Entry."""

    schedule_id = attr.ib(type=str, default=None)
    weekdays = attr.ib(type=list, default=[])
    start_date = attr.ib(type=str, default=None)
    end_date = attr.ib(type=str, default=None)
    timeslots = attr.ib(type=[TimeslotEntry], default=[])
    repeat_type = attr.ib(type=str, default=None)
    name = attr.ib(type=str, default=None)
    enabled = attr.ib(type=bool, default=True)


@attr.s(slots=True, frozen=True)
class TagEntry:
    """Tag storage Entry."""

    name = attr.ib(type=str, default=None)
    schedules = attr.ib(type=[str], default=[])


def parse_schedule_data(data: dict):
    if const.ATTR_TIMESLOTS in data:
        timeslots = []
        for item in data[const.ATTR_TIMESLOTS]:
            timeslot = TimeslotEntry(**item)
            if CONF_CONDITIONS in item and item[CONF_CONDITIONS]:
                conditions = []
                for condition in item[CONF_CONDITIONS]:
                    conditions.append(ConditionEntry(**condition))
                timeslot = attr.evolve(timeslot, **{CONF_CONDITIONS: conditions})
            if const.ATTR_ACTIONS in item and item[const.ATTR_ACTIONS]:
                actions = []
                for action in item[const.ATTR_ACTIONS]:
                    actions.append(ActionEntry(**normalize_action_target(action)))
                timeslot = attr.evolve(timeslot, **{const.ATTR_ACTIONS: actions})
            timeslots.append(timeslot)
        data[const.ATTR_TIMESLOTS] = timeslots
    return data


class MigratableStore(Store):
    async def _async_migrate_func(self, old_version, data: dict):

        def remove_unequal_number_conditions(timeslots):
            """ensure all timeslots have the same number of conditions"""
            if len(timeslots) > 1 and not all(
                len(el["conditions"]) == len(timeslots[0]["conditions"])
                for el in timeslots
            ):
                return [
                    {
                        **slot,
                        "conditions": timeslots[0]["conditions"]
                    }
                    for slot in timeslots
                ]
            return timeslots

        if old_version < 2:
            data["schedules"] = (
                [
                    {
                        **entry,
                        const.ATTR_START_DATE: entry[const.ATTR_START_DATE]
                        if const.ATTR_START_DATE in entry
                        else None,
                        const.ATTR_END_DATE: entry[const.ATTR_END_DATE]
                        if const.ATTR_END_DATE in entry
                        else None,
                    }
                    for entry in data["schedules"]
                ]
                if "schedules" in data
                else []
            )
        if old_version < 3:
            data["schedules"] = (
                [
                    {
                        **entry,
                        const.ATTR_TIMESLOTS: remove_unequal_number_conditions(entry[const.ATTR_TIMESLOTS])
                    }
                    for entry in data["schedules"]
                ]
                if "schedules" in data
                else []
            )
        if old_version < 5:
            # v5: actions carry a HA-style target object instead of a flat
            # entity_id. Wrap legacy entity_id into { entity_id: [...] } and
            # normalize any pre-existing target dicts (v4) to list values.
            def migrate_timeslot(timeslot: dict):
                return {
                    **timeslot,
                    const.ATTR_ACTIONS: [
                        normalize_action_target(action)
                        for action in timeslot.get(const.ATTR_ACTIONS, [])
                    ],
                }

            data["schedules"] = (
                [
                    {
                        **entry,
                        const.ATTR_TIMESLOTS: [
                            migrate_timeslot(slot)
                            for slot in entry.get(const.ATTR_TIMESLOTS, [])
                        ],
                    }
                    for entry in data["schedules"]
                ]
                if "schedules" in data
                else []
            )
        return data


class ScheduleStorage:
    """Class to hold scheduler data."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the storage."""
        self.hass = hass
        self.schedules: MutableMapping[str, ScheduleEntry] = {}
        self.tags: MutableMapping[str, TagEntry] = {}
        self.time_shutdown = None
        self._store = MigratableStore(hass, STORAGE_VERSION, STORAGE_KEY)

    async def async_load(self) -> None:
        """Load the registry of schedule entries."""
        data = await self._store.async_load()
        schedules: "OrderedDict[str, ScheduleEntry]" = OrderedDict()
        tags: "OrderedDict[str, TagEntry]" = OrderedDict()

        if data is not None:

            if "schedules" in data:
                for entry in data["schedules"]:
                    entry = parse_schedule_data(entry)
                    schedules[entry[const.ATTR_SCHEDULE_ID]] = ScheduleEntry(
                        schedule_id=entry[const.ATTR_SCHEDULE_ID],
                        weekdays=entry[const.ATTR_WEEKDAYS],
                        start_date=entry[const.ATTR_START_DATE],
                        end_date=entry[const.ATTR_END_DATE],
                        timeslots=entry[const.ATTR_TIMESLOTS],
                        repeat_type=entry[const.ATTR_REPEAT_TYPE],
                        name=entry[ATTR_NAME],
                        enabled=entry[const.ATTR_ENABLED],
                    )

            if "tags" in data:
                for entry in data["tags"]:
                    tags[entry[ATTR_NAME]] = TagEntry(
                        name=entry[ATTR_NAME],
                        schedules=entry[const.ATTR_SCHEDULES],
                    )

            if "time_shutdown" in data:
                self.time_shutdown = data["time_shutdown"]

        self.schedules = schedules
        self.tags = tags

    @callback
    def async_schedule_save(self) -> None:
        """Schedule saving the registry of schedules."""
        self._store.async_delay_save(self._data_to_save, SAVE_DELAY)

    async def async_save(self) -> None:
        """Save the registry of schedules."""
        await self._store.async_save(self._data_to_save())

    @callback
    def _data_to_save(self) -> dict:
        """Return data for the registry for schedules to store in a file."""
        store_data = {}

        store_data["schedules"] = []
        store_data["tags"] = []

        for entry in self.schedules.values():
            item = {
                const.ATTR_SCHEDULE_ID: entry.schedule_id,
                const.ATTR_TIMESLOTS: [],
                const.ATTR_WEEKDAYS: entry.weekdays,
                const.ATTR_START_DATE: entry.start_date,
                const.ATTR_END_DATE: entry.end_date,
                const.ATTR_REPEAT_TYPE: entry.repeat_type,
                ATTR_NAME: entry.name,
                const.ATTR_ENABLED: entry.enabled,
            }
            for slot in entry.timeslots:
                timeslot = {
                    const.ATTR_START: slot.start,
                    const.ATTR_STOP: slot.stop,
                    CONF_CONDITIONS: [],
                    const.ATTR_CONDITION_TYPE: slot.condition_type,
                    const.ATTR_TRACK_CONDITIONS: slot.track_conditions,
                    const.ATTR_ACTIONS: [],
                }
                if slot.conditions:
                    for condition in slot.conditions:
                        timeslot[CONF_CONDITIONS].append(attr.asdict(condition))
                if slot.actions:
                    for action in slot.actions:
                        timeslot[const.ATTR_ACTIONS].append(attr.asdict(action))
                item[const.ATTR_TIMESLOTS].append(timeslot)
            store_data["schedules"].append(item)

        store_data["tags"] = [attr.asdict(entry) for entry in self.tags.values()]

        if self.time_shutdown:
            store_data["time_shutdown"] = self.time_shutdown

        return store_data

    async def async_delete(self):
        """Delete config."""
        _LOGGER.warning("Removing scheduler configuration data!")
        self.schedules = {}
        self.tags = {}
        await self._store.async_remove()

    @callback
    def async_get_schedule(self, entity_id) -> dict:
        """Get an existing ScheduleEntry by id."""
        res = self.schedules.get(entity_id)
        return attr.asdict(res) if res else None

    @callback
    def async_get_schedules(self) -> dict:
        """Get an existing ScheduleEntry by id."""
        res = {}
        for (key, val) in self.schedules.items():
            res[key] = attr.asdict(val)
        return res

    @callback
    def async_create_schedule(self, data: dict) -> ScheduleEntry:
        """Create a new ScheduleEntry."""
        if const.ATTR_SCHEDULE_ID in data:
            schedule_id = data[const.ATTR_SCHEDULE_ID]
            del data[const.ATTR_SCHEDULE_ID]
            if schedule_id in self.schedules:
                return
        else:
            schedule_id = secrets.token_hex(3)
            while schedule_id in self.schedules:
                schedule_id = secrets.token_hex(3)

        data = parse_schedule_data(data)
        new_schedule = ScheduleEntry(**data, schedule_id=schedule_id)
        self.schedules[schedule_id] = new_schedule
        self.async_schedule_save()
        return new_schedule

    @callback
    def async_delete_schedule(self, schedule_id: str) -> None:
        """Delete ScheduleEntry."""
        if schedule_id in self.schedules:
            del self.schedules[schedule_id]
            self.async_schedule_save()
            return True
        return False

    @callback
    def async_update_schedule(self, schedule_id: str, changes: dict) -> ScheduleEntry:
        """Update existing ScheduleEntry."""
        old = self.schedules[schedule_id]
        changes = parse_schedule_data(changes)
        new = self.schedules[schedule_id] = attr.evolve(old, **changes)
        self.async_schedule_save()
        return new

    @callback
    def async_get_tag(self, name: str) -> dict:
        """Get an existing TagEntry by id."""
        res = self.tags.get(name)
        return attr.asdict(res) if res else None

    @callback
    def async_get_tags(self) -> dict:
        """Get an existing TagEntry by id."""
        res = {}
        for (key, val) in self.tags.items():
            res[key] = attr.asdict(val)
        return res

    @callback
    def async_create_tag(self, data: dict) -> TagEntry:
        """Create a new TagEntry."""
        name = data[ATTR_NAME] if ATTR_NAME in data else None
        if not name or name in data:
            return None

        new_tag = TagEntry(**data)
        self.tags[name] = new_tag
        self.async_schedule_save()
        return new_tag

    @callback
    def async_delete_tag(self, name: str) -> None:
        """Delete TagEntry."""
        if name in self.tags:
            del self.tags[name]
            self.async_schedule_save()
            return True
        return False

    @callback
    def async_update_tag(self, name: str, changes: dict) -> TagEntry:
        """Update existing TagEntry."""
        old = self.tags[name]
        changes = parse_schedule_data(changes)
        new = self.tags[name] = attr.evolve(old, **changes)
        self.async_schedule_save()
        return new

    @callback
    def async_get_time_shutdown(self) -> dict:
        """Get the shutdown time and clear the stored value afterwards."""
        res = self.time_shutdown
        self.time_shutdown = None
        self.async_schedule_save()
        return res

    @callback
    async def async_set_time_shutdown(self, value: str):
        """Set the shutdown time and store it immediately."""
        self.time_shutdown = value
        await self.async_save()

@bind_hass
async def async_get_registry(hass: HomeAssistant) -> ScheduleStorage:
    """Return alarmo storage instance."""
    task = hass.data.get(DATA_REGISTRY)

    if task is None:

        async def _load_reg() -> ScheduleStorage:
            registry = ScheduleStorage(hass)
            await registry.async_load()
            return registry

        task = hass.data[DATA_REGISTRY] = hass.async_create_task(_load_reg())

    return cast(ScheduleStorage, await task)
