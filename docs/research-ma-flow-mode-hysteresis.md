# Research: Music Assistant Flow Mode Track Boundary Hysteresis

Status: **Research / External Patch** — this is a fix for Music Assistant, not this add-on.
Related issue: music-assistant/server#213

## Problem

MA's flow mode calculates the current track index from total elapsed stream time in `_get_flow_queue_stream_index()`. With coarse position updates (HA MPD integration polls every 10s), MA's interpolation overshoots track boundaries, then the next poll corrects it back — causing the index to oscillate between track N and N+1 indefinitely. The progress bar hits ~98% and falls back.

## Root Cause

The chain: **MPD** (embedded in add-on) → **HA MPD Integration** (polls every 10s) → **HA `media_player` entity** → **MA `hass_players` provider** → **Music Assistant UI**

1. MA sends a single continuous audio stream to the player (flow mode).
2. MA determines which track is playing by comparing total elapsed time against cumulative track durations in `flow_mode_stream_log`.
3. Between 10-second HA polls, MA interpolates elapsed time: `corrected_elapsed_time = elapsed_time + (time.time() - elapsed_time_last_updated)`.
4. HTTP streaming through MPD has slight buffering delays — actual playback lags ~1-2s behind wall-clock time over a ~4-minute track.
5. At track boundaries, the interpolation overshoots, MA briefly thinks the next track started, then the next poll corrects it back.
6. MA's existing `max()` anti-backwards protection only works for the **same track** — track ID oscillation resets it.

## Proposed Patch

**File:** `music_assistant/controllers/player_queues.py`
**Location:** `_update_queue_from_player()`, lines 2201-2204

```python
if queue.flow_mode:
    current_index, elapsed_time = self._get_flow_queue_stream_index(queue, player)
    # Flow mode hysteresis: prevent backward index oscillation at track
    # boundaries caused by interpolation overshoot between coarse position
    # updates. In flow mode the stream is continuous and forward-only;
    # legitimate backward jumps (previous, repeat) go through play_index()
    # which resets flow_mode_stream_log behind _transitioning_players guard.
    if (
        queue.current_index is not None
        and current_index is not None
        and current_index < queue.current_index
    ):
        current_index = queue.current_index
        elapsed_time = int(queue.elapsed_time)
```

## Why this is safe

- **Previous/repeat**: `play_index()` sets `queue.current_index` directly AND resets `flow_mode_stream_log = []`. It runs behind the `_transitioning_players` guard, which causes `on_player_update` to return early. By the time `_update_queue_from_player` runs again, `queue.current_index` is already the correct new value.
- **Same track repeat in flow**: Repeated tracks appear as additional entries in `flow_mode_stream_log`, so the log position continues advancing — no backward movement.
- **All flow mode players benefit**: hass_players, AirPlay, Squeezelite, HEOS, Bluesound, Snapcast, Alexa, Sendspin, Fully Kiosk, Universal Group, Sync Group — all use flow mode and all route backward jumps through `play_index()`.

## Verification Steps

1. Apply the patch to the MA server source
2. Build the docker image for pi64/arm and deploy to HAOS
3. Play an album through the MPD → HA → MA chain
4. Verify: progress bar reaches 100% and advances to the next track
5. Verify: previous/next buttons still work correctly
6. Verify: repeat mode works correctly
