import type { Printer, PrinterStatus, PrintQueueItem, UiSettings } from '../api/client';
import { canonicalFilamentType } from './amsHelpers';
import { parseUTCDate } from './date';
import { ACTIVE_PRINT_STATES, filterCompatibleQueueItems, isPrinterCurrentlyDispatchable } from './printer';
import { sortQueueItems } from './queueOrder';
import { isBusyOnlyWaitingReason } from './waitingReason';

const MINUTE = 60_000;
const DAY = 24 * 60 * MINUTE;

export interface QueueForecastEntry {
  printerId: number;
  printerName: string;
  startsAt: number;
  finishesAt: number;
}

function workingWindows(hours: UiSettings['queue_working_hours'], timeZone: string) {
  if (!hours?.enabled || hours.start === hours.end) return undefined;

  const formatter = new Intl.DateTimeFormat('en-GB', {
    timeZone, year: 'numeric', month: 'numeric', day: 'numeric',
    hour: 'numeric', minute: 'numeric', hourCycle: 'h23',
  });
  const wallTime = (instant: number) => {
    const parts = Object.fromEntries(formatter.formatToParts(instant).map(part => [part.type, Number(part.value)]));
    return Date.UTC(parts.year, parts.month - 1, parts.day, parts.hour, parts.minute);
  };
  const minutes = (time: string) => {
    const [hour, minute] = time.split(':').map(Number);
    return (hour * 60 + minute) * MINUTE;
  };
  const opening = minutes(hours.start);
  const closing = minutes(hours.end);
  const dates = new Map<number, { opens: number; closes: number }>();
  const toInstant = (wall: number) => {
    // Try the offsets on both sides of a timezone transition. Match the
    // scheduler's earlier occurrence at fall-back and forward shift for a gap.
    const candidates = [wall - DAY, wall + DAY].map(sample => wall - (wallTime(sample) - sample));
    const exact = candidates.filter(instant => wallTime(instant) === wall);
    return exact.length ? Math.min(...exact) : Math.max(...candidates);
  };
  const window = (day: number) => {
    let result = dates.get(day);
    if (!result) {
      result = {
        opens: toInstant(day + opening),
        closes: toInstant(day + closing + (closing < opening ? DAY : 0)),
      };
      dates.set(day, result);
    }
    return result;
  };
  return (instant: number) => {
    const day = Math.floor(wallTime(instant) / DAY) * DAY;
    const previous = window(day - DAY);
    const today = window(day);
    if (instant < today.opens) {
      return instant < previous.closes ? { active: previous } : { previous, next: today };
    }
    return instant < today.closes ? { active: today } : { previous: today, next: window(day + DAY) };
  };
}

export function runningPrintFinish(item: PrintQueueItem, status: Partial<PrinterStatus> | undefined, now: number) {
  if (status?.remaining_time && status.remaining_time > 0) return now + status.remaining_time * MINUTE;
  const duration = item.print_time_seconds ?? 0;
  if (duration <= 0) return undefined;
  if (status?.progress != null) return now + duration * 1000 * (1 - Math.min(100, Math.max(0, status.progress)) / 100);
  const start = parseUTCDate(item.started_at)?.getTime();
  return start == null ? undefined : Math.max(now, start + duration * 1000);
}

export function forecastQueue(
  items: PrintQueueItem[],
  printers: Printer[],
  statuses: Record<number, PrinterStatus>,
  settings: UiSettings | undefined,
  now: number,
): Map<number, QueueForecastEntry> {
  const results = new Map<number, QueueForecastEntry>();
  const gap = (settings?.queue_forecast_gap_minutes ?? 5) * MINUTE;
  const sjf = settings?.queue_shortest_first ?? false;
  const hours = settings?.queue_working_hours;
  const getWindow = workingWindows(hours, settings?.queue_working_hours_timezone ?? 'UTC');
  const running = new Map(items.filter(item => item.status === 'printing').map(item => [item.printer_id, item]));
  const lanes = printers.filter(printer => printer.is_active).flatMap(printer => {
    const status = statuses[printer.id];
    if (!status?.connected || status.awaiting_plate_clear || status.state === 'PAUSE') return [];
    const current = running.get(printer.id);
    let availableAt: number;
    if (current || ACTIVE_PRINT_STATES.includes(status.state ?? '')) {
      const finish = current ? runningPrintFinish(current, status, now) :
        (status.remaining_time && status.remaining_time > 0 ? now + status.remaining_time * MINUTE : undefined);
      if (finish == null) return [];
      availableAt = finish + gap;
    } else {
      if (!isPrinterCurrentlyDispatchable(status)) return [];
      availableAt = now;
    }
    const trays = [...(status.ams ?? []).flatMap(unit => unit.tray ?? []), ...(status.vt_tray ?? [])];
    const types = new Set<string>();
    const colors = new Set<string>();
    const variants = new Set<string>();
    for (const tray of trays) {
      const type = canonicalFilamentType(tray.tray_type ?? undefined);
      if (!type) continue;
      types.add(type);
      if (tray.tray_color) {
        const color = tray.tray_color.replace('#', '').toLowerCase().slice(0, 6);
        colors.add(`${type}:${color}`);
        variants.add(`${type}:${color}:${tray.tray_info_idx ?? ''}`);
      }
    }
    return [{ printer, availableAt, types, colors, variants }];
  }).sort((a, b) => a.printer.id - b.printer.id);
  const modelKey = (model: string | null) => model?.toUpperCase().replace(/[\s-]/g, '');
  const pending = items.flatMap(item => {
    if (item.status !== 'pending' || item.manual_start || item.require_previous_success ||
      (item.waiting_reason && !isBusyOnlyWaitingReason(item.waiting_reason))) return [];

    const alternatives = item.variants?.length ? [...item.variants].sort((a, b) => a.position - b.position) : [item];
    const options = alternatives.flatMap(candidate => {
      const duration = candidate.print_time_seconds;
      const durationSeconds = duration != null && Number.isFinite(duration) && duration > 0 ? duration : undefined;
      const overrides = candidate.filament_overrides?.map(override =>
        ({ ...override, type: canonicalFilamentType(override.type) })) ?? null;
      const types = [...(candidate.required_filament_types ?? []), ...(overrides ?? []).map(override => override.type)]
        .map(canonicalFilamentType);
      const requirements = { ...item, required_filament_types: types, filament_overrides: overrides };
      const compatible = lanes.filter(lane => {
        if (item.printer_id != null) return lane.printer.id === item.printer_id;
        if (!candidate.target_model || modelKey(lane.printer.model) !== modelKey(candidate.target_model)) return false;
        if (item.target_location && item.target_location !== lane.printer.location) return false;
        return filterCompatibleQueueItems([requirements], lane.types, lane.colors, lane.variants).length > 0;
      });
      // For equally available printers, prefer more of the requested optional colors.
      const colorScore = (lane: typeof lanes[number]) => (overrides ?? []).filter(override =>
        !override.force_color_match &&
        lane.colors.has(`${override.type}:${override.color.replace('#', '').toLowerCase().slice(0, 6)}`)
      ).length;
      compatible.sort((a, b) => colorScore(b) - colorScore(a) || a.printer.id - b.printer.id);
      return compatible.map(lane => ({ lane, duration: durationSeconds }));
    });
    if (!options.length) return [];
    const scheduled = parseUTCDate(item.scheduled_time)?.getTime();
    const earliest = scheduled != null && Number.isFinite(scheduled) ? Math.max(now, scheduled) : now;
    return [{ item, options, earliest }];
  });

  const remaining = new Map(pending.map(job => [job.item.id, job]));
  let lastWorkingOrder: { closing: number; ids: number[] } | undefined;
  let keptClosing: number | undefined;
  let keptPositions = new Map<number, number>();
  while (remaining.size) {
    let time = Infinity;
    for (const job of remaining.values()) {
      for (const option of job.options) time = Math.min(time, Math.max(job.earliest, option.lane.availableAt));
    }
    if (!Number.isFinite(time)) break;
    const phase = getWindow?.(time);
    const deadline = phase?.active?.closes;
    const endOfDay = phase?.next ? hours?.end_of_day : undefined;
    const opening = phase?.next?.opens;
    let orderable = Array.from(remaining.values(), job => job.item);
    if (endOfDay === 'keep' && phase?.previous) {
      const closing = phase.previous.closes;
      if (keptClosing !== closing) {
        keptClosing = closing;
        const saved = orderable.some(item => item.working_hours_kept_position != null) && closing < now;
        const order = saved
          ? sortQueueItems(orderable, sjf, 0, undefined, 'keep')
          : sortQueueItems(orderable, sjf, 0.001, Math.floor(closing / 1000));
        const ids = lastWorkingOrder?.closing === closing ? lastWorkingOrder.ids : order.map(item => item.id);
        keptPositions = new Map(ids.map((id, index) => [id, index]));
      }
      orderable = orderable.map(item => ({ ...item, working_hours_kept_position: keptPositions.get(item.id) }));
    }
    const order = sortQueueItems(orderable, sjf, deadline == null ? 0 : (deadline - time) / 1000,
      deadline == null ? undefined : Math.floor(deadline / 1000), endOfDay, opening == null ? undefined : (opening - time) / 1000);
    if (deadline != null && hours?.end_of_day === 'keep') {
      lastWorkingOrder = { closing: deadline, ids: order.map(item => item.id) };
    }
    for (const item of order) {
      const job = remaining.get(item.id)!;
      if (job.earliest > time) continue;
      const option = job.options.find(option => option.lane.availableAt <= time);
      if (!option) continue;
      const finishesAt = option.duration == null ? undefined : time + option.duration * 1000;
      if (finishesAt != null) results.set(item.id, {
        printerId: option.lane.printer.id, printerName: option.lane.printer.name,
        startsAt: time, finishesAt,
      });
      // The queue can keep forecasting other printers after a job whose
      // finish time is unknown, but it cannot see past it on this printer.
      option.lane.availableAt = finishesAt == null ? Infinity : finishesAt + gap;
      remaining.delete(item.id);

      if (sjf && option.duration != null) {
        for (const other of remaining.values()) {
          if (other.item.been_jumped || other.item.position >= item.position) continue;
          const onPrinter = other.options.find(candidate => candidate.lane === option.lane);
          if (onPrinter && (onPrinter.duration == null || onPrinter.duration > option.duration)) {
            other.item = { ...other.item, been_jumped: true };
          }
        }
      }
      break;
    }
  }
  return results;
}
