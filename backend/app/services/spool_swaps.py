"""A spool swapped on one tray while a print runs (#3325).

The tray-change log (``utils.tray_split``) splits a print across trays when the
AMS switches to a backup on a runout. A spool replaced by hand in the *same*
tray leaves no trace there: the external spool on a printer without an AMS is
tray 254 before and after, and so is an AMS slot refilled with a new roll. The
only record of the swap is the operator assigning the new spool, and without a
layer to put it at, completion charged the whole print to whichever spool the
tray held at the end.

So a print keeps a second log: the spool on every assigned tray at print start,
and every later assignment of a different spool to a tray, with the layer the
printer was on. At completion a tray's grams are divided between its spools in
proportion to the filament the G-code feeds between those layers (by layer
count without G-code data). For a confirmed runout, use the outgoing spool's
known remaining weight and divide the balance among the other segments.

A tray with no spool at print start is not split by its first assignment:
that is the operator catching up on a forgotten one, and that spool takes the
print from its start, as before. Only a later, different spool splits it.

The log lives on ``PrinterState`` (Spoolman reports after the print session
row is gone) and is mirrored to ``active_print_sessions`` for a restart.
"""

from __future__ import annotations

import logging
import math
from typing import Any

from sqlalchemy import event

from backend.app.api.routes._spoolman_helpers import spoolman_net_weight
from backend.app.utils import threemf_tools

logger = logging.getLogger(__name__)

INVENTORY = "inventory"
SPOOLMAN = "spoolman"


def entry(backend: str, ams_id: int, tray_id: int, spool_id: int, layer: int, start: bool = False) -> dict:
    return {"backend": backend, "ams": ams_id, "tray": tray_id, "spool": spool_id, "layer": layer, "start": start}


def start_entries(backend: str, assignments: dict[tuple[int, int], int]) -> list[dict]:
    """The spool on every assigned tray as the print starts."""
    return [
        entry(backend, ams_id, tray_id, spool_id, 0, start=True) for (ams_id, tray_id), spool_id in assignments.items()
    ]


def _valid(item) -> bool:
    return isinstance(item, dict) and {"backend", "ams", "tray", "spool", "layer"} <= item.keys()


def depleted_inventory_spools(log: list | None) -> set[int]:
    """Built-in inventory spools confirmed empty by a runout replacement."""
    return {
        item["runout_spool"]
        for item in log or []
        if _valid(item) and item["backend"] == INVENTORY and type(item.get("runout_spool")) is int
    }


def _tray_assignments(log: list | None, backend: str, ams_id: int, tray_id: int) -> list[dict]:
    result: list[dict] = []
    for item in log or []:
        if not _valid(item) or (item["backend"], item["ams"], item["tray"]) != (backend, ams_id, tray_id):
            continue
        if not result or result[-1]["spool"] != item["spool"]:
            result.append(item)
    return result


def segments(log: list | None, backend: str, ams_id: int, tray_id: int) -> list[tuple[int, int]]:
    """Return the spool and starting layer for each segment of a swapped tray."""
    return _segments(_tray_assignments(log, backend, ams_id, tray_id))


def _segments(assignments: list[dict]) -> list[tuple[int, int]]:
    if len(assignments) < 2:
        return []
    # A late first assignment covers the print from the start.
    return [(item["spool"], max(0, int(item["layer"] or 0)) if index else 0) for index, item in enumerate(assignments)]


def _known_remaining(value) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        return max(0.0, float(value))
    return None


def split(
    segs: list[tuple[int, int]],
    grams: float,
    filament_id: int | None,
    layer_usage: dict[int, dict[int, float]] | None,
    end_layer: int,
    from_layer: int = 0,
    runout_weights: dict[int, float] | None = None,
) -> list[tuple[int, float]]:
    """Divide ``grams`` fed between ``from_layer`` and ``end_layer`` among the spools of ``segs``.

    Returns ``[(spool_id, grams), ...]`` in order, one entry per spool, the
    shares summing to ``grams``. With nothing to weigh by (no G-code data, no
    layer range) everything goes to the spool loaded at ``end_layer``.
    """
    end_layer = max(end_layer, from_layer)
    bounds: list[tuple[int, int, int]] = []  # (segment index, spool id, layer)
    for index, (spool_id, layer) in enumerate(segs):
        if layer <= from_layer:
            if layer == from_layer == 0 and bounds and bounds[-1][0] in (runout_weights or {}):
                bounds.append((index, spool_id, from_layer))
            else:
                bounds = [(index, spool_id, from_layer)]
        elif layer < end_layer:
            bounds.append((index, spool_id, layer))
    if not bounds:
        bounds = [(len(segs) - 1, segs[-1][0], from_layer)]

    edges = [layer for _, _, layer in bounds] + [end_layer]
    weights: list[float] = []
    if layer_usage and filament_id is not None:

        def mm_at(layer: int) -> float:
            return threemf_tools.get_cumulative_usage_at_layer(layer_usage, layer).get(filament_id, 0.0)

        weights = [max(0.0, mm_at(edges[i + 1]) - mm_at(edges[i])) for i in range(len(bounds))]
    if sum(weights) <= 0:
        weights = [float(edges[i + 1] - edges[i]) for i in range(len(bounds))]
    shares: dict[int, float] = {}
    remaining = grams
    proportional = []
    for index, (segment, spool_id, _) in enumerate(bounds):
        # A replacement must have printed in this range before we allocate
        # grams by runout weight. A print that stopped at the swap uses the
        # existing partial-print accounting.
        known = (runout_weights or {}).get(segment) if index < len(bounds) - 1 else None
        if known is None:
            proportional.append((spool_id, weights[index]))
        else:
            used = min(remaining, known)
            shares[spool_id] = shares.get(spool_id, 0.0) + used
            remaining -= used
    total = sum(weight for _, weight in proportional)
    if total <= 0:
        spool_id = proportional[-1][0]
        shares[spool_id] = shares.get(spool_id, 0.0) + remaining
    else:
        for spool_id, weight in proportional:
            shares[spool_id] = shares.get(spool_id, 0.0) + remaining * weight / total
    return [
        (spool_id, shares[spool_id])
        for spool_id in dict.fromkeys(spool for _, spool, _ in bounds)
        if spool_id in shares
    ]


class Splitter:
    """One print's swap log, ready to share a tray's grams out (one per backend).

    ``layer_usage`` is the G-code's per-layer usage, or a callable returning it,
    read only when a tray was actually swapped. ``end_layer`` is where the
    print stopped; for a completed print the G-code's last layer wins over it.
    """

    def __init__(self, log: list | None, backend: str, layer_usage, end_layer: int, completed: bool) -> None:
        self.log = list(log or [])
        self.backend = backend
        # The per-layer usage, or until first read, the callable that returns it.
        self._layer_usage: Any = layer_usage
        self.end_layer = end_layer
        self.completed = completed
        self._spoolman_runout_weights: dict[int, float] = {}
        self._charged_runout_grams: dict[int, float] = {}

    async def load_spoolman_runout_weights(self, client) -> None:
        """Read the outgoing spools before posting this print's usage."""
        spool_ids = {
            item["runout_spool"]
            for item in self.log
            if _valid(item) and item["backend"] == SPOOLMAN and isinstance(item.get("runout_spool"), int)
        }
        for spool_id in spool_ids:
            try:
                spool = await client.get_spool(spool_id)
                weight = _known_remaining(spool.get("remaining_weight"))
                if weight is None:
                    used = _known_remaining(spool.get("used_weight"))
                    net = spoolman_net_weight(spool)
                    if used is not None and net is not None:
                        weight = max(0.0, net - used)
                if weight is not None:
                    self._spoolman_runout_weights[spool_id] = weight
            except Exception:  # noqa: BLE001 -- fall back to the layer split
                logger.debug("[SPOOL-SWAP] Could not read remaining weight for spool %d", spool_id, exc_info=True)

    def _usage(self) -> dict[int, dict[int, float]] | None:
        if callable(self._layer_usage):
            try:
                self._layer_usage = self._layer_usage()
            except Exception:  # noqa: BLE001 -- the split falls back to layer counts
                logger.debug("[SPOOL-SWAP] No per-layer usage for the split", exc_info=True)
                self._layer_usage = None
        return self._layer_usage

    def swapped(self, ams_id: int, tray_id: int) -> bool:
        return bool(segments(self.log, self.backend, ams_id, tray_id))

    def shares(
        self,
        ams_id: int,
        tray_id: int,
        slot_id: int,
        grams: float,
        from_layer: int = 0,
        to_layer: int | None = None,
    ) -> list[tuple[int, float]] | None:
        """``[(spool_id, grams), ...]`` for a swapped tray, None for one that kept its spool."""
        assignments = _tray_assignments(self.log, self.backend, ams_id, tray_id)
        segs = _segments(assignments)
        if not segs:
            return None
        usage = self._usage()
        if to_layer is not None:
            end = to_layer
        elif self.completed and usage:
            end = max(usage)
        else:
            end = self.end_layer
        runout_weights: dict[int, float] = {}
        unallocated = grams
        for index, replacement in enumerate(assignments[1:]):
            spool_id, _ = segs[index]
            _, replacement_layer = segs[index + 1]
            in_range = from_layer < replacement_layer < end or (from_layer == replacement_layer == 0 and end > 0)
            if replacement.get("runout_spool") != spool_id or not in_range:
                continue
            weight = _known_remaining(replacement.get("runout_remaining_g"))
            if weight is None:
                weight = self._spoolman_runout_weights.get(spool_id)
            if weight is None:
                continue
            # One physical spool can feed several slicer slots. Consume its
            # remaining-weight allowance only once across those charges.
            charged = self._charged_runout_grams.get(spool_id, 0.0)
            used = min(unallocated, max(0.0, weight - charged))
            runout_weights[index] = used
            self._charged_runout_grams[spool_id] = charged + used
            unallocated -= used
        result = split(segs, grams, slot_id - 1, usage, end, from_layer, runout_weights)
        logger.info(
            "[SPOOL-SWAP] %s AMS%d-T%d swapped during the print %s: %.2fg split %s",
            self.backend,
            ams_id,
            tray_id,
            segs,
            grams,
            [(spool, round(share, 2)) for spool, share in result],
        )
        return result


def _on_commit(db, apply) -> None:
    """Run ``apply`` once ``db`` commits; never if it rolls back first."""
    sync = getattr(db, "sync_session", None)
    if sync is None:
        apply()
        return
    settled: list[bool] = []

    def committed(_session) -> None:
        if not settled:
            settled.append(True)
            apply()

    def rolled_back(_session) -> None:
        settled.append(False)

    event.listen(sync, "after_commit", committed, once=True)
    event.listen(sync, "after_rollback", rolled_back, once=True)


def is_runout_tray(state, ams_id: int, tray_id: int) -> bool:
    """Whether this slot is the feed that paused for filament runout."""
    if getattr(state, "state", None) != "PAUSE" or getattr(state, "stg_cur", None) != 6:
        return False
    global_tray = 254 + tray_id if ams_id == 255 else ams_id if ams_id >= 128 else ams_id * 4 + tray_id
    feeding_tray = getattr(state, "last_loaded_tray", -1)
    if feeding_tray == -1:
        feeding_tray = getattr(state, "tray_now", -1)
    return feeding_tray == global_tray


def inventory_assignment_is_empty(state, ams_id: int, tray_id: int, spool_id: int) -> bool:
    """Report the outgoing spool as empty while waiting for its replacement."""
    if not is_runout_tray(state, ams_id, tray_id):
        return False
    assignments = _tray_assignments(getattr(state, "spool_change_log", None), INVENTORY, ams_id, tray_id)
    # The printer can stay paused after the replacement has been assigned.
    return not any(
        item["spool"] == spool_id and "runout_spool" in item and item["layer"] == getattr(state, "layer_num", 0)
        for item in assignments[-1:]
    )


async def record_assignment(db, printer_id: int, ams_id: int, tray_id: int, spool_id: int, backend: str) -> None:
    """Note that ``spool_id`` was assigned to a tray, if that printer is printing.

    Runs on the caller's session, before the caller commits the assignment:
    a second session would wait on the SQLite write lock the assignment holds.
    The log is the persisted row's merged with ``PrinterState``'s: after a
    restart the state is empty until the printer reports RUNNING again, and a
    runout leaves it paused, so the row alone holds the print's start spools.
    ``PrinterState`` takes the new entry only once the assignment commits; one
    that fails must not split the print. Best-effort: an assignment must never
    fail because of this.
    """
    try:
        from backend.app.models.active_print_session import ActivePrintSession
        from backend.app.services.printer_manager import printer_manager

        state = printer_manager.get_status(printer_id)
        if state is None:
            return
        # The row exists from print start to completion: a print Bambuddy is
        # tracking is running.
        row = await db.get(ActivePrintSession, printer_id)
        if row is None:
            return
        log = [item for item in (row.spool_change_log or []) if _valid(item)]
        log += [item for item in (getattr(state, "spool_change_log", None) or []) if _valid(item) and item not in log]
        same_tray = [i for i in log if _valid(i) and (i["backend"], i["ams"], i["tray"]) == (backend, ams_id, tray_id)]
        if same_tray and same_tray[-1]["spool"] == spool_id:
            return
        layer = int(getattr(state, "layer_num", 0) or 0)
        replacement = entry(backend, ams_id, tray_id, spool_id, layer)
        if same_tray and is_runout_tray(state, ams_id, tray_id):
            outgoing_id = same_tray[-1]["spool"]
            replacement["runout_spool"] = outgoing_id
            if backend == INVENTORY:
                from backend.app.models.spool import Spool

                outgoing = await db.get(Spool, outgoing_id)
                if outgoing is not None:
                    replacement["runout_remaining_g"] = max(0.0, outgoing.label_weight - outgoing.weight_used)
        log.append(replacement)
        row.spool_change_log = list(log)
        _on_commit(db, lambda: setattr(state, "spool_change_log", log))
        logger.info(
            "[SPOOL-SWAP] Printer %d %s AMS%d-T%d: spool %d assigned at layer %d during the print",
            printer_id,
            backend,
            ams_id,
            tray_id,
            spool_id,
            layer,
        )
    except Exception:  # noqa: BLE001 -- never fail an assignment over its usage note
        logger.warning("[SPOOL-SWAP] Could not record a mid-print assignment on printer %d", printer_id, exc_info=True)
