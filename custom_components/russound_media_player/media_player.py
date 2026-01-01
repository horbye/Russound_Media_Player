import asyncio
import logging
import time
from typing import Optional

from homeassistant.components.media_player import (
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
)
from homeassistant.const import CONF_HOST, CONF_PORT, CONF_SOURCE, CONF_NAME
from homeassistant.util import dt as dt_util

_LOGGER = logging.getLogger(__name__)

# ---------- Behavior knobs ----------
SONG_CHANGE_PLAYING_WINDOW_S = 5 * 60
MANUAL_STATE_HOLD_SECONDS = 10

# Disconnect when IDLE/OFF and no incoming RIO traffic for this long
INACTIVITY_DISCONNECT_S = 15 * 60

# Re-evaluate songName fallback (no GET) this often
STATE_EVAL_INTERVAL_S = 30

# Reconnect backoff on errors
RECONNECT_DELAY_S = 30

# Art preference: show "channelArtURL" if present, else "coverArtURL"
PREFER_CHANNEL_ART = True

# --- While PLAYING, do lightweight polling (fixes MBX not pushing WATCH updates) ---
POLL_WHILE_PLAYING_ENABLED = True
POLL_PLAYTIME_INTERVAL_S = 2.0       # progress bar
POLL_PLAYSTATUS_INTERVAL_S = 5.0     # keeps state correct on some boxes
POLL_METADATA_INTERVAL_S = 25.0      # song/artist/art refresh while playing
# -----------------------------------


def _as_bool(val: str) -> Optional[bool]:
    if val is None:
        return None
    v = str(val).strip().upper()
    if v in {"1", "TRUE", "ON", "YES", "ENABLED"}:
        return True
    if v in {"0", "FALSE", "OFF", "NO", "DISABLED"}:
        return False
    return None


class _WakeSentinel:
    pass


WAKE = _WakeSentinel()


async def async_setup_entry(hass, config_entry, async_add_entities):
    config = config_entry.data
    async_add_entities(
        [
            RussoundSourceEntity(
                config[CONF_HOST],
                config[CONF_PORT],
                config[CONF_SOURCE],
                config.get(CONF_NAME, "Russound Media Player"),
            )
        ]
    )


class RussoundSourceEntity(MediaPlayerEntity):
    """
    Russound source-mode MediaPlayer.

    Low-power friendly:
    - Disconnect when IDLE/OFF and no incoming RIO traffic for INACTIVITY_DISCONNECT_S.
    - No big heartbeat loops while idle.

    BUT:
    - While PLAYING, we do small polling (playTime/playStatus) because many MBX units
      don't push WATCH updates frequently (or at all) for these keys.
    """

    _attr_should_poll = False

    def __init__(self, ip: str, port: int, source: int, display_name: str):
        self._ip, self._port, self._source = ip, port, source
        self._attr_name = display_name
        self._attr_unique_id = f"russound_{ip.replace('.', '_')}_source_{source}"

        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None

        self._main_task: Optional[asyncio.Task] = None
        self._poll_task: Optional[asyncio.Task] = None

        self._cmd_queue: asyncio.Queue[object] = asyncio.Queue()
        self._connect_lock = asyncio.Lock()

        # WATCH debug
        self._observed_keys = set()
        self._last_watch_dt = None
        self._last_watch_key = None

        # Last received line time (monotonic)
        self._last_rx_mono: Optional[float] = None
        self._last_state_eval_mono: float = 0.0

        # playStatus tracking
        self._has_playstatus_field = False
        self._playstatus_usable = False
        self._raw_playstatus: Optional[str] = None

        # playTime tracking
        self._raw_playtime: Optional[str] = None

        # Support flags (from S[x].Support.*)
        self._support_playtime: Optional[bool] = None
        self._support_playstatus: Optional[bool] = None

        # Split last-seen per message type (S=GET, N=WATCH) for REAL transport keys only
        self._last_playstatus_s: Optional[str] = None
        self._last_playstatus_n: Optional[str] = None
        self._last_playtime_s: Optional[str] = None
        self._last_playtime_n: Optional[str] = None
        self._last_transport_msgtype: Optional[str] = None  # "S" or "N"
        self._last_transport_utc = None

        # Manual override (HA buttons) when playStatus unusable
        self._manual_override_active = False
        self._manual_override_state: Optional[MediaPlayerState] = None
        self._manual_state_hold_until = 0.0

        # songName change tracking (fallback)
        self._last_songname_value: Optional[str] = None
        self._last_songname_change_utc = None

        # Attributes
        self._attr_available = False
        self._streaming_provider = "Unknown"

        self._attr_state = MediaPlayerState.OFF
        self._attr_media_title = None
        self._attr_media_artist = None

        # Art URLs
        self._cover_art_url: Optional[str] = None
        self._channel_art_url: Optional[str] = None
        self._attr_media_image_url = None

        self._attr_media_duration = None
        self._attr_media_position = None
        self._attr_media_position_updated_at = None

        self._attr_shuffle = False
        self._attr_repeat_mode = "OFF"

        self._attr_supported_features = (
            MediaPlayerEntityFeature.PLAY
            | MediaPlayerEntityFeature.PAUSE
            | MediaPlayerEntityFeature.STOP
            | MediaPlayerEntityFeature.NEXT_TRACK
            | MediaPlayerEntityFeature.PREVIOUS_TRACK
            | MediaPlayerEntityFeature.SEEK
            | MediaPlayerEntityFeature.SHUFFLE_SET
        )

        self._attr_device_info = {
            "identifiers": {("russound", f"{ip}_{source}")},
            "name": display_name,
            "manufacturer": "Russound",
            "model": "Russound Source (RIO)",
        }

        # Poll scheduling
        self._next_poll_playtime_mono = 0.0
        self._next_poll_playstatus_mono = 0.0
        self._next_poll_meta_mono = 0.0

    @property
    def shuffle(self):
        return self._attr_shuffle

    # -------------------------
    # Helpers (state/override)
    # -------------------------

    def _manual_hold_active(self) -> bool:
        now = dt_util.as_timestamp(dt_util.utcnow())
        return now < self._manual_state_hold_until

    def _set_state_manual(self, state: MediaPlayerState):
        now = dt_util.as_timestamp(dt_util.utcnow())
        self._attr_state = state
        self._manual_state_hold_until = now + MANUAL_STATE_HOLD_SECONDS

        if not self._playstatus_usable:
            self._manual_override_active = True
            self._manual_override_state = state

        self.async_write_ha_state()

    def _clear_manual_override(self, reason: str):
        if self._manual_override_active:
            _LOGGER.debug("Clearing manual override (%s)", reason)
        self._manual_override_active = False
        self._manual_override_state = None

    def _evaluate_state_from_song_change(self) -> bool:
        if self._playstatus_usable or self._manual_override_active:
            return False

        if self._last_songname_change_utc is None:
            desired = MediaPlayerState.IDLE
        else:
            age_s = (dt_util.utcnow() - self._last_songname_change_utc).total_seconds()
            desired = MediaPlayerState.PLAYING if age_s <= SONG_CHANGE_PLAYING_WINDOW_S else MediaPlayerState.IDLE

        if desired != self._attr_state:
            self._attr_state = desired
            return True
        return False

    def _apply_playstatus(self, raw_val: str) -> bool:
        self._has_playstatus_field = True
        self._raw_playstatus = raw_val

        if raw_val is None or str(raw_val).strip() == "":
            self._playstatus_usable = False
            return False

        if self._manual_hold_active():
            return False

        val = str(raw_val).strip()
        val_upper = val.upper()
        old = self._attr_state

        if val.isdigit():
            self._playstatus_usable = True
            n = int(val)
            if n == 1:
                self._attr_state = MediaPlayerState.PLAYING
            elif n == 2:
                self._attr_state = MediaPlayerState.PAUSED
            else:
                self._attr_state = MediaPlayerState.IDLE
            return self._attr_state != old

        if "PAUS" in val_upper:
            self._playstatus_usable = True
            self._attr_state = MediaPlayerState.PAUSED
        elif "PLAY" in val_upper:
            self._playstatus_usable = True
            self._attr_state = MediaPlayerState.PLAYING
        elif val_upper in {"STOP", "STOPPED", "IDLE", "OFF"}:
            self._playstatus_usable = True
            self._attr_state = MediaPlayerState.IDLE
        else:
            self._playstatus_usable = False
            return False

        return self._attr_state != old

    # -------------------------
    # Transport recording (S vs N)
    # -------------------------

    @staticmethod
    def _is_real_transport_key(key_upper: str, name: str) -> bool:
        if ".SUPPORT." in key_upper:
            return False
        return key_upper.endswith(f".{name}")

    def _record_transport_value(self, msg_type: Optional[str], key_upper: str, val: str):
        if msg_type not in ("S", "N"):
            return

        self._last_transport_msgtype = msg_type
        self._last_transport_utc = dt_util.utcnow()

        if self._is_real_transport_key(key_upper, "PLAYSTATUS"):
            if msg_type == "S":
                self._last_playstatus_s = val
            else:
                self._last_playstatus_n = val
        elif self._is_real_transport_key(key_upper, "PLAYTIME"):
            if msg_type == "S":
                self._last_playtime_s = val
            else:
                self._last_playtime_n = val

    # -------------------------
    # Art selection
    # -------------------------

    def _choose_art_url(self) -> Optional[str]:
        if PREFER_CHANNEL_ART and self._channel_art_url:
            return self._channel_art_url
        if self._cover_art_url:
            return self._cover_art_url
        if self._channel_art_url:
            return self._channel_art_url
        return None

    # -------------------------
    # HA attributes
    # -------------------------

    @property
    def extra_state_attributes(self):
        now_utc = dt_util.utcnow()

        song_change_age_s = (
            (now_utc - self._last_songname_change_utc).total_seconds()
            if self._last_songname_change_utc
            else None
        )
        last_watch_age_s = (
            (now_utc - self._last_watch_dt).total_seconds()
            if self._last_watch_dt
            else None
        )
        transport_age_s = (
            (now_utc - self._last_transport_utc).total_seconds()
            if self._last_transport_utc
            else None
        )
        rx_age_s = (
            (time.monotonic() - self._last_rx_mono)
            if self._last_rx_mono is not None
            else None
        )

        return {
            "streaming_provider": self._streaming_provider,
            "repeat_status": self._attr_repeat_mode,

            "has_playstatus": self._has_playstatus_field,
            "playstatus_usable": self._playstatus_usable,
            "raw_playstatus": self._raw_playstatus,
            "raw_playtime": self._raw_playtime,

            "support_playstatus": self._support_playstatus,
            "support_playtime": self._support_playtime,

            "playstatus_s": self._last_playstatus_s,
            "playstatus_n": self._last_playstatus_n,
            "playtime_s": self._last_playtime_s,
            "playtime_n": self._last_playtime_n,
            "last_transport_msgtype": self._last_transport_msgtype,
            "last_transport_age_s": transport_age_s,

            "song_change_window_s": SONG_CHANGE_PLAYING_WINDOW_S,
            "last_songname_change_utc": self._last_songname_change_utc.isoformat() if self._last_songname_change_utc else None,
            "last_songname_change_age_s": song_change_age_s,

            "manual_override_active": self._manual_override_active,
            "manual_override_state": self._manual_override_state,

            "prefer_channel_art": PREFER_CHANNEL_ART,
            "cover_art_url": self._cover_art_url,
            "channel_art_url": self._channel_art_url,

            "observed_keys": sorted(self._observed_keys),
            "last_watch_utc": self._last_watch_dt.isoformat() if self._last_watch_dt else None,
            "last_watch_key": self._last_watch_key,
            "last_watch_age_s": last_watch_age_s,

            "last_rx_age_s": rx_age_s,
            "inactivity_disconnect_s": INACTIVITY_DISCONNECT_S,
            "poll_while_playing_enabled": POLL_WHILE_PLAYING_ENABLED,
        }

    # -------------------------
    # Lifecycle
    # -------------------------

    async def async_added_to_hass(self):
        self._main_task = asyncio.create_task(self._io_loop())
        await self._cmd_queue.put(WAKE)

    async def async_will_remove_from_hass(self):
        if self._main_task:
            self._main_task.cancel()
        if self._poll_task:
            self._poll_task.cancel()
        await self._close_connection()

    async def _close_connection(self):
        if self._poll_task:
            self._poll_task.cancel()
            self._poll_task = None

        if self._writer:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except Exception:
                pass

        self._reader = None
        self._writer = None
        self._attr_available = False
        self.async_write_ha_state()

    def _is_connected(self) -> bool:
        return self._writer is not None and (not self._writer.is_closing())

    async def _ensure_connected(self):
        async with self._connect_lock:
            if self._is_connected():
                return

            _LOGGER.info("Connecting to Russound at %s:%s (source %s)", self._ip, self._port, self._source)
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self._ip, self._port),
                timeout=15,
            )
            self._attr_available = True
            self._last_rx_mono = time.monotonic()
            self._last_state_eval_mono = 0.0

            # reset poll timers
            now = time.monotonic()
            self._next_poll_playtime_mono = now
            self._next_poll_playstatus_mono = now
            self._next_poll_meta_mono = now

            # WATCH subscriptions
            self._writer.write(f"WATCH S[{self._source}] ON\r\n".encode())
            self._writer.write(f"WATCH S[{self._source}].playStatus ON\r\n".encode())
            self._writer.write(f"WATCH S[{self._source}].playTime ON\r\n".encode())
            await self._writer.drain()

            # One-shot refresh on connect
            await self._queue_refresh_all()

            # start poll task (only does work while PLAYING)
            if POLL_WHILE_PLAYING_ENABLED and self._poll_task is None:
                self._poll_task = asyncio.create_task(self._poll_loop())

            self.async_write_ha_state()

    async def _queue_refresh_all(self):
        fields = [
            "Support.playStatus",
            "Support.playTime",
            "playStatus",
            "playTime",
            "songName",
            "artistName",
            "albumName",
            "playlistName",
            "channelName",
            "mode",
            "coverArtURL",
            "channelArtURL",
            "trackTime",
            "shuffleMode",
            "repeatMode",
        ]
        for f in fields:
            await self._cmd_queue.put(f"GET S[{self._source}].{f}")

    # -------------------------
    # Main I/O loop
    # -------------------------

    async def _io_loop(self):
        while True:
            try:
                item = await self._cmd_queue.get()

                try:
                    await self._ensure_connected()
                except Exception as err:
                    _LOGGER.error("Russound connect failed: %s (retry in %ss)", err, RECONNECT_DELAY_S)
                    await self._close_connection()
                    await asyncio.sleep(RECONNECT_DELAY_S)
                    continue

                if isinstance(item, str):
                    await self._send_line(item)

                await self._connected_session_loop()

            except asyncio.CancelledError:
                return
            except Exception as err:
                _LOGGER.error("Russound main loop error: %s", err)
                await self._close_connection()
                await asyncio.sleep(RECONNECT_DELAY_S)

    async def _connected_session_loop(self):
        while self._is_connected():
            try:
                try:
                    line = await asyncio.wait_for(self._reader.readline(), timeout=1.0)
                except asyncio.TimeoutError:
                    line = b""

                mono_now = time.monotonic()

                if line:
                    self._last_rx_mono = mono_now
                    text = line.decode(errors="ignore").strip()
                    changed = self._parse_response(text)
                    if changed:
                        self.async_write_ha_state()
                else:
                    # Re-evaluate fallback (no GET)
                    if mono_now - self._last_state_eval_mono >= STATE_EVAL_INTERVAL_S:
                        self._last_state_eval_mono = mono_now
                        if self._evaluate_state_from_song_change():
                            self.async_write_ha_state()

                    # Inactivity disconnect ONLY when idle/off
                    if self._last_rx_mono is not None:
                        idle_like = self._attr_state in (MediaPlayerState.IDLE, MediaPlayerState.OFF)
                        if idle_like and (mono_now - self._last_rx_mono) >= INACTIVITY_DISCONNECT_S:
                            _LOGGER.info(
                                "Disconnecting Russound source %s due to inactivity (%ss)",
                                self._source,
                                INACTIVITY_DISCONNECT_S,
                            )
                            await self._close_connection()
                            return

                # Drain queue quickly
                while not self._cmd_queue.empty() and self._is_connected():
                    item = self._cmd_queue.get_nowait()
                    if item is WAKE:
                        continue
                    if isinstance(item, str):
                        await self._send_line(item)

            except (ConnectionError, OSError) as err:
                _LOGGER.warning("Russound connection lost: %s", err)
                await self._close_connection()
                return
            except asyncio.CancelledError:
                return
            except Exception as err:
                _LOGGER.error("Russound session error: %s", err)
                await self._close_connection()
                return

    async def _send_line(self, cmd: str):
        if not self._is_connected():
            return
        self._writer.write(f"{cmd}\r\n".encode())
        await self._writer.drain()
        await asyncio.sleep(0.01)

    async def _enqueue_connected(self, cmd: str):
        """Queue a command without WAKE (used by poll loop while already connected)."""
        await self._cmd_queue.put(cmd)

    async def _poll_loop(self):
        """
        Lightweight polling while PLAYING to keep MBX updated.
        Does nothing while PAUSED/IDLE/OFF.
        """
        try:
            while True:
                await asyncio.sleep(0.25)

                if not self._is_connected():
                    continue

                if self._attr_state != MediaPlayerState.PLAYING:
                    continue

                now = time.monotonic()

                # playTime -> progress bar
                if now >= self._next_poll_playtime_mono:
                    self._next_poll_playtime_mono = now + POLL_PLAYTIME_INTERVAL_S
                    await self._enqueue_connected(f"GET S[{self._source}].playTime")

                # playStatus -> state stability (some units update only via GET)
                if now >= self._next_poll_playstatus_mono:
                    self._next_poll_playstatus_mono = now + POLL_PLAYSTATUS_INTERVAL_S
                    await self._enqueue_connected(f"GET S[{self._source}].playStatus")

                # metadata refresh while playing
                if now >= self._next_poll_meta_mono:
                    self._next_poll_meta_mono = now + POLL_METADATA_INTERVAL_S
                    await self._enqueue_connected(f"GET S[{self._source}].songName")
                    await self._enqueue_connected(f"GET S[{self._source}].artistName")
                    await self._enqueue_connected(f"GET S[{self._source}].coverArtURL")
                    await self._enqueue_connected(f"GET S[{self._source}].channelArtURL")
                    await self._enqueue_connected(f"GET S[{self._source}].trackTime")
        except asyncio.CancelledError:
            return

    # -------------------------
    # Parsing
    # -------------------------

    def _parse_response(self, resp: str) -> bool:
        if not resp:
            return False

        if resp.startswith("E "):
            _LOGGER.debug("RIO error ignored: %s", resp)
            return False

        msg_type = None
        payload = resp

        if len(resp) > 2 and resp[1] == " " and resp[0] in ("N", "S"):
            msg_type = resp[0]
            payload = resp[2:]

        if "=" not in payload:
            return False

        key, rhs = payload.split("=", 1)
        val = rhs.replace('"', "").strip()
        key_upper = key.upper()

        # Debug watch info
        if msg_type:
            self._observed_keys.add(f"{msg_type} {key}")
            if msg_type == "N":
                self._last_watch_dt = dt_util.utcnow()
                self._last_watch_key = key
        else:
            self._observed_keys.add(key)

        # Support flags
        if ".SUPPORT." in key_upper and key_upper.endswith(".PLAYTIME"):
            b = _as_bool(val)
            if b is not None and b != self._support_playtime:
                self._support_playtime = b
                return True
            return False

        if ".SUPPORT." in key_upper and key_upper.endswith(".PLAYSTATUS"):
            b = _as_bool(val)
            if b is not None and b != self._support_playstatus:
                self._support_playstatus = b
                return True
            return False

        # Transport record (real keys only)
        if self._is_real_transport_key(key_upper, "PLAYSTATUS") or self._is_real_transport_key(key_upper, "PLAYTIME"):
            self._record_transport_value(msg_type, key_upper, val)

        # playStatus
        if self._is_real_transport_key(key_upper, "PLAYSTATUS"):
            before_usable = self._playstatus_usable
            changed = self._apply_playstatus(val)
            if self._playstatus_usable and not before_usable:
                self._clear_manual_override("playStatus_became_usable")
            return changed

        # playTime -> update media_position
        if self._is_real_transport_key(key_upper, "PLAYTIME"):
            old_raw = self._raw_playtime
            self._raw_playtime = val

            # Also update HA progress fields
            try:
                pt = int(val)
                old_pos = self._attr_media_position
                self._attr_media_position = pt
                self._attr_media_position_updated_at = dt_util.utcnow()
                return (old_raw != self._raw_playtime) or (old_pos != self._attr_media_position)
            except Exception:
                return old_raw != self._raw_playtime

        # songName
        if key_upper.endswith(".SONGNAME"):
            changed_any = False

            old_title = self._attr_media_title
            self._attr_media_title = val or None
            if self._attr_media_title != old_title:
                changed_any = True

            if val and val != self._last_songname_value:
                self._last_songname_value = val
                self._last_songname_change_utc = dt_util.utcnow()
                changed_any = True

                self._clear_manual_override("song_changed")
                if self._evaluate_state_from_song_change():
                    changed_any = True

            return changed_any

        # artist
        if key_upper.endswith(".ARTISTNAME"):
            old = self._attr_media_artist
            self._attr_media_artist = val or None
            return self._attr_media_artist != old

        # cover art
        if key_upper.endswith(".COVERARTURL"):
            old_cover = self._cover_art_url
            self._cover_art_url = val if val.startswith("http") else None
            chosen_old = self._attr_media_image_url
            self._attr_media_image_url = self._choose_art_url()
            return (self._cover_art_url != old_cover) or (self._attr_media_image_url != chosen_old)

        # channel art
        if key_upper.endswith(".CHANNELARTURL"):
            old_chan = self._channel_art_url
            self._channel_art_url = val if val.startswith("http") else None
            chosen_old = self._attr_media_image_url
            self._attr_media_image_url = self._choose_art_url()
            return (self._channel_art_url != old_chan) or (self._attr_media_image_url != chosen_old)

        # provider
        if key_upper.endswith(".MODE"):
            old = self._streaming_provider
            self._streaming_provider = val or "Unknown"
            return self._streaming_provider != old

        # duration
        if key_upper.endswith(".TRACKTIME"):
            old = self._attr_media_duration
            try:
                self._attr_media_duration = int(val)
            except Exception:
                self._attr_media_duration = None
            return self._attr_media_duration != old

        # shuffle
        if key_upper.endswith(".SHUFFLEMODE"):
            old = self._attr_shuffle
            falsy = {"OFF", "0", "FALSE", "DISABLED", "NO", ""}
            truthy = {"ON", "1", "TRUE", "ENABLED", "YES"}
            v = val.upper()
            if v in truthy:
                self._attr_shuffle = True
            elif v in falsy:
                self._attr_shuffle = False
            else:
                self._attr_shuffle = True
            return self._attr_shuffle != old

        # repeat
        if key_upper.endswith(".REPEATMODE"):
            old = self._attr_repeat_mode
            self._attr_repeat_mode = val or "OFF"
            return self._attr_repeat_mode != old

        return False

    # -------------------------
    # Command helpers
    # -------------------------

    async def _enqueue(self, cmd: str):
        await self._cmd_queue.put(WAKE)
        await self._cmd_queue.put(cmd)

    async def _send_event(self, event: str):
        await self._enqueue(f"EVENT S[{self._source}]!KeyRelease {event}")
        # One-shot refresh after command
        await self._enqueue(f"GET S[{self._source}].playStatus")
        await self._enqueue(f"GET S[{self._source}].playTime")
        await self._enqueue(f"GET S[{self._source}].songName")
        await self._enqueue(f"GET S[{self._source}].artistName")
        await self._enqueue(f"GET S[{self._source}].coverArtURL")
        await self._enqueue(f"GET S[{self._source}].channelArtURL")

    # -------------------------
    # MediaPlayer actions
    # -------------------------

    async def async_media_play(self):
        self._set_state_manual(MediaPlayerState.PLAYING)
        await self._send_event("Play")

    async def async_media_pause(self):
        self._set_state_manual(MediaPlayerState.PAUSED)
        await self._send_event("Pause")

    async def async_media_stop(self):
        self._set_state_manual(MediaPlayerState.IDLE)
        await self._send_event("Stop")

    async def async_media_next_track(self):
        self._set_state_manual(MediaPlayerState.PLAYING)
        await self._send_event("Next")

    async def async_media_previous_track(self):
        self._set_state_manual(MediaPlayerState.PLAYING)
        await self._send_event("Previous")

    async def async_set_shuffle(self, shuffle: bool):
        # Source-mode shuffle is a toggle via key event
        await self._send_event("Shuffle")

    async def async_media_seek(self, position: float):
        await self._enqueue(f"EVENT S[{self._source}]!SetSeekTime {int(position)}")
        self._attr_media_position = int(position)
        self._attr_media_position_updated_at = dt_util.utcnow()
        self.async_write_ha_state()
