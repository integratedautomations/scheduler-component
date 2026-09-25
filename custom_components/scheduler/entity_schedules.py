"""Reverse lookup: which schedules act on a given entity.

Backs the `scheduler/entity_schedules` and
`scheduler/subscribe_entity_schedules` websocket commands, used by the
scheduler-card when it is embedded in the more-info dialog.

Membership is decided by running every action's stored (raw) target
through `resolve_target()` with its `target_filter`, exactly as execution
does, so the answer always matches what will actually run. Nothing is
cached: every call recomputes from the store and the registries.
"""
import logging

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.const import ATTR_ENTITY_ID, ATTR_NAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import (
    area_registry as ar,
    config_validation as cv,
    device_registry as dr,
    entity_registry as er,
    floor_registry as fr,
    label_registry as lr,
)
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.event import async_call_later

from . import const
from .actions import resolve_target

_LOGGER = logging.getLogger(__name__)

WS_ENTITY_SCHEDULES = f"{const.DOMAIN}/entity_schedules"
WS_SUBSCRIBE_ENTITY_SCHEDULES = f"{const.DOMAIN}/subscribe_entity_schedules"

MATCH_ENTITY = "entity"
MATCH_DEVICE = "device"
MATCH_AREA = "area"
MATCH_FLOOR = "floor"
MATCH_LABEL = "label"

# most specific first; lower index wins
MATCH_PRECEDENCE = [MATCH_ENTITY, MATCH_DEVICE, MATCH_AREA, MATCH_FLOOR, MATCH_LABEL]

# coalesce bursts of changes (enable_all, bulk registry edits) into one push
SUBSCRIPTION_PUSH_DELAY = 0.25

# registry changes that can alter membership or the reported matched_via name
ENTITY_REGISTRY_CHANGES = {
    "area_id",
    "device_id",
    "labels",
    "entity_category",
    "disabled_by",
    "hidden_by",
    "entity_id",
    "name",
    "original_name",
}
DEVICE_REGISTRY_CHANGES = {"area_id", "labels", "name", "name_by_user"}
# area/floor/label registry "update" events carry no `changes` payload, so
# every update of those is treated as relevant


def _as_list(value) -> list:
    if not value:
        return []
    if isinstance(value, str):
        return [value]
    return list(value)


def _rank(match: dict | None) -> int:
    if match is None:
        return len(MATCH_PRECEDENCE)
    return MATCH_PRECEDENCE.index(match["type"])


@callback
def _entity_name(hass: HomeAssistant, entity_id: str) -> str:
    state = hass.states.get(entity_id)
    if state and state.name:
        return state.name
    entry = er.async_get(hass).async_get(entity_id)
    if entry and (entry.name or entry.original_name):
        return entry.name or entry.original_name
    return entity_id


@callback
def _match_reason(hass: HomeAssistant, entity_id: str, target: dict) -> dict | None:
    """return the most specific way `target` references `entity_id`.

    Only called once resolve_target() has confirmed membership, so this only
    has to explain *why*, following the same area/device rules:
    an entity's own area overrides its device's area.
    """
    if entity_id in _as_list(target.get(ATTR_ENTITY_ID)):
        return {
            "type": MATCH_ENTITY,
            "id": entity_id,
            "name": _entity_name(hass, entity_id),
        }

    entry = er.async_get(hass).async_get(entity_id)
    if entry is None:
        return None

    device_reg = dr.async_get(hass)
    area_reg = ar.async_get(hass)
    device = device_reg.async_get(entry.device_id) if entry.device_id else None
    area_id = entry.area_id or (device.area_id if device else None)
    area = area_reg.async_get_area(area_id) if area_id else None

    device_ids = set(_as_list(target.get(const.ATTR_DEVICE_ID)))
    if device and device.id in device_ids:
        return {
            "type": MATCH_DEVICE,
            "id": device.id,
            "name": device.name_by_user or device.name or device.id,
        }

    area_ids = set(_as_list(target.get(const.ATTR_AREA_ID)))
    if area and area.id in area_ids:
        return {"type": MATCH_AREA, "id": area.id, "name": area.name}

    floor_ids = set(_as_list(target.get(const.ATTR_FLOOR_ID)))
    if area and area.floor_id and area.floor_id in floor_ids:
        floor = fr.async_get(hass).async_get_floor(area.floor_id)
        return {
            "type": MATCH_FLOOR,
            "id": area.floor_id,
            "name": floor.name if floor else area.floor_id,
        }

    label_ids = set(_as_list(target.get(const.ATTR_LABEL_ID)))
    if label_ids:
        labels = set(entry.labels)
        if device:
            labels |= set(device.labels)
        if area:
            labels |= set(area.labels)
        matched = sorted(labels & label_ids)
        if matched:
            label = lr.async_get(hass).async_get_label(matched[0])
            return {
                "type": MATCH_LABEL,
                "id": matched[0],
                "name": label.name if label else matched[0],
            }

    return None


@callback
def _next_trigger(hass: HomeAssistant, schedule_id: str):
    entity = hass.data[const.DOMAIN]["schedules"].get(schedule_id)
    if entity is None or not entity._next_entries:
        return None
    try:
        return entity._timestamps[entity._next_entries[0]]
    except (IndexError, TypeError):
        return None


@callback
def async_get_entity_schedules(hass: HomeAssistant, entity_id: str) -> list:
    """return the schedules whose actions will act on `entity_id`.

    Group entities are not expanded: a schedule targeting `light.group`
    matches `light.group`, not its members.
    """
    data = hass.data.get(const.DOMAIN)
    if not data or "coordinator" not in data:
        return []
    store = data["coordinator"].store
    entities = data.get("schedules", {})

    results = []
    for schedule_id, schedule in store.schedules.items():
        best = None
        for timeslot in schedule.timeslots or []:
            for action in timeslot.actions or []:
                target = action.target
                if not target:
                    continue
                if entity_id not in _as_list(target.get(ATTR_ENTITY_ID)):
                    # explicit entities always resolve; skip the registry
                    # walk unless the target has dynamic parts
                    if not any(target.get(key) for key in const.DYNAMIC_TARGET_KEYS):
                        continue
                    service = action.service or ""
                    domain = service.split(".")[0] if service else None
                    resolved = resolve_target(hass, target, domain, action.target_filter)
                    if entity_id not in resolved:
                        continue
                reason = _match_reason(hass, entity_id, target)
                if reason and _rank(reason) < _rank(best):
                    best = reason
            if best and best["type"] == MATCH_ENTITY:
                break
        if best is None:
            continue

        schedule_entity = entities.get(schedule_id)
        results.append(
            {
                const.ATTR_SCHEDULE_ID: schedule_id,
                "entity_id": schedule_entity.entity_id if schedule_entity else None,
                ATTR_NAME: schedule.name,
                const.ATTR_ENABLED: schedule.enabled,
                "next_trigger": _next_trigger(hass, schedule_id),
                "matched_via": best,
            }
        )

    results.sort(key=lambda x: ((x[ATTR_NAME] or "").lower(), x[const.ATTR_SCHEDULE_ID]))
    return results


@callback
def websocket_entity_schedules(hass, connection, msg):
    """one-shot: schedules acting on an entity"""
    connection.send_result(
        msg["id"], async_get_entity_schedules(hass, msg[ATTR_ENTITY_ID])
    )


def _relevant(event, tracked: set | None) -> bool:
    action = event.data.get("action")
    if action in ("create", "remove"):
        return True
    if action == "update":
        if tracked is None:
            return True
        return bool(set(event.data.get("changes", {})) & tracked)
    return False


@callback
def websocket_subscribe_entity_schedules(hass, connection, msg):
    """subscription: push the list now and whenever it may have changed"""
    entity_id = msg[ATTR_ENTITY_ID]
    unsubscribers = []
    pending = None

    @callback
    def push(_now=None):
        nonlocal pending
        pending = None
        connection.send_message(
            websocket_api.event_message(
                msg["id"],
                {"schedules": async_get_entity_schedules(hass, entity_id)},
            )
        )

    @callback
    def schedule_push(*_args):
        nonlocal pending
        if pending is None:
            pending = async_call_later(hass, SUBSCRIPTION_PUSH_DELAY, push)

    # schedule add / edit / rename / delete / toggle, timer changes
    # (next_trigger), storage reload
    for signal in [
        const.EVENT_ITEM_CREATED,
        const.EVENT_ITEM_UPDATED,
        const.EVENT_ITEM_REMOVED,
        const.EVENT_TIMER_UPDATED,
        const.EVENT_STARTED,
    ]:
        unsubscribers.append(async_dispatcher_connect(hass, signal, schedule_push))

    def registry_listener(tracked):
        @callback
        def listener(event):
            if _relevant(event, tracked):
                schedule_push()

        return listener

    for event_type, tracked in [
        (er.EVENT_ENTITY_REGISTRY_UPDATED, ENTITY_REGISTRY_CHANGES),
        (dr.EVENT_DEVICE_REGISTRY_UPDATED, DEVICE_REGISTRY_CHANGES),
        (ar.EVENT_AREA_REGISTRY_UPDATED, None),
        (fr.EVENT_FLOOR_REGISTRY_UPDATED, None),
        (lr.EVENT_LABEL_REGISTRY_UPDATED, None),
    ]:
        unsubscribers.append(hass.bus.async_listen(event_type, registry_listener(tracked)))

    @callback
    def unsubscribe():
        nonlocal pending
        while unsubscribers:
            unsubscribers.pop()()
        if pending is not None:
            pending()
            pending = None

    connection.subscriptions[msg["id"]] = unsubscribe
    connection.send_result(msg["id"])
    push()


@callback
def async_register_entity_schedule_websockets(hass: HomeAssistant):
    websocket_api.async_register_command(
        hass,
        WS_ENTITY_SCHEDULES,
        websocket_entity_schedules,
        websocket_api.BASE_COMMAND_MESSAGE_SCHEMA.extend(
            {
                vol.Required("type"): WS_ENTITY_SCHEDULES,
                vol.Required(ATTR_ENTITY_ID): cv.entity_id,
            }
        ),
    )
    websocket_api.async_register_command(
        hass,
        WS_SUBSCRIBE_ENTITY_SCHEDULES,
        websocket_subscribe_entity_schedules,
        websocket_api.BASE_COMMAND_MESSAGE_SCHEMA.extend(
            {
                vol.Required("type"): WS_SUBSCRIBE_ENTITY_SCHEDULES,
                vol.Required(ATTR_ENTITY_ID): cv.entity_id,
            }
        ),
    )


@callback
def async_setup_rename_listener(hass: HomeAssistant):
    """rewrite directly targeted entity IDs when an entity is renamed.

    Goes through the coordinator's edit path, so the change is persisted by
    the store and dispatched like any other edit. Returns a detach callable.
    """

    @callback
    def entity_registry_updated(event):
        if event.data.get("action") != "update":
            return
        old_id = event.data.get("old_entity_id")
        new_id = event.data.get(ATTR_ENTITY_ID)
        if not old_id or not new_id or old_id == new_id:
            return
        data = hass.data.get(const.DOMAIN)
        if not data or "coordinator" not in data:
            return
        coordinator = data["coordinator"]

        for schedule_id in list(coordinator.store.schedules):
            schedule = coordinator.store.async_get_schedule(schedule_id)
            changed = False
            for timeslot in schedule[const.ATTR_TIMESLOTS]:
                for action in timeslot.get(const.ATTR_ACTIONS) or []:
                    target = action.get(const.ATTR_TARGET) or {}
                    ids = _as_list(target.get(ATTR_ENTITY_ID))
                    if old_id in ids:
                        target[ATTR_ENTITY_ID] = [new_id if e == old_id else e for e in ids]
                        changed = True
            if changed:
                _LOGGER.info(
                    "Entity %s renamed to %s, updating schedule %s",
                    old_id,
                    new_id,
                    schedule_id,
                )
                changes = {const.ATTR_TIMESLOTS: schedule[const.ATTR_TIMESLOTS]}
                if schedule_id in data.get("schedules", {}):
                    coordinator.async_edit_schedule(schedule_id, changes)
                else:
                    # no switch entity yet (e.g. during startup): the edit
                    # path would drop the change, so persist it directly
                    coordinator.store.async_update_schedule(schedule_id, changes)

    return hass.bus.async_listen(er.EVENT_ENTITY_REGISTRY_UPDATED, entity_registry_updated)
