from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.app.services import smart_plug_manager as module
from backend.app.services.bambu_mqtt import PrinterState
from backend.app.services.smart_plug_manager import SmartPlugManager


@pytest.mark.asyncio
async def test_after_hours_reuses_auto_off_and_cancels_at_opening():
    manager = SmartPlugManager()
    plug = SimpleNamespace(id=1, printer_id=1, plug_type="tasmota", auto_off_pending=False)
    db = AsyncMock()
    db.scalars.return_value.all = lambda: [plug]
    state = PrinterState(connected=True, state="FINISH")
    closed = True
    task = MagicMock()

    def schedule(plug, printer_id, *, allowed):
        manager._pending_off[plug.id] = task

    with (
        patch.object(module.printer_manager, "get_status", return_value=state),
        patch.object(module.printer_manager, "get_drying_targets", return_value=None),
        patch.object(manager, "_schedule_off_per_mode", side_effect=schedule) as auto_off,
    ):
        await manager.check_after_hours_idle_off(db, schedule_closed=lambda: closed, busy_printers={1})
        auto_off.assert_not_called()
        await manager.check_after_hours_idle_off(db, schedule_closed=lambda: closed, busy_printers=set())
        await manager.check_after_hours_idle_off(db, schedule_closed=lambda: closed, busy_printers=set())
        auto_off.assert_called_once()
        assert auto_off.call_args.args == (plug, 1)
        allowed = auto_off.call_args.kwargs["allowed"]
        assert allowed()
        state.state = "PAUSE"
        assert not allowed()
        state.state = "FINISH"
        closed = False
        assert not allowed()
        await manager.check_after_hours_idle_off(db, schedule_closed=lambda: closed, busy_printers=set())
        task.cancel.assert_called_once()
        assert 1 not in manager._pending_off
