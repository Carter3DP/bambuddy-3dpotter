"""One queue order for pinned and "Any <model>" jobs (#3200).

Three P2S printers. The user dragged a job pinned to printer 1 above two
"Any P2S" jobs, printer 1 finished, and it started one of the "Any" jobs from
further down the queue. The pinned job sat at the top saying "Busy".

The scheduler read the queue ``ORDER BY printer_id, position``, which put the
lane ahead of the position. A model-based job has ``printer_id`` NULL, which
SQLite sorts first and PostgreSQL sorts last, so which job won a printer both
wanted was decided by the database, never by where the user put it.

These run on SQLite, where the "Any" jobs used to win.
"""

from contextlib import ExitStack
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import backend.app.models  # noqa: F401 - populate Base.metadata
from backend.app.core.database import Base
from backend.app.models.library import LibraryFile
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.app.models.settings import Settings
from backend.app.services.print_scheduler import PrintScheduler


@pytest.fixture
async def ctx():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_maker = async_sessionmaker(engine, expire_on_commit=False)

    async with session_maker() as db:
        for pid in (1, 2, 3):
            db.add(
                Printer(
                    id=pid,
                    name=f"3D printer 0{pid}",
                    serial_number=f"P2S000{pid}",
                    ip_address=f"10.0.0.{pid}",
                    access_code="x",
                    model="P2S",
                    is_active=True,
                )
            )
        await db.commit()

    try:
        yield SimpleNamespace(session_maker=session_maker)
    finally:
        await engine.dispose()


async def _add(ctx, *, position, printer_id=None, target_model=None, print_time=None, manual_start=False):
    async with ctx.session_maker() as db:
        lib = LibraryFile(
            filename="job.gcode.3mf",
            file_path="/library/job.gcode.3mf",
            file_size=10,
            file_type="gcode.3mf",
            file_metadata={"sliced_for_model": "P2S"},
        )
        db.add(lib)
        await db.flush()
        item = PrintQueueItem(
            status="pending",
            position=position,
            printer_id=printer_id,
            target_model=target_model,
            library_file_id=lib.id,
            print_time_seconds=print_time,
            manual_start=manual_start,
        )
        db.add(item)
        await db.commit()
        return item.id


async def _pinned(ctx, position, printer_id=1, **kw):
    return await _add(ctx, position=position, printer_id=printer_id, **kw)


async def _any(ctx, position, **kw):
    return await _add(ctx, position=position, target_model="P2S", **kw)


async def _set(ctx, key, value):
    async with ctx.session_maker() as db:
        db.add(Settings(key=key, value=value))
        await db.commit()


async def _item(ctx, item_id):
    async with ctx.session_maker() as db:
        return (await db.execute(select(PrintQueueItem).where(PrintQueueItem.id == item_id))).scalar_one()


async def _run(ctx, *, idle_printers, now=None):
    """One check_queue pass; returns {item_id: printer_id} for what went out."""
    scheduler = PrintScheduler()
    launched = MagicMock()
    patches = [
        patch("backend.app.services.print_scheduler.async_session", ctx.session_maker),
        patch("backend.app.core.database.async_session", ctx.session_maker),
        patch("backend.app.services.print_scheduler.printer_manager.is_connected", MagicMock(return_value=True)),
        patch("backend.app.services.print_scheduler.printer_manager.get_status", MagicMock(return_value=None)),
        patch(
            "backend.app.services.print_scheduler.printer_manager.is_awaiting_plate_clear",
            MagicMock(return_value=False),
        ),
        patch(
            "backend.app.services.print_scheduler.ha_sensor_manager.blocked_printers",
            AsyncMock(return_value={}),
        ),
        patch(
            "backend.app.services.notification_service.notification_service.on_queue_job_waiting",
            AsyncMock(),
        ),
        patch(
            "backend.app.services.notification_service.notification_service.on_queue_job_assigned",
            AsyncMock(),
        ),
        patch.object(
            scheduler,
            "_is_printer_idle",
            MagicMock(side_effect=lambda pid, *_a, **_k: pid in idle_printers),
        ),
        patch.object(scheduler, "_check_auto_drying", AsyncMock()),
        patch.object(scheduler, "_ensure_ams_mapping", AsyncMock(return_value=None)),
        patch.object(scheduler, "_block_on_filament_deficit", AsyncMock(return_value=False)),
        patch.object(scheduler, "_get_smart_plugs", AsyncMock(return_value=[])),
        patch.object(scheduler, "_launch_uploads", launched),
    ]
    with ExitStack() as stack:
        if now is not None:
            clock = stack.enter_context(patch("backend.app.services.print_scheduler.datetime", wraps=datetime))
            clock.now.return_value = now
        for p in patches:
            stack.enter_context(p)
        await scheduler.check_queue()

    if not launched.called:
        return {}
    dispatched = {}
    for item_id in launched.call_args[0][0]:
        dispatched[item_id] = (await _item(ctx, item_id)).printer_id
    return dispatched


class TestPositionDecidesWhoGetsThePrinter:
    @pytest.mark.asyncio
    async def test_the_reporters_queue(self, ctx):
        """Pinned job dragged to the top, two "Any P2S" jobs below it, only
        printer 1 free: the pinned job starts."""
        shuttle_a = await _any(ctx, 2)
        shuttle_b = await _any(ctx, 3)
        coral = await _pinned(ctx, 1)

        assert await _run(ctx, idle_printers={1}) == {coral: 1}
        assert (await _item(ctx, shuttle_a)).status == "pending"
        assert (await _item(ctx, shuttle_b)).status == "pending"

    @pytest.mark.asyncio
    async def test_an_any_job_above_a_pinned_one_goes_first(self, ctx):
        """The other direction: position still decides, so a model-based job
        the user put higher takes the printer."""
        pinned = await _pinned(ctx, 2)
        any_job = await _any(ctx, 1)

        assert await _run(ctx, idle_printers={1}) == {any_job: 1}
        assert (await _item(ctx, pinned)).status == "pending"

    @pytest.mark.asyncio
    async def test_a_pinned_job_that_cannot_start_does_not_hold_up_other_printers(self, ctx):
        """The top job is pinned to a busy printer; the "Any" job below it
        still takes the free one."""
        await _pinned(ctx, 1, printer_id=2)
        any_job = await _any(ctx, 2)

        assert await _run(ctx, idle_printers={1}) == {any_job: 1}

    @pytest.mark.asyncio
    async def test_several_free_printers_each_get_the_next_job(self, ctx):
        coral = await _pinned(ctx, 1)
        shuttle_a = await _any(ctx, 2)
        shuttle_b = await _any(ctx, 3)

        dispatched = await _run(ctx, idle_printers={1, 2, 3})

        assert dispatched[coral] == 1
        assert {dispatched[shuttle_a], dispatched[shuttle_b]} == {2, 3}

    @pytest.mark.asyncio
    async def test_a_staged_job_at_the_top_does_not_block_the_printer(self, ctx):
        """A manual-start job waits for the user, so the next job takes the
        printer rather than the queue stalling behind it."""
        await _pinned(ctx, 1, manual_start=True)
        any_job = await _any(ctx, 2)

        assert await _run(ctx, idle_printers={1}) == {any_job: 1}


class TestShortestJobFirstAcrossLanes:
    @pytest.mark.asyncio
    async def test_a_shorter_any_job_beats_a_longer_pinned_one(self, ctx):
        await _set(ctx, "queue_shortest_first", "true")
        pinned = await _pinned(ctx, 1, print_time=7200)
        short_any = await _any(ctx, 2, print_time=600)

        assert await _run(ctx, idle_printers={1}) == {short_any: 1}
        assert (await _item(ctx, pinned)).status == "pending"

    @pytest.mark.asyncio
    async def test_the_pinned_job_it_jumped_is_marked_and_goes_next(self, ctx):
        """The starvation guard has to look across lanes, or a stream of short
        "Any" jobs holds the pinned one back forever."""
        await _set(ctx, "queue_shortest_first", "true")
        pinned = await _pinned(ctx, 1, print_time=7200)
        await _any(ctx, 2, print_time=600)

        await _run(ctx, idle_printers={1})
        assert (await _item(ctx, pinned)).been_jumped is True

        await _any(ctx, 3, print_time=300)
        async with ctx.session_maker() as db:
            for row in (await db.execute(select(PrintQueueItem).where(PrintQueueItem.status != "pending"))).scalars():
                row.status = "completed"
            await db.commit()

        assert await _run(ctx, idle_printers={1}) == {pinned: 1}

    @pytest.mark.asyncio
    async def test_a_jumped_any_job_is_marked_when_a_pinned_job_goes_first(self, ctx):
        await _set(ctx, "queue_shortest_first", "true")
        long_any = await _any(ctx, 1, print_time=7200)
        short_pinned = await _pinned(ctx, 2, print_time=600)

        assert await _run(ctx, idle_printers={1}) == {short_pinned: 1}
        assert (await _item(ctx, long_any)).been_jumped is True

    @pytest.mark.asyncio
    async def test_an_any_job_for_another_model_is_not_marked(self, ctx):
        await _set(ctx, "queue_shortest_first", "true")
        other_model = await _add(ctx, position=1, target_model="X1C", print_time=7200)
        await _pinned(ctx, 2, print_time=600)

        await _run(ctx, idle_printers={1})
        assert (await _item(ctx, other_model)).been_jumped is False


@pytest.mark.parametrize(
    ("hour", "later_time", "manual_start", "expected_position"),
    [
        (8, 5 * 3600, False, 1),  # before opening
        (9, 5 * 3600, False, 2),  # fits before closing
        (9, 8 * 3600, False, 1),  # neither job fits
        (9, 5 * 3600, True, 1),  # fitting job is staged
        (9, None, False, 1),  # unknown duration
        (9, 0, False, 1),  # zero is not a usable estimate
        (17, 5 * 3600, False, 1),  # after closing
    ],
)
async def test_working_hours_dispatch(ctx, monkeypatch, hour, later_time, manual_start, expected_position):
    monkeypatch.setenv("TZ", "UTC")
    await _set(ctx, "queue_working_hours", '{"enabled":true,"start":"08:30","end":"16:30"}')
    first = await _pinned(ctx, 1, print_time=16 * 3600)
    second = await _any(ctx, 2, print_time=later_time, manual_start=manual_start)
    now = datetime(2026, 11, 9, hour, tzinfo=timezone.utc)

    expected = first if expected_position == 1 else second
    assert await _run(ctx, idle_printers={1}, now=now) == {expected: 1}


@pytest.mark.parametrize(("day", "manual_first"), [(9, True), (10, False)])
async def test_working_hours_manual_order_only_overrides_the_selected_window(ctx, monkeypatch, day, manual_first):
    monkeypatch.setenv("TZ", "UTC")
    await _set(ctx, "queue_working_hours", '{"enabled":true,"start":"08:30","end":"16:30"}')
    await _set(ctx, "queue_shortest_first", "true")
    closing = datetime(2026, 11, 9, 16, 30, tzinfo=timezone.utc)
    await _set(ctx, "queue_working_hours_manual_until", str(int(closing.timestamp())))
    first = await _pinned(ctx, 1, print_time=16 * 3600)
    second = await _any(ctx, 2, print_time=5 * 3600)

    now = datetime(2026, 11, day, 9, tzinfo=timezone.utc)
    expected = first if manual_first else second
    assert await _run(ctx, idle_printers={1}, now=now) == {expected: 1}
