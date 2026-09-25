"""Regression checks for pre-existing behaviour (valid on main and on the branch)."""
from .conftest import action, add_schedule, fire_later


async def test_setup_and_crud_services(hass, hass_storage, scheduler, world):
    sid = await add_schedule(hass, scheduler, "Morning", [action({"entity_id": [world["lamp"]]})])
    assert hass.states.get("switch.schedule_morning") is not None

    await hass.services.async_call("switch", "turn_off", {"entity_id": "switch.schedule_morning"}, blocking=True)
    await hass.async_block_till_done()
    assert scheduler.store.schedules[sid].enabled is False
    await hass.services.async_call("switch", "turn_on", {"entity_id": "switch.schedule_morning"}, blocking=True)
    await hass.async_block_till_done()
    assert scheduler.store.schedules[sid].enabled is True

    await hass.services.async_call(
        "scheduler", "edit", {"entity_id": "switch.schedule_morning", "name": "Evening"}, blocking=True
    )
    await hass.async_block_till_done()
    assert scheduler.store.schedules[sid].name == "Evening"
    # the switch is re-created on rename; which entity_id it gets depends on
    # HA's deleted-entity restore, so look it up rather than assume
    switch_id = hass.data["scheduler"]["schedules"][sid].entity_id
    assert hass.states.get(switch_id) is not None

    await hass.services.async_call("scheduler", "remove", {"entity_id": switch_id}, blocking=True)
    await hass.async_block_till_done()
    assert sid not in scheduler.store.schedules

    fire_later(hass, 15)
    await hass.async_block_till_done()
    assert hass_storage["scheduler.storage"]["version"] == 5


async def test_existing_websockets(hass, hass_ws_client, scheduler, world):
    sid = await add_schedule(hass, scheduler, "Morning", [action({"area_id": [world["kitchen"].id]})])
    client = await hass_ws_client(hass)
    await client.send_json({"id": 1, "type": "scheduler"})
    assert len((await client.receive_json())["result"]) == 1
    await client.send_json({"id": 2, "type": "scheduler/item", "schedule_id": sid})
    assert (await client.receive_json())["result"]["name"] == "Morning"
    await client.send_json({"id": 3, "type": "scheduler/tags"})
    assert (await client.receive_json())["success"]
    await client.send_json({"id": 4, "type": "scheduler_updated"})
    assert (await client.receive_json())["success"]


async def test_resolve_target_semantics(hass, scheduler, world):
    from custom_components.scheduler.actions import resolve_target

    assert resolve_target(hass, {"area_id": [world["kitchen"].id]}, "light") == sorted(
        [world["lamp"], world["other"]]
    )
    assert resolve_target(
        hass, {"area_id": [world["kitchen"].id]}, "light", {"exclude": [world["lamp"]]}
    ) == [world["other"]]
