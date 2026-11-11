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

## Queue finish forecast

Pending jobs show a predicted printer and finish time. The Timeline places those jobs on the predicted printer's lane. Bambuddy uses live printer status, filament compatibility, scheduled starts, queue order, and Working Hours at each predicted start. Change the setup allowance under **Settings → Workflow → Queue & Dispatch → Queue Forecast**. It defaults to **5 minutes** between prints on each printer and affects estimates only.

The forecast skips jobs that need a manual action and leaves offline printers out until they reconnect. If a job has no duration estimate, the forecast stops at that job on its predicted printer and continues on other printers. Predictions update as printer status and the queue change.

## Docker images

The [Docker publishing workflow](.github/workflows/docker-publish.yml) builds `linux/amd64` and `linux/arm64` images for `ghcr.io/carter3dp/bambuddy-3dpotter`. It runs on pushes to `main`, version tags beginning with `v`, and manual runs. A manual run uses the selected branch name as a tag, with characters such as `/` replaced by `-`.

To build the Working Hours branch, select `feat/working-hours-dispatch` in **Actions → Build and publish image → Run workflow**. When the run succeeds, the branch image is `ghcr.io/carter3dp/bambuddy-3dpotter:feat-working-hours-dispatch`.

## Maintaining this file

Update this file in the same pull request whenever a fork feature is added or its behavior changes. Extend the relevant section or add one for a new feature. Include where to find the feature, its default behavior, and any setup or upgrade steps users need.
