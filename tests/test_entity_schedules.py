"""Tests for scheduler/entity_schedules, its subscription, and rename rewrite."""
import asyncio

import pytest
from homeassistant.helpers import (
    area_registry as ar,
    device_registry as dr,
    entity_registry as er,
    label_registry as lr,
)

from custom_components.scheduler import const
from custom_components.scheduler.entity_schedules import async_get_entity_schedules

from .conftest import action, add_schedule, fire_later


def via(hass, entity_id):
    return {r["name"]: r["matched_via"] for r in async_get_entity_schedules(hass, entity_id)}


async def test_direct_entity(hass, scheduler, world):
    sid = await add_schedule(hass, scheduler, "Direct", [action({"entity_id": [world["lamp"]]})])
    result = async_get_entity_schedules(hass, world["lamp"])
    assert len(result) == 1
    item = result[0]
    assert item["schedule_id"] == sid
    assert item["name"] == "Direct"
    assert item["enabled"] is True
    assert item["entity_id"] == "switch.schedule_direct"
    assert "next_trigger" in item
    assert item["matched_via"] == {"type": "entity", "id": world["lamp"], "name": "lamp"}
    assert async_get_entity_schedules(hass, world["other"]) == []


async def test_device(hass, scheduler, world):
    await add_schedule(hass, scheduler, "Dev", [action({"device_id": [world["lamp_dev"].id]})])
    assert via(hass, world["lamp"])["Dev"] == {
        "type": "device", "id": world["lamp_dev"].id, "name": "Lamp device",
    }
    assert via(hass, world["other"]) == {}


async def test_area_via_device_area(hass, scheduler, world):
    await add_schedule(hass, scheduler, "Area", [action({"area_id": [world["kitchen"].id]})])
    for eid in (world["lamp"], world["other"]):
        assert via(hass, eid)["Area"] == {
            "type": "area", "id": world["kitchen"].id, "name": "Kitchen",
        }
    assert via(hass, world["solo"]) == {}


async def test_entity_area_override_excludes(hass, scheduler, world):
    """an entity's own area overrides its device's area, as in resolve_target"""
    er.async_get(hass).async_update_entity(world["other"], area_id=world["hall"].id)
    await add_schedule(hass, scheduler, "Area", [action({"area_id": [world["kitchen"].id]})])
    assert via(hass, world["other"]) == {}
    assert "Area" in via(hass, world["lamp"])


async def test_floor(hass, scheduler, world):
    await add_schedule(hass, scheduler, "Floor", [action({"floor_id": [world["floor"].floor_id]})])
    assert via(hass, world["lamp"])["Floor"] == {
        "type": "floor", "id": world["floor"].floor_id, "name": "Ground",
    }


async def test_label_on_entity(hass, scheduler, world):
    lbl = world["ent_lbl"]
    await add_schedule(hass, scheduler, "Lbl", [action({"label_id": [lbl.label_id]})])
    assert via(hass, world["lamp"])["Lbl"] == {
        "type": "label", "id": lbl.label_id, "name": "Entity label",
    }
    assert via(hass, world["other"]) == {}


async def test_label_on_device_and_area(hass, scheduler, world):
    await add_schedule(hass, scheduler, "DevLbl", [action({"label_id": [world["dev_lbl"].label_id]})])
    await add_schedule(hass, scheduler, "AreaLbl", [action({"label_id": [world["area_lbl"].label_id]})])
    lamp = via(hass, world["lamp"])
    assert lamp["DevLbl"]["type"] == "label"
    assert lamp["AreaLbl"] == {"type": "label", "id": world["area_lbl"].label_id, "name": "Area label"}
    assert set(via(hass, world["other"])) == {"AreaLbl"}


async def test_target_filter_exclusion(hass, scheduler, world):
    await add_schedule(
        hass, scheduler, "Filtered",
        [action({"area_id": [world["kitchen"].id]}, target_filter={"exclude": [world["lamp"]]})],
    )
    assert via(hass, world["lamp"]) == {}
    assert "Filtered" in via(hass, world["other"])


async def test_target_filter_does_not_apply_to_explicit(hass, scheduler, world):
    """matches execution: explicitly picked entities bypass the filter"""
    await add_schedule(
        hass, scheduler, "Explicit",
        [action({"entity_id": [world["lamp"]]}, target_filter={"exclude": ["light"]})],
    )
    assert via(hass, world["lamp"])["Explicit"]["type"] == "entity"


async def test_service_domain_filter(hass, scheduler, world):
    """area + light.turn_on must not match switch.fan, like execution"""
    await add_schedule(hass, scheduler, "Lights", [action({"area_id": [world["kitchen"].id]})])
    assert via(hass, world["fan"]) == {}
    await add_schedule(
        hass, scheduler, "Switches",
        [action({"area_id": [world["kitchen"].id]}, service="switch.turn_on")],
    )
    assert set(via(hass, world["fan"])) == {"Switches"}


async def test_precedence_within_action(hass, scheduler, world):
    await add_schedule(
        hass, scheduler, "Multi",
        [action({
            "label_id": [world["ent_lbl"].label_id],
            "floor_id": [world["floor"].floor_id],
            "area_id": [world["kitchen"].id],
            "device_id": [world["lamp_dev"].id],
        })],
    )
    assert via(hass, world["lamp"])["Multi"]["type"] == "device"


async def test_precedence_across_actions_and_timeslots(hass, scheduler, world):
    await add_schedule(
        hass, scheduler, "Multi",
        [action({"floor_id": [world["floor"].floor_id]}),
         action({"area_id": [world["kitchen"].id]}, service="light.turn_off")],
        [action({"entity_id": [world["lamp"]]}, service="light.turn_off")],
    )
    result = async_get_entity_schedules(hass, world["lamp"])
    assert len(result) == 1
    assert result[0]["matched_via"]["type"] == "entity"
    assert via(hass, world["other"])["Multi"]["type"] == "area"


async def test_group_members_not_expanded(hass, scheduler, world):
    hass.states.async_set(
        "light.all_lights", "off", {"entity_id": [world["lamp"], world["other"]]}
    )
    await add_schedule(hass, scheduler, "Group", [action({"entity_id": ["light.all_lights"]})])
    assert via(hass, world["lamp"]) == {}
    assert via(hass, "light.all_lights")["Group"]["type"] == "entity"


async def test_rename_rewrites_direct_target_and_persists(hass, hass_storage, scheduler, world):
    sid = await add_schedule(
        hass, scheduler, "Direct",
        [action({"entity_id": [world["lamp"], world["solo"]]})],
    )
    er.async_get(hass).async_update_entity(world["lamp"], new_entity_id="light.lamp_renamed")
    await hass.async_block_till_done()

    stored = scheduler.store.schedules[sid].timeslots[0].actions[0].target
    assert stored["entity_id"] == ["light.lamp_renamed", world["solo"]]
    assert via(hass, "light.lamp_renamed")["Direct"]["type"] == "entity"
    assert via(hass, world["lamp"]) == {}

    # persisted: the edit went through the store, so the full save (the same
    # one the shutdown handler performs) writes the new ID. HA's delayed
    # write checks the real loop clock, so it cannot be fast-forwarded here.
    await scheduler.store.async_save()
    saved = hass_storage["scheduler.storage"]["data"]["schedules"]
    saved_target = saved[0]["timeslots"][0]["actions"][0]["target"]
    assert "light.lamp_renamed" in saved_target["entity_id"]
    assert hass_storage["scheduler.storage"]["version"] == 5


async def test_rename_leaves_unrelated_schedules(hass, scheduler, world):
    sid = await add_schedule(hass, scheduler, "Area", [action({"area_id": [world["kitchen"].id]})])
    before = scheduler.store.schedules[sid]
    er.async_get(hass).async_update_entity(world["lamp"], new_entity_id="light.lamp_renamed")
    await hass.async_block_till_done()
    assert scheduler.store.schedules[sid] is before


async def test_existing_commands_unchanged(hass, hass_ws_client, scheduler, world):
    await add_schedule(hass, scheduler, "Direct", [action({"entity_id": [world["lamp"]]})])
    client = await hass_ws_client(hass)
    await client.send_json({"id": 1, "type": "scheduler"})
    msg = await client.receive_json()
    assert msg["success"] and len(msg["result"]) == 1
    await client.send_json(
        {"id": 2, "type": "scheduler/resolve_target", "target": {"area_id": [world["kitchen"].id]}, "domain": "light"}
    )
    msg = await client.receive_json()
    assert msg["result"]["entities"] == sorted([world["lamp"], world["other"]])


async def test_websocket_one_shot(hass, hass_ws_client, scheduler, world):
    await add_schedule(hass, scheduler, "Dev", [action({"device_id": [world["lamp_dev"].id]})])
    client = await hass_ws_client(hass)
    await client.send_json({"id": 1, "type": "scheduler/entity_schedules", "entity_id": world["lamp"]})
    msg = await client.receive_json()
    assert msg["success"]
    assert [r["name"] for r in msg["result"]] == ["Dev"]
    assert msg["result"][0]["matched_via"]["type"] == "device"

    await client.send_json({"id": 2, "type": "scheduler/entity_schedules", "entity_id": "not valid"})
    msg = await client.receive_json()
    assert not msg["success"]


class Sub:
    def __init__(self, hass, client):
        self.hass = hass
        self.client = client

    async def next(self):
        fire_later(self.hass, 1)
        await self.hass.async_block_till_done()
        msg = await asyncio.wait_for(self.client.receive_json(), 2)
        assert msg["type"] == "event"
        return msg["event"]["schedules"]

    async def drain(self):
        """collect pushes until quiet, return the latest list"""
        latest = await self.next()
        while True:
            fire_later(self.hass, 1)
            await self.hass.async_block_till_done()
            try:
                msg = await asyncio.wait_for(self.client.receive_json(), 0.3)
            except asyncio.TimeoutError:
                return latest
            latest = msg["event"]["schedules"]


async def subscribe(hass, hass_ws_client, entity_id):
    client = await hass_ws_client(hass)
    await client.send_json({"id": 5, "type": "scheduler/subscribe_entity_schedules", "entity_id": entity_id})
    msg = await client.receive_json()
    assert msg["success"]
    first = await asyncio.wait_for(client.receive_json(), 2)
    return Sub(hass, client), first["event"]["schedules"]


async def test_subscription_schedule_lifecycle(hass, hass_ws_client, scheduler, world):
    sub, initial = await subscribe(hass, hass_ws_client, world["lamp"])
    assert initial == []

    # add
    sid = await add_schedule(hass, scheduler, "Dev", [action({"device_id": [world["lamp_dev"].id]})])
    assert [r["schedule_id"] for r in await sub.drain()] == [sid]

    # toggle via the existing switch service
    switch_id = hass.data["scheduler"]["schedules"][sid].entity_id
    await hass.services.async_call("switch", "turn_off", {"entity_id": switch_id}, blocking=True)
    assert (await sub.drain())[0]["enabled"] is False
    await hass.services.async_call("switch", "turn_on", {"entity_id": switch_id}, blocking=True)
    assert (await sub.drain())[0]["enabled"] is True

    # rename
    scheduler.async_edit_schedule(sid, {"name": "Renamed"})
    await hass.async_block_till_done()
    latest = await sub.drain()
    assert latest[0]["name"] == "Renamed"
    assert latest[0]["entity_id"] == hass.data["scheduler"]["schedules"][sid].entity_id

    # edit so the entity no longer matches
    scheduler.async_edit_schedule(
        sid,
        const.EDIT_SCHEDULE_SCHEMA({"timeslots": [
            {"start": "08:00:00", "stop": "08:30:00", "actions": [action({"entity_id": [world["other"]]})]}
        ]}),
    )
    await hass.async_block_till_done()
    assert await sub.drain() == []

    # delete
    sid2 = await add_schedule(hass, scheduler, "Again", [action({"entity_id": [world["lamp"]]})])
    assert [r["schedule_id"] for r in await sub.drain()] == [sid2]
    scheduler.async_delete_schedule(sid2)
    await hass.async_block_till_done()
    assert await sub.drain() == []


async def test_subscription_registry_changes(hass, hass_ws_client, scheduler, world):
    await add_schedule(hass, scheduler, "Hall", [action({"area_id": [world["hall"].id]})])
    await add_schedule(hass, scheduler, "Lbl", [action({"label_id": [world["dev_lbl"].label_id]})])
    sub, initial = await subscribe(hass, hass_ws_client, world["other"])
    assert initial == []

    # device registry: move other_dev into the hall
    dr.async_get(hass).async_update_device(world["other_dev"].id, area_id=world["hall"].id)
    assert [r["name"] for r in await sub.drain()] == ["Hall"]

    # area registry: rename changes matched_via.name
    ar.async_get(hass).async_update(world["hall"].id, name="Hallway")
    assert (await sub.drain())[0]["matched_via"]["name"] == "Hallway"

    # entity registry: give the entity its own area override
    er.async_get(hass).async_update_entity(world["other"], area_id=world["kitchen"].id)
    assert await sub.drain() == []

    # device labels
    dr.async_get(hass).async_update_device(world["other_dev"].id, labels={world["dev_lbl"].label_id})
    assert [r["name"] for r in await sub.drain()] == ["Lbl"]

    # label registry: rename label
    lr.async_get(hass).async_update(world["dev_lbl"].label_id, name="Night")
    assert (await sub.drain())[0]["matched_via"]["name"] == "Night"


async def test_unsubscribe_cleans_up(hass, hass_ws_client, scheduler, world):
    sub, _ = await subscribe(hass, hass_ws_client, world["lamp"])
    listeners_before = sum(hass.bus.async_listeners().values())

    await sub.client.send_json({"id": 6, "type": "unsubscribe_events", "subscription": 5})
    msg = await sub.client.receive_json()
    assert msg["success"]
    assert sum(hass.bus.async_listeners().values()) == listeners_before - 5

    await add_schedule(hass, scheduler, "Direct", [action({"entity_id": [world["lamp"]]})])
    fire_later(hass, 1)
    await hass.async_block_till_done()
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(sub.client.receive_json(), 0.3)
