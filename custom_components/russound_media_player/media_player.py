import asyncio
import json
import logging
import re
import time
from typing import Optional, Tuple, Dict, Any, List, Set

from homeassistant.components.media_player import (
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
)
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.util import dt as dt_util
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import device_registry as dr

from .const import (
    DOMAIN,
    DEFAULT_PORT_PRIMARY,
    DEFAULT_PORT_FALLBACK,
    DEFAULT_SCAN_MAX,
    OPT_DISCOVERED_SOURCES,
    OPT_LAST_DISCOVERY_MS,
)

_LOGGER = logging.getLogger(__name__)

# ---- Tuning ----
PROBE_INTERVAL_S = 15.0
PROBE_CONNECT_TIMEOUT_S = 2.0
PROBE_READ_WINDOW_S = 1.0

ACTIVE_READ_TIMEOUT_S = 0.4
REFRESH_INTERVAL_PLAYING_S = 8.0
REFRESH_INTERVAL_IDLE_S = 30.0
RECONNECT_DELAY_S = 10.0

MANUAL_STATE_HOLD_SECONDS = 10.0
LEGACY_VERSION_CUTOFF = (1, 14, 0)

PAUSED_TO_IDLE_SECONDS = 120.0

FIELDS = [
    "playStatus",
    "playTime",
    "trackTime",
    "songName",
    "artistName",
    "coverArtURL",
    "mode",
    "shuffleMode",
    "repeatMode",
    "playlistName",
]


def _decode_value(rhs: str) -> str:
    s = (rhs or "").strip()
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        try:
            v = json.loads(s)
            return v if isinstance(v, str) else str(v)
        except Exception:
            return s[1:-1]
    return s


def _safe_host_key(host: str) -> str:
    return (host or "").replace(".", "_").replace(":", "_")


def _source_unique_id(streamer_host: str, source: int) -> str:
    # Som du ønskede: russound_<streamerip>_source_<i>
    return f"russound_{_safe_host_key(streamer_host)}_source_{source}"


def _source_device_identifier(streamer_host: str, source: int) -> str:
    # device registry identifier (stabilt, uden port)
    return f"{streamer_host}_source_{source}"


async def _probe_port(host: str, port: int, timeout: float = 1.0) -> bool:
    reader = writer = None
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=timeout)
        writer.write(b"VERSION\r\n")
        await writer.drain()

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                raw = await asyncio.wait_for(reader.readline(), timeout=0.25)
            except asyncio.TimeoutError:
                continue
            if not raw:
                break
            line = raw.decode(errors="ignore").strip()
            if not line or line.startswith("E "):
                continue
            payload = line[2:] if (len(line) > 2 and line[1] == " " and line[0] in ("N", "S")) else line
            if payload.upper().startswith("VERSION"):
                return True
        return False
    except Exception:
        return False
    finally:
        if writer:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass


async def _select_controller_port(host: str, preferred_port: Optional[int]) -> Optional[int]:
    """
    Controller: prøv først valgt port, ellers 9622 -> 9621.
    """
    tried: Set[int] = set()
    if preferred_port:
        tried.add(preferred_port)
        if await _probe_port(host, preferred_port, timeout=1.5):
            return preferred_port

    for p in (DEFAULT_PORT_FALLBACK, DEFAULT_PORT_PRIMARY):  # 9622 -> 9621
        if p in tried:
            continue
        if await _probe_port(host, p, timeout=1.5):
            return p
    return None


async def _select_streamer_port(host: str) -> Optional[int]:
    """
    Streamer: prøv 9621 -> 9622 (MBX streamer svarer typisk kun på 9621).
    """
    if await _probe_port(host, DEFAULT_PORT_PRIMARY):
        return DEFAULT_PORT_PRIMARY
    if await _probe_port(host, DEFAULT_PORT_FALLBACK):
        return DEFAULT_PORT_FALLBACK
    return None


async def _discover_streamer_sources(controller_host: str, controller_port: int, scan_max: int) -> List[Dict[str, Any]]:
    """
    Ask the controller for S[1..scan_max].type/ipAddress/name and return only sources
    whose type contains EXACT "Russound Media Streamer".
    """
    reader = writer = None
    sources: Dict[int, Dict[str, str]] = {i: {} for i in range(1, scan_max + 1)}
    req_fields = ("type", "ipAddress", "name")

    try:
        reader, writer = await asyncio.open_connection(controller_host, controller_port)

        for i in range(1, scan_max + 1):
            for f in req_fields:
                writer.write(f"GET S[{i}].{f}\r\n".encode())
        await writer.drain()

        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            try:
                raw = await asyncio.wait_for(reader.readline(), timeout=0.25)
            except asyncio.TimeoutError:
                continue
            if not raw:
                break

            line = raw.decode(errors="ignore").strip()
            if not line or line.startswith("E "):
                continue

            payload = line[2:] if (len(line) > 2 and line[1] == " " and line[0] in ("N", "S")) else line
            if "=" not in payload:
                continue

            key, rhs = payload.split("=", 1)
            key = key.strip()
            val = _decode_value(rhs).strip()

            m = re.match(r"^S\[(\d+)\]\.(.+)$", key, flags=re.IGNORECASE)
            if not m:
                continue

            idx = int(m.group(1))
            if idx < 1 or idx > scan_max:
                continue

            field = m.group(2).strip().lower()
            sources[idx][field] = val

        discovered: List[Dict[str, Any]] = []
        for i in range(1, scan_max + 1):
            t = (sources[i].get("type") or "").strip()
            if "RUSSOUND MEDIA STREAMER" not in t.upper():
                continue

            ip_raw = (sources[i].get("ipaddress") or "").strip()
            name = (sources[i].get("name") or "").strip()

            # Brug controllerens navn (selv "Source 7" osv er OK hvis den faktisk er konfigureret streamer)
            if not name:
                name = f"Source {i}"

            host = controller_host if ip_raw.lower() in ("", "localhost", "127.0.0.1") else ip_raw

            port = await _select_streamer_port(host)
            if port is None:
                _LOGGER.warning(
                    "Discovered streamer source %s (%s) at %s but no RIO port responded",
                    i,
                    name,
                    host,
                )
                continue

            discovered.append(
                {
                    "source": i,
                    "name": name,
                    "host": host,
                    "port": port,
                    "ip_raw": ip_raw,
                    "type": t,
                }
            )

        return discovered

    finally:
        if writer:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass


def _prune_stale_registry(
    hass,
    config_entry,
    desired_unique_ids: Set[str],
    desired_device_identifiers: Set[Tuple[str, str]],
) -> None:
    """
    Kun når controller er online og vi har frisk discovery:
    - fjern entities i registry som ikke længere findes (unique_id ikke i discovered)
    - fjern devices uden entities, som ikke længere findes
    """
    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)

    # 1) Entity cleanup
    for entry in list(ent_reg.entities.values()):
        if entry.config_entry_id != config_entry.entry_id:
            continue
        if entry.platform != DOMAIN:
            continue
        if entry.unique_id not in desired_unique_ids:
            _LOGGER.warning("Removing stale entity from registry: %s (unique_id=%s)", entry.entity_id, entry.unique_id)
            ent_reg.async_remove(entry.entity_id)

    # 2) Device cleanup (kun devices uden entities)
    for device in list(dev_reg.devices.values()):
        if config_entry.entry_id not in device.config_entries:
            continue

        our_identifiers = {i for i in device.identifiers if i[0] in (DOMAIN, "russound")}
        if not our_identifiers:
            continue

        if er.async_entries_for_device(ent_reg, device.id):
            continue

        if any(i in desired_device_identifiers for i in our_identifiers):
            continue

        if len(device.config_entries) > 1:
            continue

        _LOGGER.warning("Removing stale device from registry: %s identifiers=%s", device.name, list(our_identifiers))
        dev_reg.async_remove_device(device.id)


async def async_setup_entry(hass, config_entry, async_add_entities):
    """
    Controller entry -> discover streamer sources and create one entity per source.

    Hvis controller er nede, falder vi tilbage til cached discovered_sources.
    Oprydning (sletning af gamle sources) sker kun når controlleren er online.
    """
    cfg = config_entry.data
    controller_host = cfg[CONF_HOST]
    preferred_port = cfg.get(CONF_PORT)

    hass.data.setdefault(DOMAIN, {}).setdefault(config_entry.entry_id, {})

    controller_port = await _select_controller_port(controller_host, preferred_port)
    controller_available = controller_port is not None

    discovered: List[Dict[str, Any]] = []
    discovery_ms = None

    if controller_available and controller_port is not None:
        t0 = time.monotonic()
        discovered = await _discover_streamer_sources(controller_host, controller_port, DEFAULT_SCAN_MAX)
        discovery_ms = int((time.monotonic() - t0) * 1000)

        opts = dict(config_entry.options)
        opts[OPT_DISCOVERED_SOURCES] = discovered
        opts[OPT_LAST_DISCOVERY_MS] = discovery_ms
        hass.config_entries.async_update_entry(config_entry, options=opts)

        desired_unique_ids = {_source_unique_id(s["host"], int(s["source"])) for s in discovered}
        desired_device_identifiers = {(DOMAIN, _source_device_identifier(s["host"], int(s["source"]))) for s in discovered}
        _prune_stale_registry(hass, config_entry, desired_unique_ids, desired_device_identifiers)

    if not discovered:
        cached = list(config_entry.options.get(OPT_DISCOVERED_SOURCES, []) or [])
        if cached:
            discovered = cached
            discovery_ms = config_entry.options.get(OPT_LAST_DISCOVERY_MS)
            _LOGGER.warning(
                "Controller %s unavailable or returned no sources; using cached discovery (%s sources)",
                controller_host,
                len(discovered),
            )
        else:
            _LOGGER.error("No controller connection and no cached discovery for %s", controller_host)
            return True

    entities = []
    for s in discovered:
        host = s["host"]
        source = int(s["source"])
        ent = RussoundSourceEntity(
            host=host,
            port=int(s["port"]),
            source=source,
            name=s["name"],
            controller_host=controller_host,
            controller_port=int(controller_port or preferred_port or 0),
            controller_available=controller_available,
            discovery_duration_ms=int(discovery_ms) if discovery_ms is not None else None,
            discovered_ip_raw=s.get("ip_raw"),
            discovered_type=s.get("type"),
        )
        entities.append(ent)

    if entities:
        async_add_entities(entities)

    return True


class RussoundSourceEntity(MediaPlayerEntity):
    _attr_should_poll = False

    def __init__(
        self,
        host: str,
        port: int,
        source: int,
        name: str,
        controller_host: str,
        controller_port: int,
        controller_available: bool,
        discovery_duration_ms: Optional[int],
        discovered_ip_raw: Optional[str] = None,
        discovered_type: Optional[str] = None,
    ):
        self._host, self._port, self._source = host, port, source

        self._controller_host = controller_host
        self._controller_port = controller_port
        self._controller_available = controller_available
        self._discovery_duration_ms = discovery_duration_ms
        self._discovered_ip_raw = discovered_ip_raw
        self._discovered_type = discovered_type

        self._attr_name = name
        self._attr_unique_id = _source_unique_id(host, source)

        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._cmd_q: asyncio.Queue[str] = asyncio.Queue()

        self._task_main: Optional[asyncio.Task] = None
        self._task_probe: Optional[asyncio.Task] = None
        self._task_refresh: Optional[asyncio.Task] = None
        self._task_pause_to_idle: Optional[asyncio.Task] = None

        self._version_raw: Optional[str] = None
        self._version_tuple: Optional[Tuple[int, int, int]] = None
        self._is_legacy: Optional[bool] = None

        self._system_status: Optional[str] = None
        self._awake = False
        self._active_event = asyncio.Event()

        self._playlist_name: Optional[str] = None
        self._playlist_changed_utc = None
        self._paused_reason: Optional[str] = None
        self._paused_since_utc = None

        self._manual_hold_until = 0.0
        self._observed_keys: set[str] = set()

        # ✅ NYT: hvis playStatus er tom streng på denne streamer, så brug playlist-transport som fallback
        self._playstatus_blank_seen: bool = False

        self._attr_available = False
        self._attr_state = MediaPlayerState.OFF

        self._streaming_provider = "Unknown"
        self._attr_media_title = None
        self._attr_media_artist = None
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
            "identifiers": {(DOMAIN, _source_device_identifier(host, source))},
            "name": name,
            "manufacturer": "Russound",
            "model": "Russound Media Streamer (Source RIO)",
        }

    # ---------- decoding ----------
    @staticmethod
    def _fix_mojibake(text: str) -> str:
        if not text:
            return text
        if "Ã" not in text and "Â" not in text:
            return text
        try:
            return text.encode("latin-1", errors="strict").decode("utf-8", errors="strict")
        except Exception:
            return text

    @classmethod
    def _decode_line(cls, raw: bytes) -> str:
        if not raw:
            return ""
        for enc in ("utf-8", "cp1252"):
            try:
                return cls._fix_mojibake(raw.decode(enc, errors="strict").strip())
            except UnicodeDecodeError:
                continue
        return cls._fix_mojibake(raw.decode("latin-1", errors="replace").strip())

    @classmethod
    def _decode_value(cls, rhs: str) -> str:
        s = (rhs or "").strip()
        if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
            try:
                return cls._fix_mojibake(json.loads(s))
            except Exception:
                return cls._fix_mojibake(s[1:-1])
        return cls._fix_mojibake(s)

    # ---------- helpers ----------
    def _connected(self) -> bool:
        return self._writer is not None and (not self._writer.is_closing())

    def _manual_hold(self) -> bool:
        return dt_util.as_timestamp(dt_util.utcnow()) < self._manual_hold_until

    def _set_state_manual(self, st: MediaPlayerState):
        self._attr_state = st
        self._manual_hold_until = dt_util.as_timestamp(dt_util.utcnow()) + MANUAL_STATE_HOLD_SECONDS
        self.async_write_ha_state()

    def _parse_version_tuple(self, line: str) -> Optional[Tuple[int, int, int]]:
        m = re.search(r"(\d{2})\.(\d{2})\.(\d{2})", line or "")
        if not m:
            return None
        return (int(m.group(1)), int(m.group(2)), int(m.group(3)))

    def _update_version(self, line: str) -> bool:
        changed = False
        if line and line != self._version_raw:
            self._version_raw = line
            changed = True
        vt = self._parse_version_tuple(line)
        if vt and vt != self._version_tuple:
            self._version_tuple = vt
            changed = True
        if vt:
            legacy = vt < LEGACY_VERSION_CUTOFF
            if legacy != self._is_legacy:
                self._is_legacy = legacy
                changed = True
        return changed

    def _set_awake(self, awake: bool) -> bool:
        if awake == self._awake:
            return False
        self._awake = awake
        if awake:
            self._active_event.set()
            if self._attr_state == MediaPlayerState.OFF:
                self._attr_state = MediaPlayerState.IDLE
        else:
            self._active_event.clear()
            if not self._manual_hold():
                self._attr_state = MediaPlayerState.OFF
        return True

    # ---------- legacy pause->idle ----------
    def _cancel_pause_to_idle(self):
        if self._task_pause_to_idle:
            self._task_pause_to_idle.cancel()
            self._task_pause_to_idle = None
        self._paused_reason = None
        self._paused_since_utc = None

    def _schedule_pause_to_idle(self, seconds: float, reason: str):
        self._cancel_pause_to_idle()
        self._paused_reason = reason
        self._paused_since_utc = dt_util.utcnow()
        start_dt = self._paused_since_utc

        async def _run():
            try:
                await asyncio.sleep(seconds)
                if self._manual_hold():
                    return
                if self._attr_state != MediaPlayerState.PAUSED:
                    return
                if self._playlist_changed_utc and self._playlist_changed_utc > start_dt:
                    return
                self._attr_state = MediaPlayerState.IDLE
                self._cancel_pause_to_idle()
                self.async_write_ha_state()
            except asyncio.CancelledError:
                return

        self._task_pause_to_idle = asyncio.create_task(_run())

    def _apply_playlist_transport(self) -> bool:
        # Brug legacy transport hvis legacy firmware ELLER playStatus er ubrugelig
        if not (bool(self._is_legacy) or self._playstatus_blank_seen) or self._manual_hold():
            return False

        pn = (self._playlist_name or "").strip()
        up = pn.upper()

        title = (self._attr_media_title or "").strip()
        title_up = title.upper()

        # --- Placeholder / "status" tekster der IKKE betyder playback ---
        placeholder_playlist_tokens = (
            "PLEASE WAIT",        # "Please Wait..."
        )
        placeholder_title_tokens = (
            "CONNECTING TO MEDIA SOURCE",  # "Connecting to media source."
            "PLEASE MAKE A SELECTION",     # klassiker når intet spiller
            "PLEASE WAIT",
        )

        def _is_placeholder() -> bool:
            if any(tok in up for tok in placeholder_playlist_tokens):
                return True
            if any(tok in title_up for tok in placeholder_title_tokens):
                return True
            return False

        # Tom / STOPPED => IDLE
        if not pn or up in {"STOPPED", "STOP"}:
            self._cancel_pause_to_idle()
            if self._attr_state != MediaPlayerState.IDLE:
                self._attr_state = MediaPlayerState.IDLE
                return True
            return False

        # PAUSED => PAUSED (+ evt timer til IDLE)
        if up in {"PAUSED", "PAUSE"}:
            if self._attr_state != MediaPlayerState.PAUSED:
                self._attr_state = MediaPlayerState.PAUSED
                self._schedule_pause_to_idle(PAUSED_TO_IDLE_SECONDS, "playlist_paused")
                return True
            self._schedule_pause_to_idle(PAUSED_TO_IDLE_SECONDS, "playlist_paused")
            return False

        # Waiting for Spotify Connect => IDLE (direkte)
        if "WAITING FOR SPOTIFY CONNECT" in up or "VENTER PÅ SPOTIFY CONNECT" in up:
            self._cancel_pause_to_idle()
            if self._attr_state != MediaPlayerState.IDLE:
                self._attr_state = MediaPlayerState.IDLE
                return True
            return False

        # Please Wait / Connecting / Please make a selection => IDLE
        if _is_placeholder():
            self._cancel_pause_to_idle()
            if self._attr_state != MediaPlayerState.IDLE:
                self._attr_state = MediaPlayerState.IDLE
                return True
            return False

        # Ellers (fx "Spotify") => PLAYING, men kun hvis title ikke er tom
        self._cancel_pause_to_idle()
        if title:
            if self._attr_state != MediaPlayerState.PLAYING:
                self._attr_state = MediaPlayerState.PLAYING
                return True
        else:
            # Hvis vi har "Spotify" i playlist men ingen title endnu, så er vi stadig i "mellem-state"
            if self._attr_state != MediaPlayerState.IDLE:
                self._attr_state = MediaPlayerState.IDLE
                return True

        return False




    # ---------- HA ----------
    @property
    def shuffle(self):
        return self._attr_shuffle

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        return {
            "controller_host": self._controller_host,
            "controller_port": self._controller_port,
            "controller_available": self._controller_available,
            "discovery_duration_ms": self._discovery_duration_ms,
            "discovered_ip_raw": self._discovered_ip_raw,
            "discovered_type": self._discovered_type,
            "streamer_host": self._host,
            "streamer_port": self._port,
            "source": self._source,
            "version": self._version_raw,
            "is_legacy": self._is_legacy,
            "system_status": self._system_status,
            "awake": self._awake,
            "streaming_provider": self._streaming_provider,
            "repeat_status": self._attr_repeat_mode,
            "playlist_name": self._playlist_name,
            "paused_reason": self._paused_reason,
            "playstatus_blank_seen": self._playstatus_blank_seen,
            "observed_keys": "\n".join(sorted(self._observed_keys)) if self._observed_keys else "",
        }

    async def async_added_to_hass(self):
        self._task_main = asyncio.create_task(self._bootstrap())

    async def async_will_remove_from_hass(self):
        for t in (self._task_main, self._task_probe, self._task_refresh, self._task_pause_to_idle):
            if t:
                t.cancel()
        await self._close()

    async def _close(self):
        self._cancel_pause_to_idle()
        if self._writer:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except Exception:
                pass
        self._reader = None
        self._writer = None

    # ---------- bootstrap / probe ----------
    async def _bootstrap(self):
        while True:
            try:
                reachable, version_line, system_status = await self._probe_once()
                self._attr_available = reachable
                if version_line:
                    self._update_version(version_line)
                self._system_status = system_status

                if self._is_legacy is not None:
                    break
                await asyncio.sleep(PROBE_INTERVAL_S)
            except asyncio.CancelledError:
                return
            except Exception:
                await asyncio.sleep(PROBE_INTERVAL_S)

        if self._is_legacy:
            self._set_awake(True)
            self._task_refresh = asyncio.create_task(self._refresh_loop())
            await self._active_loop(always_on=True)
        else:
            self._task_probe = asyncio.create_task(self._probe_loop())
            self._task_refresh = asyncio.create_task(self._refresh_loop())
            await self._active_loop(always_on=False)

    async def _probe_loop(self):
        while True:
            try:
                if self._connected():
                    await asyncio.sleep(PROBE_INTERVAL_S)
                    continue

                reachable, version_line, system_status = await self._probe_once()
                changed = False

                if reachable != self._attr_available:
                    self._attr_available = reachable
                    changed = True
                if version_line and self._update_version(version_line):
                    changed = True
                if system_status != self._system_status:
                    self._system_status = system_status
                    changed = True
                if self._set_awake(system_status == "ON"):
                    changed = True

                if changed:
                    self.async_write_ha_state()

                await asyncio.sleep(PROBE_INTERVAL_S)
            except asyncio.CancelledError:
                return
            except Exception:
                await asyncio.sleep(PROBE_INTERVAL_S)

    async def _probe_once(self) -> Tuple[bool, Optional[str], Optional[str]]:
        reader = writer = None
        reachable = False
        version_line = None
        system_status = None

        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self._host, self._port),
                timeout=PROBE_CONNECT_TIMEOUT_S,
            )
            reachable = True

            writer.write(b"VERSION\r\n")
            writer.write(b"GET System.status\r\n")
            await writer.drain()

            deadline = time.monotonic() + PROBE_READ_WINDOW_S
            while time.monotonic() < deadline:
                try:
                    raw = await asyncio.wait_for(reader.readline(), timeout=0.25)
                except asyncio.TimeoutError:
                    continue
                if not raw:
                    break

                text = self._decode_line(raw)
                if not text or text.startswith("E "):
                    continue

                payload = text[2:] if (len(text) > 2 and text[1] == " " and text[0] in ("N", "S")) else text
                up = payload.upper()

                if up.startswith("VERSION"):
                    version_line = payload
                    continue

                if "=" in payload:
                    k, rhs = payload.split("=", 1)
                    if k.strip().upper() == "SYSTEM.STATUS":
                        v = self._decode_value(rhs).strip().upper()
                        system_status = v or None

            return reachable, version_line, system_status
        except Exception:
            return reachable, version_line, system_status
        finally:
            if writer:
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:
                    pass

    # ---------- active session ----------
    async def _connect_active(self):
        self._reader, self._writer = await asyncio.open_connection(self._host, self._port)

        # One WATCH only
        self._writer.write(f"WATCH S[{self._source}] ON\r\n".encode("utf-8"))
        await self._writer.drain()

        await self._queue_refresh()

    async def _active_loop(self, always_on: bool):
        while True:
            try:
                if not always_on:
                    await self._active_event.wait()

                if not self._connected():
                    try:
                        await self._connect_active()
                        self._attr_available = True
                        if self._attr_state == MediaPlayerState.OFF:
                            self._attr_state = MediaPlayerState.IDLE
                        self.async_write_ha_state()
                    except Exception:
                        await self._close()
                        if not always_on:
                            self._set_awake(False)
                            self.async_write_ha_state()
                        await asyncio.sleep(RECONNECT_DELAY_S)
                        continue

                while self._connected() and (always_on or self._active_event.is_set()):
                    try:
                        raw = await asyncio.wait_for(self._reader.readline(), timeout=ACTIVE_READ_TIMEOUT_S)
                    except asyncio.TimeoutError:
                        raw = b""

                    if raw:
                        text = self._decode_line(raw)
                        if self._parse_message(text):
                            self.async_write_ha_state()

                    while self._connected() and (not self._cmd_q.empty()):
                        cmd = self._cmd_q.get_nowait()
                        self._writer.write(f"{cmd}\r\n".encode("utf-8"))
                        await self._writer.drain()
                        await asyncio.sleep(0.01)

                await self._close()
                if not always_on:
                    continue
                await asyncio.sleep(RECONNECT_DELAY_S)

            except asyncio.CancelledError:
                return
            except Exception:
                await self._close()
                await asyncio.sleep(RECONNECT_DELAY_S)

    async def _refresh_loop(self):
        while True:
            try:
                if self._connected() and self._attr_available:
                    interval = (
                        REFRESH_INTERVAL_PLAYING_S
                        if (not self._is_legacy and self._attr_state == MediaPlayerState.PLAYING)
                        else REFRESH_INTERVAL_IDLE_S
                    )
                    await self._queue_refresh()
                    await asyncio.sleep(interval)
                else:
                    await asyncio.sleep(REFRESH_INTERVAL_IDLE_S)
            except asyncio.CancelledError:
                return
            except Exception:
                await asyncio.sleep(REFRESH_INTERVAL_IDLE_S)

    async def _queue_refresh(self):
        await self._cmd_q.put("VERSION")
        await self._cmd_q.put("GET System.status")
        for f in FIELDS:
            await self._cmd_q.put(f"GET S[{self._source}].{f}")

    def _parse_message(self, text: str) -> bool:
        if not text or text.startswith("E "):
            return False

        payload = text[2:] if (len(text) > 2 and text[1] == " " and text[0] in ("N", "S")) else text

        up = payload.upper()
        if up.startswith("VERSION"):
            return self._update_version(payload)

        if "=" not in payload:
            return False

        key, rhs = payload.split("=", 1)
        key = key.strip()
        key_u = key.upper()

        val = self._decode_value(rhs)
        val_u = val.upper()

        self._observed_keys.add(key)

        if key_u == "SYSTEM.STATUS":
            new = val_u or None
            if new != self._system_status:
                self._system_status = new
                return True
            return False

        # ---- PLAYSTATUS: hvis den er tom, så switch til playlist-transport ----
        if key_u.endswith(".PLAYSTATUS") and (not self._manual_hold()):
            # Tom streng => playStatus er ubrugelig på denne streamer
            if not (val or "").strip():
                changed = False
                if not self._playstatus_blank_seen:
                    self._playstatus_blank_seen = True
                    changed = True
                # forsøg at anvende playlist transport straks (hvis vi allerede har playlist/title)
                if self._apply_playlist_transport():
                    changed = True
                return changed

            if "PLAYING" in val_u:
                if self._attr_state != MediaPlayerState.PLAYING:
                    self._attr_state = MediaPlayerState.PLAYING
                    return True
                return False

            if "PAUSED" in val_u:
                if self._attr_state != MediaPlayerState.PAUSED:
                    self._attr_state = MediaPlayerState.PAUSED
                    return True
                return False

            # Hvis vi har set blank playStatus før, så stoler vi ikke på STOP/IDLE her (playlist styrer)
            if ("STOP" in val_u or "IDLE" in val_u) and not self._playstatus_blank_seen:
                if self._attr_state != MediaPlayerState.IDLE:
                    self._attr_state = MediaPlayerState.IDLE
                    return True
                return False

            return False

        if key_u.endswith(".PLAYTIME"):
            try:
                pos = int(val) if val else None
            except Exception:
                pos = None
            if pos is not None:
                self._attr_media_position = pos
                self._attr_media_position_updated_at = dt_util.utcnow()
                return True
            return False

        if key_u.endswith(".TRACKTIME"):
            try:
                dur = int(val) if val else None
            except Exception:
                dur = None
            if dur is not None and dur != self._attr_media_duration:
                self._attr_media_duration = dur
                return True
            return False

        if key_u.endswith(".PLAYLISTNAME"):
            new = val or None
            changed = False
            if new != self._playlist_name:
                self._playlist_name = new
                self._playlist_changed_utc = dt_util.utcnow()
                changed = True
            if self._apply_playlist_transport():
                changed = True
            return changed

        changed = False

        if key_u.endswith(".SONGNAME"):
            new = val or None
            if new != self._attr_media_title:
                self._attr_media_title = new
                changed = True
            if self._apply_playlist_transport():
                changed = True

        elif key_u.endswith(".ARTISTNAME"):
            new = val or None
            if new != self._attr_media_artist:
                self._attr_media_artist = new
                changed = True

        elif key_u.endswith(".COVERARTURL"):
            new = val if val.startswith("http") else None
            if new != self._attr_media_image_url:
                self._attr_media_image_url = new
                changed = True

        elif key_u.endswith(".MODE"):
            new = val or "Unknown"
            if new != self._streaming_provider:
                self._streaming_provider = new
                changed = True

        elif key_u.endswith(".SHUFFLEMODE"):
            new = ("ON" in val_u) or (val_u in {"1", "TRUE", "YES", "ENABLED"})
            if new != self._attr_shuffle:
                self._attr_shuffle = new
                changed = True

        elif key_u.endswith(".REPEATMODE"):
            new = val or "OFF"
            if new != self._attr_repeat_mode:
                self._attr_repeat_mode = new
                changed = True

        return changed

    # ---------- commands ----------
    async def _send_event(self, key: str):
        if not self._connected():
            return
        await self._cmd_q.put(f"EVENT S[{self._source}]!KeyRelease {key}")
        await asyncio.sleep(0.25)
        await self._queue_refresh()

    async def async_media_play(self):
        self._set_state_manual(MediaPlayerState.PLAYING)
        self._cancel_pause_to_idle()
        await self._send_event("Play")

    async def async_media_pause(self):
        self._set_state_manual(MediaPlayerState.PAUSED)
        await self._send_event("Pause")

    async def async_media_stop(self):
        self._set_state_manual(MediaPlayerState.IDLE)
        self._cancel_pause_to_idle()
        await self._send_event("Stop")

    async def async_media_next_track(self):
        self._set_state_manual(MediaPlayerState.PLAYING)
        self._cancel_pause_to_idle()
        await self._send_event("Next")

    async def async_media_previous_track(self):
        self._set_state_manual(MediaPlayerState.PLAYING)
        self._cancel_pause_to_idle()
        await self._send_event("Previous")

    async def async_set_shuffle(self, shuffle: bool):
        await self._send_event("Shuffle")

    async def async_media_seek(self, position: float):
        if not self._connected():
            return
        await self._cmd_q.put(f"EVENT S[{self._source}]!SetSeekTime {int(position)}")
        self._attr_media_position = int(position)
        self._attr_media_position_updated_at = dt_util.utcnow()
        self.async_write_ha_state()
