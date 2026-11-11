import { describe, expect, it } from 'vitest';
import type { Printer, PrinterStatus, PrintQueueItem, UiSettings } from '../../api/client';
import { forecastQueue } from '../../utils/queueForecast';

const minute = 60_000;
const printer = (id: number, model = 'P1S', location = 'Shop') =>
  ({ id, name: `Printer ${id}`, model, location, is_active: true }) as Printer;
const status = (material: string, overrides: Partial<PrinterStatus> = {}) =>
  ({ connected: true, state: 'IDLE', vt_tray: [{ tray_type: material }], ...overrides }) as PrinterStatus;
const job = (id: number, minutes: number, overrides: Partial<PrintQueueItem> = {}) =>
  ({
    id, position: id, printer_id: null, target_model: 'P1S', status: 'pending',
    print_time_seconds: minutes * 60, ...overrides,
  }) as PrintQueueItem;

describe('queue forecast', () => {
  const now = Date.parse('2026-11-11T10:00:00Z');

  it('uses the first compatible available printer and adds the setup gap on each lane', () => {
    const items = [
      job(1, 30, { required_filament_types: ['PETG'] }),
      job(2, 10, { required_filament_types: ['PETG'] }),
      job(3, 20, { required_filament_types: ['PLA'] }),
    ];
    const printers = [printer(1), printer(2), printer(3)];
    const statuses = {
      1: status('PETG', { connected: false }),
      2: status('PETG', { state: 'RUNNING', remaining_time: 15 }),
      3: status('PLA'),
    };
    const forecast = forecastQueue(items, printers, statuses, undefined, now);
    expect(forecast.get(1)).toMatchObject({ printerId: 2, startsAt: now + 20 * minute, finishesAt: now + 50 * minute });
    expect(forecast.get(2)).toMatchObject({ printerId: 2, startsAt: now + 55 * minute, finishesAt: now + 65 * minute });
    expect(forecast.get(3)).toMatchObject({ printerId: 3, startsAt: now, finishesAt: now + 20 * minute });

    const noGap = forecastQueue(items, printers, statuses, { queue_forecast_gap_minutes: 0 }, now);
    expect(noGap.get(2)?.startsAt).toBe(now + 45 * minute);
  });

  it('reapplies the Working Hours priorities at future starts in the server timezone', () => {
    // 16:00 New York, half an hour before closing.
    const start = Date.parse('2026-11-11T21:00:00Z');
    const settings: UiSettings = {
      queue_working_hours_timezone: 'America/New_York',
      queue_working_hours: { enabled: true, start: '08:30', end: '16:30', end_of_day: 'longest_first' },
    };
    const items = [job(1, 17 * 60), job(2, 25), job(3, 3 * 60), job(4, 2 * 60)];
    const forecast = forecastQueue(items, [printer(1)], { 1: status('PLA') }, settings, start);
    expect([...forecast.keys()]).toEqual([2, 3, 4, 1]);
    expect(forecast.get(3)?.startsAt).toBe(start + 30 * minute);
    expect(forecast.get(1)?.startsAt).toBe(start + 340 * minute);
  });

  it('continues on other printers after an unknown duration blocks a lane', () => {
    const items = [job(1, 0, { printer_id: 1 }), job(2, 20, { printer_id: 1 }), job(3, 20)];
    const forecast = forecastQueue(items, [printer(1), printer(2)], { 1: status('PLA'), 2: status('PLA') }, undefined, now);
    expect([...forecast.keys()]).toEqual([3]);
    expect(forecast.get(3)?.printerId).toBe(2);
  });

  it('uses the selected variant estimate and leaves staged and unknown jobs out', () => {
    const scheduled = now + 30 * minute;
    const items = [
      job(1, 100, { variants: [
        { position: 1, library_file_id: 1, filename: 'p1s.3mf', target_model: 'P1S', required_filament_types: ['PETG'], print_time_seconds: 6000 },
        { position: 2, library_file_id: 2, filename: 'a1.3mf', target_model: 'A1', required_filament_types: ['PLA'], print_time_seconds: 1200 },
      ], scheduled_time: new Date(scheduled).toISOString() }),
      job(2, 10, { manual_start: true }),
      job(3, 0),
    ];
    const forecast = forecastQueue(items, [printer(1), printer(2, 'A1')], { 1: status('PLA'), 2: status('PLA') }, undefined, now);
    expect([...forecast.keys()]).toEqual([1]);
    expect(forecast.get(1)).toMatchObject({ printerId: 2, startsAt: scheduled, finishesAt: scheduled + 20 * minute });
  });
});
