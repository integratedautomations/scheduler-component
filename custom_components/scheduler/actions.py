import logging
import re

from homeassistant.core import (
    HomeAssistant,
    callback,
    CoreState,
)
from homeassistant.const import (
    CONF_SERVICE,
    ATTR_SERVICE_DATA,
    CONF_SERVICE_DATA,
    CONF_DELAY,
    ATTR_ENTITY_ID,
    STATE_UNKNOWN,
    STATE_UNAVAILABLE,
    CONF_CONDITIONS,
    CONF_ATTRIBUTE,
    CONF_STATE,
    CONF_ACTION
)
from homeassistant.components.climate import (
    SERVICE_SET_TEMPERATURE,
    SERVICE_SET_HVAC_MODE,
    ATTR_HVAC_MODE,
    ATTR_TEMPERATURE,
    ATTR_TARGET_TEMP_LOW,
    ATTR_TARGET_TEMP_HIGH,
    DOMAIN as CLIMATE_DOMAIN,
)
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_call_later,
)
from homeassistant.helpers.service import async_call_from_config
from homeassistant.helpers.dispatcher import (
    async_dispatcher_connect,
    async_dispatcher_send,
)
from homeassistant.helpers import (
    area_registry as ar,
    device_registry as dr,
    entity_registry as er,
    floor_registry as fr,
    label_registry as lr,
)
from homeassistant.helpers.debounce import Debouncer

from . import const
from .store import ScheduleEntry

_LOGGER = logging.getLogger(__name__)

ACTION_WAIT = "wait"
ACTION_WAIT_STATE_CHANGE = "wait_state_change"

# entity registry changes that can affect target resolution
ENTITY_REGISTRY_RELEVANT_CHANGES = {
    "area_id",
    "device_id",
    "labels",
    "entity_category",
    "disabled_by",
    "hidden_by",
    "entity_id",
}
DEVICE_REGISTRY_RELEVANT_CHANGES = {"area_id", "labels"}
AREA_REGISTRY_RELEVANT_CHANGES = {"floor_id", "labels"}


def match_pattern(pattern: str, value: str) -> bool:
    """mirror of the card's matchPattern (lib/patterns.ts):
    - plain patterns: exact entity match, or domain match when the pattern
      has no dot but the value does
    - wildcard patterns ('*') and /regex/ patterns
    """
    if not value:
        return False
    if re.fullmatch(r"[a-z0-9_\.]+", pattern):
        if "." not in pattern and "." in value:
            return pattern == value.split(".")[0]
        return pattern == value
    try:
        if pattern.startswith("/") and pattern.endswith("/"):
            return re.search(pattern[1:-1], value) is not None
        if "*" in pattern:
            return re.search("^" + pattern.replace("*", ".*") + "$", value) is not None
    except re.error:
        pass
    return False


def entity_allowed_by_filter(entity_id: str, target_filter: dict | None) -> bool:
    """apply a schedule's stored include/exclude patterns to an entity"""
    if not target_filter:
        return True
    include = target_filter.get(const.ATTR_INCLUDE) or ["*"]
    exclude = target_filter.get(const.ATTR_EXCLUDE) or []
    if not any(match_pattern(pattern, entity_id) for pattern in include):
        return False
    if any(match_pattern(pattern, entity_id) for pattern in exclude):
        return False
    return True


def target_is_dynamic(target: dict | None) -> bool:
    """return True if the target contains device/area/floor/label references
    whose entity membership can change over time"""
    if not target:
        return False
    return any(target.get(key) for key in const.DYNAMIC_TARGET_KEYS)


def resolve_target(hass: HomeAssistant, target: dict | None, domain: str = None, target_filter: dict = None) -> list:
    """expand a HA-style target object into a list of concrete entity IDs.

    Single source of truth for target resolution; called at execution time
    (when a timeslot's actions are queued) and by the frontend preview
    websocket command — never at save time, so schedules automatically pick
    up entities that join a targeted device/area/floor/label later.

    Mirrors HA's own service targeting semantics:
    - explicit entity_ids are always included, unfiltered
    - floor targets expand to their areas
    - label targets expand to labeled areas, devices and entities
    - area targets include entities assigned to the area directly, plus
      entities of devices in the area that have no area override of their own
    - device targets include all entities of the device
    - indirectly referenced entities are skipped when disabled, hidden, or
      config/diagnostic category, and filtered to the action's service domain
    - indirectly referenced entities are additionally constrained by the
      optional target_filter (include/exclude patterns stamped by the card
      that created the schedule); explicitly picked entity_ids are exempt,
      the picker already vetted those at selection time
    """
    if not target:
        return []

    entity_reg = er.async_get(hass)
    device_reg = dr.async_get(hass)
    area_reg = ar.async_get(hass)

    def as_set(key):
        value = target.get(key) or []
        if isinstance(value, str):
            value = [value]
        return set(value)

    explicit_entities = as_set(ATTR_ENTITY_ID)
    device_ids = as_set(const.ATTR_DEVICE_ID)
    area_ids = as_set(const.ATTR_AREA_ID)
    floor_ids = as_set(const.ATTR_FLOOR_ID)
    label_ids = as_set(const.ATTR_LABEL_ID)

    # floors -> areas
    if floor_ids:
        for area in area_reg.areas.values():
            if area.floor_id and area.floor_id in floor_ids:
                area_ids.add(area.id)

    # labels -> areas / devices
    if label_ids:
        for area in area_reg.areas.values():
            if set(area.labels) & label_ids:
                area_ids.add(area.id)
        for device in device_reg.devices.values():
            if set(device.labels) & label_ids:
                device_ids.add(device.id)

    # areas -> devices located in those areas
    area_device_ids = set()
    if area_ids:
        for device in device_reg.devices.values():
            if device.area_id and device.area_id in area_ids:
                area_device_ids.add(device.id)

    resolved = set(explicit_entities)

    if device_ids or area_ids or area_device_ids or label_ids:
        # skip domain filtering for services that are not tied to an entity
        # domain (e.g. homeassistant.turn_off applies across domains)
        filter_domain = domain if domain and domain != "homeassistant" else None

        for entry in entity_reg.entities.values():
            if (
                entry.disabled
                or entry.hidden_by is not None
                or entry.entity_category is not None
            ):
                continue

            include = False
            if entry.device_id and entry.device_id in device_ids:
                # explicitly targeted device (or label-selected device):
                # all its entities are included regardless of area override
                include = True
            elif entry.area_id:
                include = entry.area_id in area_ids
            elif entry.device_id and entry.device_id in area_device_ids:
                # entity inherits its device's area
                include = True
            if not include and label_ids and (set(entry.labels) & label_ids):
                include = True

            if not include:
                continue
            if filter_domain and entry.domain != filter_domain:
                continue
            if not entity_allowed_by_filter(entry.entity_id, target_filter):
                continue
            resolved.add(entry.entity_id)

    return sorted(resolved)


def expand_action_target(hass: HomeAssistant, action: dict) -> list:
    """expand an action with a target object into per-entity action dicts,
    ready for parse_service_call / per-entity action queues"""
    base = {
        key: value
        for (key, value) in action.items()
        if key not in [const.ATTR_TARGET, const.ATTR_TARGET_FILTER, ATTR_ENTITY_ID]
    }
    target = action.get(const.ATTR_TARGET)

    # legacy action shape (flat entity_id, no target object)
    if not target:
        if action.get(ATTR_ENTITY_ID):
            return [{**base, ATTR_ENTITY_ID: action[ATTR_ENTITY_ID]}]
        return [base]

    service = action.get(CONF_ACTION, action.get(CONF_SERVICE, ""))
    domain = service.split(".").pop(0) if service else None
    entities = resolve_target(hass, target, domain, action.get(const.ATTR_TARGET_FILTER))
    if not entities:
        _LOGGER.warning(
            "Target {} of action {} resolved to no entities, skipping".format(
                target, service
            )
        )
        return []
    return [{**base, ATTR_ENTITY_ID: entity} for entity in entities]


@callback
def async_setup_target_listener(hass: HomeAssistant):
    """watch entity/device/area/floor/label registries and fire a debounced
    dispatcher signal so schedules with dynamic targets can re-resolve.

    Returns a callable that detaches all listeners."""

    async def notify():
        _LOGGER.debug("Registry changes detected, re-evaluating schedule targets")
        async_dispatcher_send(hass, const.EVENT_TARGET_REGISTRY_UPDATED)

    debouncer = Debouncer(
        hass,
        _LOGGER,
        cooldown=const.TARGET_REGISTRY_UPDATE_DEBOUNCE,
        immediate=False,
        function=notify,
    )

    def relevant(event, tracked_changes) -> bool:
        action = event.data.get("action")
        if action in ("create", "remove"):
            return True
        if action == "update":
            changes = event.data.get("changes", {})
            return bool(set(changes) & tracked_changes)
        return False

    async def entity_registry_updated(event):
        if relevant(event, ENTITY_REGISTRY_RELEVANT_CHANGES):
            await debouncer.async_call()

    async def device_registry_updated(event):
        if relevant(event, DEVICE_REGISTRY_RELEVANT_CHANGES):
            await debouncer.async_call()

    async def area_registry_updated(event):
        if relevant(event, AREA_REGISTRY_RELEVANT_CHANGES):
            await debouncer.async_call()

    async def grouping_registry_updated(event):
        # floor/label removal cascades membership changes; create/rename
        # cannot change membership, so only removal is relevant
        if event.data.get("action") == "remove":
            await debouncer.async_call()

    unsubscribers = [
        hass.bus.async_listen(er.EVENT_ENTITY_REGISTRY_UPDATED, entity_registry_updated),
        hass.bus.async_listen(dr.EVENT_DEVICE_REGISTRY_UPDATED, device_registry_updated),
        hass.bus.async_listen(ar.EVENT_AREA_REGISTRY_UPDATED, area_registry_updated),
        hass.bus.async_listen(fr.EVENT_FLOOR_REGISTRY_UPDATED, grouping_registry_updated),
        hass.bus.async_listen(lr.EVENT_LABEL_REGISTRY_UPDATED, grouping_registry_updated),
    ]

    @callback
    def detach():
        while unsubscribers:
            unsubscribers.pop()()
        debouncer.async_cancel()

    return detach


def parse_service_call(data: dict):
    """turn action data into a service call"""

    service_call = {
        CONF_ACTION: data[CONF_ACTION] if CONF_ACTION in data else data[CONF_SERVICE], # map service->action for backwards compaibility
        CONF_SERVICE_DATA: data[ATTR_SERVICE_DATA],
    }
    if ATTR_ENTITY_ID in data and data[ATTR_ENTITY_ID]:
        service_call[ATTR_ENTITY_ID] = data[ATTR_ENTITY_ID]

    if (
        service_call[CONF_ACTION]
        == "{}.{}".format(CLIMATE_DOMAIN, SERVICE_SET_TEMPERATURE)
        and ATTR_HVAC_MODE in service_call[CONF_SERVICE_DATA]
        and ATTR_ENTITY_ID in service_call
    ):
        # fix for climate integrations which don't support setting hvac_mode and temperature together
        # add small delay between service calls for integrations that have a long processing time
        # set temperature setpoint again for integrations which lose setpoint after switching hvac_mode
        _service_call = [
            {
                CONF_ACTION: "{}.{}".format(CLIMATE_DOMAIN, SERVICE_SET_HVAC_MODE),
                ATTR_ENTITY_ID: service_call[ATTR_ENTITY_ID],
                CONF_SERVICE_DATA: {
                    ATTR_HVAC_MODE: service_call[CONF_SERVICE_DATA][ATTR_HVAC_MODE]
                },
            }
        ]
        if (
            ATTR_TEMPERATURE in service_call[CONF_SERVICE_DATA]
            or ATTR_TARGET_TEMP_LOW in service_call[CONF_SERVICE_DATA]
            or ATTR_TARGET_TEMP_HIGH in service_call[CONF_SERVICE_DATA]
        ):
            _service_call.extend([
                {
                    CONF_ACTION: ACTION_WAIT_STATE_CHANGE,
                    ATTR_ENTITY_ID: service_call[ATTR_ENTITY_ID],
                    CONF_SERVICE_DATA: {
                        CONF_DELAY: 50,
                        CONF_STATE: service_call[CONF_SERVICE_DATA][ATTR_HVAC_MODE]
                    },
                },
                {
                    CONF_ACTION: "{}.{}".format(CLIMATE_DOMAIN, SERVICE_SET_TEMPERATURE),
                    ATTR_ENTITY_ID: service_call[ATTR_ENTITY_ID],
                    CONF_SERVICE_DATA: {
                        x: service_call[CONF_SERVICE_DATA][x]
                        for x in service_call[CONF_SERVICE_DATA]
                        if x != ATTR_HVAC_MODE
                    },
                },
            ])
        return _service_call
    else:
        return [service_call]


def entity_is_available(hass: HomeAssistant, entity, is_target_entity=False):
    """evaluate whether an entity is ready for targeting"""
    state = hass.states.get(entity)
    if state is None:
        return False
    elif state.state == STATE_UNAVAILABLE:
        return False
    elif state.state != STATE_UNKNOWN:
        return True
    elif is_target_entity:
        # only reject unknown state when scheduler is initializing
        coordinator = hass.data["scheduler"]["coordinator"]
        if coordinator.state == const.STATE_INIT:
            return False
        else:
            return True
    else:
        #  for condition entities the unknown state is not allowed
        return False


def action_is_available(hass: HomeAssistant, action: str):
    """evaluate whether a HA action is ready for targeting"""
    if action in [ACTION_WAIT, ACTION_WAIT_STATE_CHANGE]:
        return True
    domain = action.split(".").pop(0)
    domain_service = action.split(".").pop(1)
    return hass.services.has_service(domain, domain_service)


def validate_condition(hass: HomeAssistant, condition: dict, *args):
    """Validate a condition against the current state"""

    if not entity_is_available(hass, condition[ATTR_ENTITY_ID], True):
        return False

    state = hass.states.get(condition[ATTR_ENTITY_ID])

    required = condition[const.ATTR_VALUE]
    actual = state.state if state else None
    if len(args):
        actual = args[0]

    if (
        condition[const.ATTR_MATCH_TYPE]
        in [
            const.MATCH_TYPE_BELOW,
            const.MATCH_TYPE_ABOVE,
        ]
        and isinstance(required, str)
    ):
        # parse condition as numeric if should be smaller or larger than X
        required = float(required)

    if isinstance(required, int):
        try:
            actual = int(float(actual))
        except (ValueError, TypeError):
            return False
    elif isinstance(required, float):
        try:
            actual = float(actual)
        except (ValueError, TypeError):
            return False
    elif isinstance(required, str):
        actual = str(actual).lower()
        required = required.lower()

    if condition[const.ATTR_MATCH_TYPE] == const.MATCH_TYPE_EQUAL:
        result = actual == required
    elif condition[const.ATTR_MATCH_TYPE] == const.MATCH_TYPE_UNEQUAL:
        result = actual != required
    elif condition[const.ATTR_MATCH_TYPE] == const.MATCH_TYPE_BELOW:
        result = actual < required
    elif condition[const.ATTR_MATCH_TYPE] == const.MATCH_TYPE_ABOVE:
        result = actual > required
    else:
        result = False

    # _LOGGER.debug(
    #     "validating condition for {}: required={}, actual={}, match_type={}, result={}"
    #     .format(condition[ATTR_ENTITY_ID], required, actual, condition[const.ATTR_MATCH_TYPE], result)
    # )
    return result


def action_has_effect(action: dict, hass: HomeAssistant):
    """check if action has an effect on the entity"""
    if ATTR_ENTITY_ID not in action:
        return True

    domain = action[CONF_ACTION].split(".").pop(0)
    service = action[CONF_ACTION].split(".").pop(1)
    state = hass.states.get(action[ATTR_ENTITY_ID])
    current_state = state.state if state else None

    if (
        domain == CLIMATE_DOMAIN
        and service in [SERVICE_SET_HVAC_MODE, SERVICE_SET_TEMPERATURE]
        and state
    ):
        if (
            ATTR_HVAC_MODE in action[CONF_SERVICE_DATA]
            and action[CONF_SERVICE_DATA][ATTR_HVAC_MODE] != current_state
        ):
            return True
        if ATTR_TEMPERATURE in action[CONF_SERVICE_DATA] and float(
            state.attributes.get(ATTR_TEMPERATURE, 0) or 0
        ) != float(action[CONF_SERVICE_DATA].get(ATTR_TEMPERATURE)):
            return True
        if ATTR_TARGET_TEMP_LOW in action[CONF_SERVICE_DATA] and float(
            state.attributes.get(ATTR_TARGET_TEMP_LOW, 0) or 0
        ) != float(action[CONF_SERVICE_DATA].get(ATTR_TARGET_TEMP_LOW)):
            return True
        if ATTR_TARGET_TEMP_HIGH in action[CONF_SERVICE_DATA] and float(
            state.attributes.get(ATTR_TARGET_TEMP_HIGH, 0) or 0
        ) != float(action[CONF_SERVICE_DATA].get(ATTR_TARGET_TEMP_HIGH)):
            return True
        return False

    return True


class ActionHandler:
    def __init__(self, hass: HomeAssistant, schedule_id: str):
        """init"""
        self.hass = hass
        self._queues = {}
        self._timer = None
        self.id = schedule_id

        async_dispatcher_connect(
            self.hass, "action_queue_finished", self.async_cleanup_queues
        )

    async def async_queue_actions(self, data: ScheduleEntry, skip_initial_execution = False):
        """add new actions to queue"""
        await self.async_empty_queue()

        conditions = data[CONF_CONDITIONS]
        # expand each action's target (entities/devices/areas/floors/labels)
        # into concrete per-entity actions at execution time
        actions = [
            e
            for x in data[const.ATTR_ACTIONS]
            for expanded in expand_action_target(self.hass, x)
            for e in parse_service_call(expanded)
        ]
        condition_type = data[const.ATTR_CONDITION_TYPE]
        track_conditions = data[const.ATTR_TRACK_CONDITIONS]

        # create an ActionQueue object per targeted entity (such that the tasks are handled independently)
        for action in actions:
            entity = action[ATTR_ENTITY_ID] if ATTR_ENTITY_ID in action else "none"

            if entity not in self._queues:
                self._queues[entity] = ActionQueue(
                    self.hass, self.id, conditions, condition_type, track_conditions
                )

            self._queues[entity].add_action(action)

        for queue in self._queues.copy().values():
            await queue.async_start(skip_initial_execution)

    async def async_cleanup_queues(self, id: str = None):
        """remove all objects from queue which have no remaining tasks"""
        if id is not None and id != self.id or not len(self._queues.keys()):
            return

        # remove all items which are either finished executing
        # or have all their entities available (i.e. conditions have failed beforee)
        queue_items = list(self._queues.keys())
        for key in queue_items:
            if self._queues[key].is_finished() or (
                self._queues[key].is_available() and not self._queues[key].queue_busy
            ):
                await self._queues[key].async_clear()
                self._queues.pop(key)

        if not len(self._queues.keys()):
            _LOGGER.debug("[{}]: Finished execution of tasks".format(self.id))

    async def async_empty_queue(self, **kwargs):
        """remove all objects from queue"""
        restore_time = kwargs.get("restore_time")

        async def async_clear_queue(_now=None):
            """clear queue"""
            if self._timer:
                self._timer()
                self._timer = None

            while len(self._queues.keys()):
                key = list(self._queues.keys())[0]
                await self._queues[key].async_clear()
                self._queues.pop(key)

        if restore_time:
            await self.async_cleanup_queues()
            if not len(self._queues):
                return

            _LOGGER.debug(
                "Waiting for unavailable entities to be restored for {} mins".format(
                    restore_time
                )
            )
            self._timer = async_call_later(
                self.hass, restore_time * 60, async_clear_queue
            )
        else:
            await async_clear_queue()


class ActionQueue:
    def __init__(
        self,
        hass: HomeAssistant,
        id: str,
        conditions: list,
        condition_type: str,
        track_conditions: bool,
    ):
        """create a new action queue"""
        self.hass = hass
        self.id = id
        self._timer = None
        self._action_entities = []
        self._condition_entities = []
        self._listeners = []
        self._state_update_listener = None
        self._conditions = conditions
        self._condition_type = condition_type
        self._queue = []
        self.queue_busy = False
        self._track_conditions = track_conditions
        self._wait_for_available = True

        for condition in conditions:
            if (
                ATTR_ENTITY_ID in condition
                and condition[ATTR_ENTITY_ID] not in self._condition_entities
            ):
                self._condition_entities.append(condition[ATTR_ENTITY_ID])

    def add_action(self, action: dict):
        """add an action to the queue"""
        if (
            ATTR_ENTITY_ID in action
            and action[ATTR_ENTITY_ID]
            and action[ATTR_ENTITY_ID] not in self._action_entities
        ):
            self._action_entities.append(action[ATTR_ENTITY_ID])

        self._queue.append(action)

    async def async_start(self, skip_initial_execution):
        """start execution of the actions in the queue"""

        @callback
        async def async_entity_changed(event):
            """check if actions can be processed"""
            entity = event.data["entity_id"]
            old_state = event.data["old_state"].state if event.data["old_state"] else None
            new_state = event.data["new_state"].state if event.data["new_state"] else None

            if old_state == new_state:
                # no change
                return

            if self.queue_busy:
                return

            if entity not in self._condition_entities and not self._wait_for_available:
                # only watch until entity becomes available in the action entities
                return

            if (
                entity in self._condition_entities
                and old_state
                and new_state
                and old_state not in [STATE_UNAVAILABLE, STATE_UNKNOWN]
                and new_state not in [STATE_UNAVAILABLE, STATE_UNKNOWN]
            ):
                conditions = list(filter(lambda e: e[ATTR_ENTITY_ID] == entity, self._conditions))
                if all([
                    validate_condition(self.hass, item, old_state) == validate_condition(self.hass, item, new_state)
                    for item in conditions
                ]):
                    # ignore if state change has no effect on condition rules
                    return

            _LOGGER.debug(
                "[{}]: State of {} has changed, re-evaluating actions".format(
                    self.id, entity
                )
            )
            await self.async_process_queue()

        watched_entities = list(set(self._condition_entities + self._action_entities))
        if len(watched_entities):
            self._listeners.append(
                async_track_state_change_event(
                    self.hass, watched_entities, async_entity_changed
                )
            )


        if not skip_initial_execution:
            await self.async_process_queue()

            # trigger the queue once when HA has restarted
            if self.hass.state != CoreState.running:
                self._listeners.append(
                    async_dispatcher_connect(
                        self.hass, const.EVENT_STARTED, self.async_process_queue
                    )
                )
        else:
            self._wait_for_available = False

    async def async_clear(self):
        """clear action queue object"""
        if self._timer:
            self._timer()
        self._timer = None

        while len(self._listeners):
            self._listeners.pop()()

        if self._state_update_listener:
            self._state_update_listener()
        self._state_update_listener = None

    def is_finished(self):
        """check whether all queue items are finished"""
        return len(self._queue) == 0

    def is_available(self):
        """check if all actions and entities involved in the task are available"""

        # check actions
        required_actions = [action[CONF_ACTION] for action in self._queue]
        failed_action = next(
            (x for x in required_actions if not action_is_available(self.hass, x)),
            None,
        )
        if failed_action:
            _LOGGER.debug(
                "[{}]: Action {} is unavailable, scheduled task cannot be executed".format(
                    self.id, failed_action
                )
            )
            return False

        # check entities
        watched_entities = list(set(self._condition_entities + self._action_entities))
        failed_entity = next(
            (
                x
                for x in watched_entities
                if not entity_is_available(self.hass, x, x in self._action_entities)
            ),
            None,
        )
        if failed_entity:
            _LOGGER.debug(
                "[{}]: Entity {} is unavailable, scheduled action cannot be executed".format(
                    self.id, failed_entity
                )
            )
            return False

        if self._wait_for_available:
            self._wait_for_available = False

        return True

    async def async_process_queue(self, task_idx=0):
        """walk through the list of tasks and execute the ones that are ready"""
        if self.queue_busy or not self.is_available():
            return

        self.queue_busy = True

        # verify conditions
        conditions_passed = (
            (
                all(validate_condition(self.hass, item) for item in self._conditions)
                if self._condition_type == const.CONDITION_TYPE_AND
                else any(
                    validate_condition(self.hass, item) for item in self._conditions
                )
            )
            if len(self._conditions)
            else True
        )

        if not conditions_passed and len(self._queue):
            _LOGGER.debug(
                "[{}]: Conditions have failed, skipping execution of actions".format(
                    self.id
                )
            )
            if self._track_conditions:
                # postpone tasks
                self.queue_busy = False
                return

            else:
                # abort all items in queue
                while len(self._queue):
                    self._queue.pop()

        skip_task = False

        while task_idx < len(self._queue):
            task = self._queue[task_idx]

            if task[CONF_ACTION] in [ACTION_WAIT, ACTION_WAIT_STATE_CHANGE]:
                if skip_action:
                    task_idx = task_idx + 1
                    continue
                elif task[CONF_ACTION] == ACTION_WAIT_STATE_CHANGE:
                    state = self.hass.states.get(task[ATTR_ENTITY_ID])
                    if CONF_ATTRIBUTE in task[CONF_SERVICE_DATA]:
                        state = state.attributes.get(task[CONF_SERVICE_DATA][CONF_ATTRIBUTE])
                    else:
                        state = state.state
                    if state == task[CONF_SERVICE_DATA][CONF_STATE]:
                        _LOGGER.debug(
                            "[{}]: Entity {} is already set to {}, proceed with next task".format(
                                self.id,
                                task[ATTR_ENTITY_ID],
                                state,
                            )
                        )
                        task_idx = task_idx + 1
                        continue

                @callback
                async def async_timer_finished(_now):
                    self._timer = None
                    if self._state_update_listener:
                        self._state_update_listener()
                    self._state_update_listener = None
                    self.queue_busy = False
                    await self.async_process_queue(task_idx + 1)

                self._timer = async_call_later(
                    self.hass,
                    task[CONF_SERVICE_DATA][CONF_DELAY],
                    async_timer_finished,
                )
                _LOGGER.debug(
                    "[{}]: Postponing next task for {} seconds".format(
                        self.id, task[CONF_SERVICE_DATA][CONF_DELAY]
                    )
                )

                @callback
                async def async_entity_changed(event):
                    entity = event.data["entity_id"]
                    old_state = event.data["old_state"]
                    new_state = event.data["new_state"]

                    if CONF_ATTRIBUTE in task[CONF_SERVICE_DATA]:
                        old_state = old_state.attributes.get(task[CONF_SERVICE_DATA][CONF_ATTRIBUTE])
                        new_state = new_state.attributes.get(task[CONF_SERVICE_DATA][CONF_ATTRIBUTE])
                    else:
                        old_state = old_state.state
                        new_state = new_state.state
                    if old_state == new_state:
                        return
                    _LOGGER.debug(
                        "[{}]: Entity {} was updated from {} to {}".format(
                            self.id,
                            entity,
                            old_state,
                            new_state
                        )
                    )
                    if new_state == task[CONF_SERVICE_DATA][CONF_STATE]:
                        _LOGGER.debug("[{}]: Stop postponing next task".format(self.id))
                        if self._timer:
                            self._timer()
                        self._timer = None
                        self._state_update_listener()
                        self._state_update_listener = None
                        self.queue_busy = False
                        await self.async_process_queue(task_idx + 1)

                if task[CONF_ACTION] == ACTION_WAIT_STATE_CHANGE:
                    self._state_update_listener = async_track_state_change_event(
                        self.hass, task[ATTR_ENTITY_ID], async_entity_changed
                    )
                return

            if ATTR_ENTITY_ID in task:
                _LOGGER.debug(
                    "[{}]: Executing action {} on entity {}".format(
                        self.id, task[CONF_ACTION], task[ATTR_ENTITY_ID]
                    )
                )
            else:
                _LOGGER.debug(
                    "[{}]: Executing action {}".format(self.id, task[CONF_ACTION])
                )

            skip_action = not action_has_effect(task, self.hass)
            if skip_action:
                _LOGGER.debug("[{}]: Action has no effect, skipping".format(self.id))
            else:
                await async_call_from_config(
                    self.hass,
                    task,
                )
            task_idx = task_idx + 1

        self.queue_busy = False

        if not self._track_conditions or not len(self._conditions):
            while len(self._queue):
                self._queue.pop()

            async_dispatcher_send(self.hass, "action_queue_finished", self.id)
        else:
            _LOGGER.debug(
                "[{}]: Done for now, Waiting for conditions to change".format(self.id)
            )
