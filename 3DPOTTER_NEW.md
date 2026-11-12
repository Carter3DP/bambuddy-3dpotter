# What's new in 3DPotter Bambuddy

This file tracks features added or changed in the 3DPotter fork of Bambuddy. The upstream release history is in [CHANGELOG.md](CHANGELOG.md).

## Working Hours

Configure a daily schedule in **Settings → Workflow → Queue & Dispatch → Working Hours**. Times use the Bambuddy server's `TZ` setting. If the end time is earlier than the start time, the window closes the following day.

- **Queue scheduling.** During Working Hours, the scheduler prioritizes eligible jobs estimated to finish before closing. If no eligible job fits, the scheduler falls back to the normal queue order. The queue and timeline reflect the dispatch order.
- **Per-job control.** A clock and **Moved by Working Hours** badge identify jobs whose order changed. Drag a job or use its mobile move arrows to reserve a position for the current window. Click its **Manual position** badge to return it to automatic ordering. These moves leave the saved normal queue positions intact.
- **Between-print warming.** If **Keep bed warm between prints** and Working Hours are both enabled, Bambuddy holds the bed warm only during the working window and releases the hold on the next scheduler check after closing. With Working Hours disabled, the existing warming rules apply at any time. Preheat and heat soak for a print about to start can still run after hours.

### End-of-day queue behavior

Use **Change the queue at the end of the working day** to choose what happens at closing.

| Setting | Behavior |
| --- | --- |
| **Off — Keep the queue as is** | Keep the last order the scheduler observed during Working Hours, including manual placements. If there is no saved order for the window, use the normal queue order. |
| **On → Restore the queue** | Use the normal queue order, including Shortest Job First if enabled. This is the default. |
| **On → Longest job that fits before opening** | Start the longest eligible job estimated to finish before the next window opens. If no eligible job fits, dispatch follows the normal queue order, including Shortest Job First if enabled. Longer jobs and jobs without an estimate remain eligible. |

Turning off Working Hours itself returns the queue to normal ordering.

PostgreSQL upgrades now align the Working Hours manual-position default with fresh installs. Existing queue positions and manual placements are preserved.

## Color match threshold

Set **Color match threshold (%)** in **Settings → Workflow → Default Print Options**. It applies to queued jobs and virtual printer jobs that use **Force color match**. The default of **0%** requires the exact hex color. Intermediate values allow that percentage of the full range of difference in each red, green, and blue channel. **100%** allows any color. Material type and existing filament variant checks still apply.
## Offline printer filament matching

Bambuddy saves the last reported AMS and external spool filament for each printer across restarts. Model-based queue jobs use that reading to skip offline printers with incompatible filament, including when deciding which printer to wake with a smart plug. When a compatible printer is busy, the queue shows that printer instead of listing incompatible offline printers.

## Runout spool accounting

When you assign a tracked replacement to the same tray during a confirmed filament-runout pause, Bambuddy charges the outgoing spool up to its tracked remaining weight. The replacement takes the balance of the print usage. Built-in inventory and Spoolman both support this. Ordinary swaps and runouts without a known remaining weight use the existing layer-based split. When the print finishes, confirmed runout spools in built-in inventory report zero grams remaining, even if the print estimate charged fewer grams. This inventory correction is separate from the print’s recorded usage.

## Docker images

The [Docker publishing workflow](.github/workflows/docker-publish.yml) builds `linux/amd64` and `linux/arm64` images for `ghcr.io/carter3dp/bambuddy-3dpotter`. It runs on pushes to `main`, version tags beginning with `v`, and manual runs. A manual run uses the selected branch name as a tag, with characters such as `/` replaced by `-`.

To build the Working Hours branch, select `feat/working-hours-dispatch` in **Actions → Build and publish image → Run workflow**. When the run succeeds, the branch image is `ghcr.io/carter3dp/bambuddy-3dpotter:feat-working-hours-dispatch`.

## Maintaining this file

Update this file in the same pull request whenever a fork feature is added or its behavior changes. Extend the relevant section or add one for a new feature. Include where to find the feature, its default behavior, and any setup or upgrade steps users need.
