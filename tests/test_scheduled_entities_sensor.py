"""Tests for sensor.scheduled_entities."""
from homeassistant.const import EVENT_STATE_CHANGED
from homeassistant.helpers import area_registry as ar, device_registry as dr

from custom_components.scheduler.entity_schedules import async_get_entity_schedules
from custom_components.scheduler.sensor import ScheduledEntitiesSensor

from .conftest import action, add_schedule, fire_later

SENSOR = "sensor.scheduled_entities"


async def settle(hass):
    fire_later(hass, 1)
    await hass.async_block_till_done()


def entities(hass):
    return hass.states.get(SENSOR).attributes["entities"]


async def test_sensor_exists_empty(hass, scheduler, world):
    state = hass.states.get(SENSOR)
    assert state is not None
    assert state.state == "0"
    assert state.attributes["friendly_name"] == "Scheduled entities"
    assert state.attributes["entities"] == {}


async def test_counts_and_enabled(hass, scheduler, world):
    await add_schedule(hass, scheduler, "Area", [action({"area_id": [world["kitchen"].id]})])
    sid = await add_schedule(hass, scheduler, "Direct", [action({"entity_id": [world["lamp"]]})])
    await settle(hass)

    assert hass.states.get(SENSOR).state == "2"
    assert entities(hass) == {
        world["lamp"]: {"schedules": 2, "enabled": 2},
        world["other"]: {"schedules": 1, "enabled": 1},
    }

    # toggle through the existing switch service
    switch_id = hass.data["scheduler"]["schedules"][sid].entity_id
    await hass.services.async_call("switch", "turn_off", {"entity_id": switch_id}, blocking=True)
    await settle(hass)
    assert entities(hass)[world["lamp"]] == {"schedules": 2, "enabled": 1}

    scheduler.async_delete_schedule(sid)
    await settle(hass)
    assert entities(hass)[world["lamp"]] == {"schedules": 1, "enabled": 1}


async def test_resolution_rules_match_execution(hass, scheduler, world):
    """service domain, target_filter and groups follow resolve_target()"""
    await add_schedule(
        hass, scheduler, "Filtered",
        [action({"area_id": [world["kitchen"].id]}, target_filter={"exclude": [world["lamp"]]})],
    )
    hass.states.async_set("light.all_lights", "off", {"entity_id": [world["lamp"]]})
    await add_schedule(hass, scheduler, "Group", [action({"entity_id": ["light.all_lights"]})])
    await settle(hass)
    assert set(entities(hass)) == {world["other"], "light.all_lights"}
    assert world["fan"] not in entities(hass)  # light.turn_on, switch domain


async def test_agrees_with_websocket_lookup(hass, scheduler, world):
    await add_schedule(hass, scheduler, "Floor", [action({"floor_id": [world["floor"].floor_id]})])
    await add_schedule(hass, scheduler, "Lbl", [action({"label_id": [world["dev_lbl"].label_id]})])
    await add_schedule(
        hass, scheduler, "Mixed",
        [action({"entity_id": [world["solo"]]}), action({"device_id": [world["other_dev"].id]}, service="switch.turn_on")],
    )
    await settle(hass)
    sensor = entities(hass)
    for eid in [world["lamp"], world["other"], world["fan"], world["solo"]]:
        per_entity = async_get_entity_schedules(hass, eid)
        if per_entity:
            assert sensor[eid]["schedules"] == len(per_entity), eid
            assert sensor[eid]["enabled"] == sum(1 for x in per_entity if x["enabled"]), eid
        else:
            assert eid not in sensor, eid


async def test_registry_change_updates(hass, scheduler, world):
    await add_schedule(hass, scheduler, "Hall", [action({"area_id": [world["hall"].id]})])
    await settle(hass)
    assert entities(hass) == {}

    dr.async_get(hass).async_update_device(world["other_dev"].id, area_id=world["hall"].id)
    await settle(hass)
    assert set(entities(hass)) == {world["other"]}


async def test_writes_only_when_changed(hass, scheduler, world):
    await add_schedule(hass, scheduler, "Area", [action({"area_id": [world["kitchen"].id]})])
    await settle(hass)

    writes = []
    hass.bus.async_listen(
        EVENT_STATE_CHANGED,
        lambda e: writes.append(e) if e.data["entity_id"] == SENSOR else None,
    )
    # an area rename changes no membership -> no state write
    ar.async_get(hass).async_update(world["kitchen"].id, name="Cuisine")
    await settle(hass)
    assert writes == []


async def test_entities_attribute_not_recorded(hass, scheduler, world):
    assert "entities" in ScheduledEntitiesSensor._unrecorded_attributes
    entity = next(
        e for e in hass.data["entity_components"]["sensor"].entities if e.entity_id == SENSOR
    )
    assert "entities" in entity._state_info["unrecorded_attributes"]
