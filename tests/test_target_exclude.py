"""Tests for target exclusions (HA 2026.11 "Exclude target") in schedules."""
import pytest
import voluptuous as vol
from homeassistant.helpers import (
    area_registry as ar,
    device_registry as dr,
    entity_registry as er,
)

from custom_components.scheduler import const
from custom_components.scheduler.actions import expand_action_target, resolve_target
from custom_components.scheduler.entity_schedules import async_get_entity_schedules
from custom_components.scheduler.store import normalize_action_target

from .conftest import action, add_schedule, make_schedule


def via(hass, entity_id):
    return {r["name"]: r["matched_via"] for r in async_get_entity_schedules(hass, entity_id)}


# --- resolution -----------------------------------------------------------


async def test_exclude_entity_from_area(hass, scheduler, world):
    target = {"area_id": [world["kitchen"].id], "exclude": {"entity_id": [world["lamp"]]}}
    assert resolve_target(hass, target, "light") == [world["other"]]


async def test_exclude_device_area_floor_label(hass, scheduler, world):
    kitchen = world["kitchen"].id
    # device: takes out both of other_dev's entities, whatever their domain
    assert resolve_target(
        hass, {"area_id": [kitchen], "exclude": {"device_id": [world["other_dev"].id]}}, "homeassistant"
    ) == [world["lamp"]]
    # area excluded from its own floor: nothing left
    assert resolve_target(
        hass, {"floor_id": [world["floor"].floor_id], "exclude": {"area_id": [kitchen]}}, "light"
    ) == []
    # label on an entity
    assert resolve_target(
        hass, {"area_id": [kitchen], "exclude": {"label_id": [world["ent_lbl"].label_id]}}, "light"
    ) == [world["other"]]
    # floor excludes everything on it
    assert resolve_target(
        hass, {"entity_id": [world["solo"]], "area_id": [kitchen],
               "exclude": {"floor_id": [world["floor"].floor_id]}}, "light"
    ) == [world["solo"]]


async def test_exclude_removes_explicit_entities(hass, scheduler, world):
    """as in HA, an explicitly listed entity is still taken out by an
    exclusion that covers it (here: through its device's area)"""
    target = {"entity_id": [world["lamp"], world["solo"]], "exclude": {"area_id": [world["kitchen"].id]}}
    assert resolve_target(hass, target, "light") == [world["solo"]]


async def test_exclude_without_match_has_no_effect(hass, scheduler, world):
    base = {"area_id": [world["kitchen"].id]}
    target = {**base, "exclude": {"area_id": [world["hall"].id], "entity_id": ["light.does_not_exist"]}}
    assert resolve_target(hass, target, "light") == resolve_target(hass, base, "light")


async def test_exclude_is_evaluated_at_run_time(hass, scheduler, world):
    """exclusions stay as smart as targets: entities that join an excluded
    area later are left out too, without editing the schedule"""
    entities = er.async_get(hass)
    target = {"floor_id": [world["floor"].floor_id], "exclude": {"area_id": [world["kitchen"].id]}}
    den = ar.async_get(hass).async_create("Den", floor_id=world["floor"].floor_id)
    entities.async_update_entity(world["solo"], area_id=den.id)
    assert resolve_target(hass, target, "light") == [world["solo"]]

    entities.async_update_entity(world["solo"], area_id=world["kitchen"].id)
    assert resolve_target(hass, target, "light") == []


async def test_exclude_ignores_card_target_filter(hass, scheduler, world):
    """the card's include/exclude patterns only limit what a target may add;
    they don't stop an exclusion from taking entities out"""
    target = {"area_id": [world["kitchen"].id], "exclude": {"entity_id": [world["lamp"]]}}
    assert resolve_target(hass, target, "light", {"include": ["light.*"]}) == [world["other"]]


async def test_execution_expands_without_excluded_entities(hass, scheduler, world):
    expanded = expand_action_target(
        hass,
        {"service": "light.turn_on", "service_data": {},
         "target": {"area_id": [world["kitchen"].id], "exclude": {"entity_id": [world["lamp"]]}}},
    )
    assert [a["entity_id"] for a in expanded] == [world["other"]]


# --- validation and storage -------------------------------------------------


async def test_schema_accepts_exclude_rejects_nested(hass, scheduler, world):
    payload = make_schedule("Ok", [action({"area_id": world["kitchen"].id, "exclude": {"entity_id": world["lamp"]}})])
    target = payload["timeslots"][0]["actions"][0]["target"]
    assert target["exclude"] == {"entity_id": [world["lamp"]]}

    with pytest.raises(vol.Invalid):
        make_schedule("Nested", [action({
            "area_id": [world["kitchen"].id],
            "exclude": {"entity_id": [world["lamp"]], "exclude": {"entity_id": [world["other"]]}},
        })])


async def test_normalize_keeps_exclude_only_with_a_target(hass):
    normalized = normalize_action_target({
        "service": "light.turn_on",
        "target": {"area_id": "b", "exclude": {"entity_id": "light.z", "area_id": ["y", "x", "x"], "label_id": []}},
    })
    assert normalized["target"] == {
        "area_id": ["b"],
        "exclude": {"entity_id": ["light.z"], "area_id": ["x", "y"]},
    }
    # an exclude on its own is not a target, as in HA
    assert normalize_action_target(
        {"service": "light.turn_on", "target": {"exclude": {"entity_id": ["light.z"]}}}
    )["target"] is None
    # an empty exclude is dropped
    assert normalize_action_target(
        {"service": "light.turn_on", "target": {"area_id": ["b"], "exclude": {}}}
    )["target"] == {"area_id": ["b"]}


async def test_exclude_is_stored_and_survives_save(hass, hass_storage, scheduler, world):
    sid = await add_schedule(hass, scheduler, "Except lamp", [action(
        {"area_id": [world["kitchen"].id], "exclude": {"entity_id": [world["lamp"]]}}
    )])
    stored = scheduler.store.schedules[sid].timeslots[0].actions[0].target
    assert stored["exclude"] == {"entity_id": [world["lamp"]]}

    await scheduler.store.async_save()
    saved = hass_storage["scheduler.storage"]["data"]["schedules"][0]
    assert saved["timeslots"][0]["actions"][0]["target"]["exclude"] == {"entity_id": [world["lamp"]]}
    assert hass_storage["scheduler.storage"]["version"] == 5


async def test_card_paths_preview_add_and_edit(hass, hass_ws_client, hass_client, scheduler, world):
    """the card's own paths: preview through the scheduler/resolve_target
    websocket command, saving through POST /api/scheduler/add and /edit"""
    ws = await hass_ws_client(hass)
    target = {"area_id": [world["kitchen"].id], "exclude": {"entity_id": [world["lamp"]]}}

    await ws.send_json({"id": 1, "type": "scheduler/resolve_target", "target": target, "domain": "light"})
    response = await ws.receive_json()
    assert response["success"], response
    assert response["result"]["entities"] == [world["other"]]

    http = await hass_client()
    schedule = {
        "name": "Via card", "repeat_type": "repeat", "weekdays": ["daily"],
        "timeslots": [{"start": "08:00:00", "actions": [{"service": "light.turn_on", "target": target}]}],
    }
    response = await http.post("/api/scheduler/add", json=schedule)
    assert response.status == 200, await response.text()
    await hass.async_block_till_done()
    sid, stored = next((k, s) for k, s in scheduler.store.schedules.items() if s.name == "Via card")
    assert stored.timeslots[0].actions[0].target["exclude"] == {"entity_id": [world["lamp"]]}

    # edit: exclude the other light instead
    schedule["timeslots"][0]["actions"][0]["target"]["exclude"] = {"entity_id": [world["other"]]}
    response = await http.post("/api/scheduler/edit", json={**schedule, "schedule_id": sid})
    assert response.status == 200, await response.text()
    await hass.async_block_till_done()
    edited = scheduler.store.schedules[sid].timeslots[0].actions[0].target
    assert edited["exclude"] == {"entity_id": [world["other"]]}
    assert resolve_target(hass, edited, "light") == [world["lamp"]]


# --- entity -> schedules lookup ---------------------------------------------


async def test_entity_schedules_respect_exclude(hass, scheduler, world):
    await add_schedule(hass, scheduler, "Kitchen but lamp", [action(
        {"area_id": [world["kitchen"].id], "exclude": {"entity_id": [world["lamp"]]}}
    )])
    await add_schedule(hass, scheduler, "Listed then excluded", [action(
        {"entity_id": [world["lamp"]], "exclude": {"device_id": [world["lamp_dev"].id]}}
    )])
    assert via(hass, world["lamp"]) == {}
    assert via(hass, world["other"])["Kitchen but lamp"]["type"] == "area"


async def test_rename_rewrites_exclude_block(hass, scheduler, world):
    sid = await add_schedule(hass, scheduler, "Except lamp", [action(
        {"area_id": [world["kitchen"].id], "exclude": {"entity_id": [world["lamp"]]}}
    )])
    er.async_get(hass).async_update_entity(world["lamp"], new_entity_id="light.lamp_renamed")
    await hass.async_block_till_done()

    stored = scheduler.store.schedules[sid].timeslots[0].actions[0].target
    assert stored["exclude"] == {"entity_id": ["light.lamp_renamed"]}
    assert resolve_target(hass, stored, "light") == [world["other"]]
