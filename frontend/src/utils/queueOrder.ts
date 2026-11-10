/**
 * The order the scheduler will actually dispatch pending queue items in.
 *
 * The backend decides this in SQL (`print_scheduler.check_queue`). During
 * working hours, jobs that fit before closing come first. Each group uses:
 *
 *     ORDER BY been_jumped DESC,
 *              print_time_seconds ASC NULLS LAST,
 *              position, id
 *
 * with the first two keys only when Shortest-Job-First is on. It is one order
 * across the whole queue, not one per printer (#3200): a job pinned to a
 * printer and an "Any <model>" job both want that printer, and the one higher
 * in this order gets it. Every UI surface that claims to show queue order has
 * to reproduce it, and each one that reproduced it privately drifted: the
 * timeline sorted by `position` alone and so ignored Shortest-Job-First
 * entirely (#3043).
 */

import type { WorkingHoursEndOfDay } from '../api/client';

interface OrderableQueueItem {
  printer_id?: number | null;
  target_model?: string | null;
  been_jumped?: boolean;
  working_hours_manual_position?: boolean;
  working_hours_position?: number | null;
  working_hours_until?: number | null;
  working_hours_kept_position?: number | null;
  print_time_seconds?: number | null;
  position: number;
  id?: number;
}

/**
 * Which swimlane an item is drawn in on the timeline: a named printer, a
 * printer model, or neither. Display only -- lanes do not decide dispatch
 * order, which is global (#3200).
 *
 * Returned as a string rather than a number because the group is a name, not
 * a magnitude. The flat list used to fold `target_model` down to
 * `-charCodeAt(0)`, which gave `X1C` and `X2D` (and `P1S` and `P1P`) the same
 * key and interleaved two lanes into one.
 */
export function queueLaneKey(item: OrderableQueueItem): string {
  if (item.printer_id != null) return `printer:${item.printer_id}`;
  if (item.target_model) return `model:${item.target_model}`;
  return 'unassigned';
}

/**
 * Order two pending items the way the scheduler takes them, whichever printer
 * or model each is queued for.
 *
 * @param sjfEnabled the `queue_shortest_first` setting.
 * @param workingSecondsLeft seconds until the active working window closes.
 */
export function compareQueueOrder(
  a: OrderableQueueItem,
  b: OrderableQueueItem,
  sjfEnabled: boolean,
  workingSecondsLeft = 0,
): number {
  if (workingSecondsLeft > 0) {
    const fits = ({ print_time_seconds: seconds }: OrderableQueueItem) =>
      seconds != null && seconds > 0 && seconds <= workingSecondsLeft;
    const byFit = Number(fits(b)) - Number(fits(a));
    if (byFit !== 0) return byFit;
  }

  if (sjfEnabled) {
    // Starvation guard: an item something else was allowed to jump ahead of
    // goes first next time, whatever the print times say.
    const aJumped = a.been_jumped ? 1 : 0;
    const bJumped = b.been_jumped ? 1 : 0;
    if (aJumped !== bJumped) return bJumped - aJumped;

    // Shortest first, and an item whose duration we don't know yet sorts last
    // rather than winning by looking like a zero-second print (NULLS LAST).
    const aTime = a.print_time_seconds ?? Infinity;
    const bTime = b.print_time_seconds ?? Infinity;
    if (aTime !== bTime) return aTime - bTime;
  }

  if (a.position !== b.position) return a.position - b.position;
  return (a.id ?? 0) - (b.id ?? 0);
}


export function hasWorkingHoursManualPosition(item: OrderableQueueItem, workingHoursUntil?: number): boolean {
  return workingHoursUntil != null && item.working_hours_until === workingHoursUntil && !!item.working_hours_manual_position;
}

/** Keep manual jobs in their temporary slots while automatic jobs fill the gaps. */
export function sortQueueItems<T extends OrderableQueueItem>(
  items: T[],
  sjfEnabled: boolean,
  workingSecondsLeft: number,
  workingHoursUntil?: number,
  afterHoursOrder?: WorkingHoursEndOfDay,
): T[] {
  if (afterHoursOrder === 'longest_first') {
    const duration = (item: T) => Math.max(0, item.print_time_seconds ?? 0);
    return [...items].sort((a, b) => duration(b) - duration(a) || compareQueueOrder(a, b, false));
  }

  const automaticOrder = [...items].sort((a, b) => compareQueueOrder(a, b, sjfEnabled, workingSecondsLeft));
  if (afterHoursOrder === 'keep') {
    const rank = (item: T) => item.working_hours_kept_position ?? Infinity;
    return automaticOrder.sort((a, b) => rank(a) - rank(b));
  }
  const reserved = (item: T) => hasWorkingHoursManualPosition(item, workingHoursUntil);
  if (!items.some(reserved)) return automaticOrder;

  const slot = (item: T) => item.working_hours_until === workingHoursUntil
    ? item.working_hours_position ?? item.position
    : item.position;
  const automatic = automaticOrder.filter(item => !reserved(item)).values();
  return [...items].sort((a, b) => slot(a) - slot(b) || (a.id ?? 0) - (b.id ?? 0)).map(item =>
    reserved(item) ? item : automatic.next().value!,
  );
}
