from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from backend.app.services import smart_plug_manager as module
from backend.app.services.bambu_mqtt import PrinterState
from backend.app.services.smart_plug_manager import SmartPlugManager


@pytest.mark.asyncio
async def test_after_hours_power_off_waits_for_each_plugs_delay():
    manager = SmartPlugManager()
    plugs = [
        SimpleNamespace(
            id=pid,
            printer_id=pid,
            name=f"plug{pid}",
            plug_type="tasmota",
            ha_entity_id=None,
            auto_off_pending=False,
            auto_off_persistent=True,
            off_delay_mode="time",
            off_delay_minutes=pid,
        )
        for pid in (1, 2, 3)
    ]
    db = AsyncMock()
    db.scalars.return_value.all = lambda: plugs
    states = {
        1: PrinterState(connected=True, state="FINISH"),
        2: PrinterState(connected=True, state="IDLE"),
        3: PrinterState(connected=True, state="PAUSE"),
    }
    service = AsyncMock()
    with (
        patch.object(module.printer_manager, "get_status", side_effect=states.get),
        patch.object(module.printer_manager, "get_drying_targets", return_value=None),
        patch.object(module.printer_manager, "mark_printer_offline", side_effect=lambda pid: states.pop(pid)),
        patch.object(manager, "get_service_for_plug", return_value=service),
        patch.object(module, "publish_plug_state"),
        patch.object(module, "run_linked_scripts_after_automatic_switch"),
        patch.object(module.time, "monotonic") as clock,
    ):
        clock.return_value = 0
        await manager.check_after_hours_idle_off(db, after_hours=True, busy_printers=set())
        clock.return_value = 60
        await manager.check_after_hours_idle_off(db, after_hours=True, busy_printers={1})
        service.turn_off.assert_not_awaited()

        clock.return_value = 120
        await manager.check_after_hours_idle_off(db, after_hours=True, busy_printers=set())
        service.turn_off.assert_awaited_once_with(plugs[1])

        await manager.check_after_hours_idle_off(db, after_hours=False, busy_printers=set())
        clock.return_value = 3600
        await manager.check_after_hours_idle_off(db, after_hours=True, busy_printers=set())
        assert service.turn_off.await_count == 1

        clock.return_value = 3660
        await manager.check_after_hours_idle_off(db, after_hours=True, busy_printers=set())
        assert [call.args[0].id for call in service.turn_off.await_args_list] == [2, 1]
