# Design Doc: Integrate BT MPRIS/Volume & Media Controls with Multi-Room Audio

## Context

**Two HA add-ons, complementary roles:**
- **BT Audio Manager** — manages Bluetooth connections (pairing, A2DP profiles, AVRCP). When a BT speaker connects, PulseAudio gets a `bluez_sink.*` that becomes available system-wide.
- **Multi-Room Audio Controller** — manages audio playback zones. Discovers any PulseAudio sink (sound cards, USB DACs, **BT sinks**). Users create "players" from sinks. Each player integrates with Music Assistant via Sendspin SDK for playback control.

**The integration need:** For BT speakers whose connection is managed by BT Audio Manager and whose playback is managed by Multi-Room Audio, hardware buttons on the speaker (volume, play/pause/next/prev) need to reach the multiroom app. Conversely, playback state from Music Assistant needs to flow back to the BT speaker (so devices like Bose show correct Playing/Paused indicators).

**This is bidirectional:**
```
BT Speaker ←→ BlueZ ←→ BT Audio Manager ←→ Multi-Room Audio ←→ Music Assistant
              AVRCP         HTTP REST              Sendspin SDK
```

---

## Architecture: HTTP REST in Both Directions

Both apps communicate via direct HTTP REST calls over the HA add-on Docker network.

### Forward Path: BT Speaker Buttons → Multiroom App

**Volume:**
```
BT speaker volume button
  → AVRCP absolute volume → BlueZ MediaTransport1.Volume (0-127)
  → BT Audio Manager converts to 0-100%
  → Looks up device address → mapped multiroom player name
  → HTTP PUT http://<multiroom-host>:8096/api/players/{name}/volume
    Body: {"volume": 50}
  → Multiroom app sets Sendspin player volume + syncs to Music Assistant
```

**Media controls:**
```
BT speaker play/pause/next/prev button
  → AVRCP → BlueZ → MPRIS method call (Play/Pause/Next/Previous)
  → BT Audio Manager _on_avrcp_command()
  → Looks up device address → mapped multiroom player name
  → HTTP POST http://<multiroom-host>:8096/api/players/{name}/{command}
  → Multiroom app → Sendspin SDK → Music Assistant handles playback
```

### Reverse Path: Playback State → BT Speaker (Webhook Callback)

```
Music Assistant changes playback state (play/pause/stop)
  → Sendspin SDK fires PlayerStateChanged event in multiroom app
  → Multiroom app POST http://<bt-audio-host>:8099/api/playback-state
    Body: {"player": "kitchen", "status": "Playing"}  // or "Paused", "Stopped"
  → BT Audio Manager maps player → BT device address(es)
  → Updates MPRIS PlaybackStatus property on D-Bus
  → BlueZ relays to BT speaker via AVRCP
  → Speaker LED/display shows correct state (e.g., Bose shows play/pause indicator)
```

---

## Player Mapping: Auto-Discovery

BT Audio Manager queries multiroom app to discover available players:

```
GET http://<multiroom-host>:8096/api/players
→ [{name: "kitchen", displayName: "Kitchen", volume: 75, ...}, ...]
```

- Queried on startup and periodically (every 60s), plus on-demand from UI
- Presented as dropdown in BT device settings modal
- Fallback to manual text entry if multiroom app is unreachable

**Per-device config** in `paired_devices.json`:
```json
{
  "address": "AA:BB:CC:DD:EE:FF",
  "name": "Bose Kitchen",
  "auto_connect": true,
  "multiroom_player": "kitchen"
}
```

If `multiroom_player` is not set, commands fall back to local MPD routing (current behavior). This keeps backward compatibility.

---

## Changes Required

### BT Audio Manager (this repo)

1. **New: Multiroom HTTP client** — `src/bt_audio_manager/integrations/multiroom.py`
   - `aiohttp.ClientSession` with connection pooling
   - `discover_players()` → GET /api/players, cache result
   - `set_volume(player_name, volume_pct)` → PUT /api/players/{name}/volume
   - `send_command(player_name, command)` → POST /api/players/{name}/{command}
     - Commands: play, pause, next, previous, stop
   - Timeout: 2s, no retry (fire-and-forget with warning log)
   - Periodic player list refresh (60s interval)

2. **New: Playback state webhook endpoint** — in `src/bt_audio_manager/web/api.py`
   - `POST /api/playback-state`
   - Body: `{"player": "kitchen", "status": "Playing"|"Paused"|"Stopped"}`
   - Maps player name → all BT devices with that `multiroom_player` mapping
   - Calls `media_player.set_playback_status(status)` to update MPRIS on D-Bus
   - BlueZ forwards the status to the BT speaker via AVRCP

3. **Modify: Manager command routing** — `src/bt_audio_manager/manager.py`
   - `_on_avrcp_command()`: if device has `multiroom_player`, route to multiroom client
   - `_on_avrcp_event()` (MediaTransport1.Volume): if device has `multiroom_player`, forward volume
   - Keep MPD routing as fallback for devices without multiroom mapping

4. **Modify: Per-device settings** — `src/bt_audio_manager/persistence/store.py`
   - Add `multiroom_player` field (optional string)

5. **Modify: Settings UI** — `web/static/app.js` + `index.html`
   - "Multiroom Player" dropdown in device settings modal
   - Populated from auto-discovered player list
   - Refresh button

6. **Modify: Settings API** — `web/api.py`
   - Accept `multiroom_player` in PUT /api/devices/{address}/settings
   - New: GET /api/multiroom/players → proxied from multiroom app

7. **Add-on config** — `config.yaml` for both stable and dev
   - `multiroom_host` (default: empty = feature disabled)
   - `multiroom_port` (default: 8096)

### Multi-Room Audio Controller (separate repo)

1. **New: Media transport endpoints** in `PlayersEndpoint.cs`
   - `POST /api/players/{name}/play` → Sendspin `PlayAsync()`
   - `POST /api/players/{name}/pause` → Sendspin `PauseAsync()`
   - `POST /api/players/{name}/next` → Sendspin `NextAsync()`
   - `POST /api/players/{name}/previous` → Sendspin `PreviousAsync()`
   - `POST /api/players/{name}/stop` → Sendspin `StopAsync()`

2. **New: Webhook callback on state change** in `PlayerManagerService.cs`
   - When `PlayerStateChanged` fires from Sendspin SDK, POST to configured BT Audio Manager URL
   - Config: `bt_audio_host` / `bt_audio_port` in multiroom's add-on options
   - Fire-and-forget with timeout (don't block playback on webhook failure)

3. **No changes needed for volume** — existing `PUT /api/players/{name}/volume` works as-is

---

## Feedback Loop Prevention

- **Volume**: One-directional. BT button → BT App → Multiroom. No reverse volume path exists (Multiroom doesn't push volume back to BT App).
- **Media controls**: One-directional commands. BT button → BT App → Multiroom → Music Assistant. MA doesn't echo commands back.
- **Playback state**: One-directional notification. MA → Multiroom → BT App → MPRIS. The MPRIS `PlaybackStatus` update doesn't trigger a new AVRCP command — it's a property notification, not a command.
- **Key**: The forward path carries *commands* (user intent). The reverse path carries *state* (system status). These are different D-Bus mechanisms (method calls vs property changes) and don't create loops.

---

## Networking

- HA add-ons share a Docker network managed by the Supervisor
- Add-ons reach each other via slug-based hostnames (e.g., `local-multiroom-audio`, `local-bluetooth-audio-manager`)
- Configurable via `multiroom_host`/`bt_audio_host` options for non-standard setups

---

## Graceful Degradation

- **Multiroom app unreachable**: BT Audio Manager logs warning, doesn't crash. If `multiroom_player` is set but multiroom is down, events are silently dropped. Player discovery returns empty list; UI shows "Multiroom app unavailable."
- **BT Audio Manager unreachable**: Multiroom app's webhook POST fails silently. Playback continues normally; BT speaker just won't get state updates.
- **No mapping configured**: Device behaves exactly as today (MPD routing or no routing).

---

## Open Questions

1. **Mute**: AVRCP supports mute. Multiroom has `PUT /api/players/{name}/mute`. Should BT mute button toggle mute in multiroom? Straightforward to add if desired.

2. **Multiple BT devices → same player**: Supported by design (multiple devices can map to `"kitchen"`). All get the same playback state. Volume reflects whichever speaker was adjusted last.

3. **Volume delta vs absolute**: AVRCP sends absolute volume (0-127). This replaces the player's current volume entirely. Relative ±step isn't available from the protocol — this is inherent to AVRCP.

4. **Seek**: MPRIS supports `Seek(offset)`. Could forward if multiroom app adds a seek endpoint. Lower priority.

5. **Track metadata sync**: Could BT Audio Manager push track metadata (title/artist) from Music Assistant → MPRIS Metadata property → BT speaker display? Bose and similar speakers can display track info via AVRCP. Would require the reverse webhook to also carry metadata.
