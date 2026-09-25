"""Shared fixtures for scheduler tests (pytest-homeassistant-custom-component)."""
from datetime import timedelta

import pytest
from homeassistant.helpers import (
    area_registry as ar,
    device_registry as dr,
    entity_registry as er,
    floor_registry as fr,
    label_registry as lr,
)
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.scheduler import const

pytest_plugins = "pytest_homeassistant_custom_component"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


@pytest.fixture
async def scheduler(hass):
    """set up the scheduler integration, return its coordinator"""
    entry = MockConfigEntry(domain=const.DOMAIN, unique_id="test", version=2, data={})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    # let the coordinator's 10s startup timer fire (-> STATE_READY); it is
    # never cancelled on unload and would trip the lingering-timer check
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=11))
    await hass.async_block_till_done()
    yield hass.data[const.DOMAIN]["coordinator"]
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


@pytest.fixture
def world(hass):
    """a small home:

    floor ground -> area kitchen (label area_lbl)
    device lamp_dev in kitchen (label dev_lbl) -> light.lamp (label ent_lbl)
    device other_dev in kitchen -> light.other, switch.fan
    light.solo: no device, no area
    """
    source = MockConfigEntry(domain="test")
    source.add_to_hass(hass)

    floors = fr.async_get(hass)
    labels = lr.async_get(hass)
    areas = ar.async_get(hass)
    devices = dr.async_get(hass)
    entities = er.async_get(hass)

    ground = floors.async_create("Ground")
    area_lbl = labels.async_create("Area label")
    dev_lbl = labels.async_create("Device label")
    ent_lbl = labels.async_create("Entity label")
    kitchen = areas.async_create("Kitchen", floor_id=ground.floor_id, labels={area_lbl.label_id})
    hall = areas.async_create("Hall")

    lamp_dev = devices.async_get_or_create(
        config_entry_id=source.entry_id, identifiers={("test", "lamp")}, name="Lamp device"
    )
    lamp_dev = devices.async_update_device(lamp_dev.id, area_id=kitchen.id, labels={dev_lbl.label_id})
    other_dev = devices.async_get_or_create(
        config_entry_id=source.entry_id, identifiers={("test", "other")}, name="Other device"
    )
    other_dev = devices.async_update_device(other_dev.id, area_id=kitchen.id)

    lamp = entities.async_get_or_create(
        "light", "test", "lamp", device_id=lamp_dev.id, suggested_object_id="lamp",
        config_entry=source,
    )
    lamp = entities.async_update_entity(lamp.entity_id, labels={ent_lbl.label_id})
    other = entities.async_get_or_create(
        "light", "test", "other", device_id=other_dev.id, suggested_object_id="other",
        config_entry=source,
    )
    fan = entities.async_get_or_create(
        "switch", "test", "fan", device_id=other_dev.id, suggested_object_id="fan",
        config_entry=source,
    )
    solo = entities.async_get_or_create("light", "test", "solo", suggested_object_id="solo")

    for e in [lamp, other, solo]:
        hass.states.async_set(e.entity_id, "off")
    hass.states.async_set(fan.entity_id, "off")

    return {
        "floor": ground, "kitchen": kitchen, "hall": hall,
        "area_lbl": area_lbl, "dev_lbl": dev_lbl, "ent_lbl": ent_lbl,
        "lamp_dev": lamp_dev, "other_dev": other_dev,
        "lamp": lamp.entity_id, "other": other.entity_id, "fan": fan.entity_id,
        "solo": solo.entity_id,
    }


def action(target, service="light.turn_on", target_filter=None):
    data = {"service": service, "target": target}
    if target_filter:
        data["target_filter"] = target_filter
    return data


def make_schedule(name, *actions_per_slot):
    """build a validated add-schedule payload; one timeslot per actions list"""
    slots = []
    for i, actions in enumerate(actions_per_slot or [[]]):
        slots.append({"start": f"{8 + i:02d}:00:00", "stop": f"{8 + i:02d}:30:00", "actions": actions})
    return const.ADD_SCHEDULE_SCHEMA(
        {"name": name, "repeat_type": "repeat", "timeslots": slots}
    )


async def add_schedule(hass, coordinator, name, *actions_per_slot):
    coordinator.async_create_schedule(make_schedule(name, *actions_per_slot))
    await hass.async_block_till_done()
    for schedule_id, entry in coordinator.store.schedules.items():
        if entry.name == name:
            return schedule_id
    raise AssertionError("schedule not created")


def fire_later(hass, seconds=1):
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=seconds))
