import asyncio
import logging
from homeassistant.components.media_player import (
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState
)
from homeassistant.const import CONF_HOST, CONF_PORT, CONF_SOURCE, CONF_NAME
from homeassistant.util import dt as dt_util

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(hass, config_entry, async_add_entities):
    """Set up the Russound platform from a config entry."""
    config = config_entry.data
    entity = RussoundSourceEntity(
        config[CONF_HOST], 
        config[CONF_PORT], 
        config[CONF_SOURCE], 
        config.get(CONF_NAME, "Russound Media Player")
    )
    async_add_entities([entity])

class RussoundSourceEntity(MediaPlayerEntity):
    """Russound Entity with fallback logic for legacy firmware."""
    
    _attr_should_poll = False 

    def __init__(self, ip, port, source, display_name):
        """Initialize the entity."""
        self._ip, self._port, self._source = ip, port, source
        self._attr_name = display_name 
        self._attr_unique_id = f"russound_{ip.replace('.', '_')}_source_{source}"
        
        self._reader = self._writer = self._main_task = self._heartbeat_task = None
        self._cmd_queue = asyncio.Queue()
        self._ignore_updates_until = 0
        
        # State attributes
        self._streaming_provider = "Unknown"
        self._attr_state = MediaPlayerState.OFF
        self._attr_media_title = self._attr_media_artist = self._attr_media_image_url = None
        self._attr_media_duration = self._attr_media_position = self._attr_media_position_updated_at = None
        self._attr_shuffle = False
        self._attr_repeat_mode = "OFF"
        
        # Supported features (Value: 53299)
        self._attr_supported_features = (
            MediaPlayerEntityFeature.PLAY | MediaPlayerEntityFeature.PAUSE | 
            MediaPlayerEntityFeature.STOP | MediaPlayerEntityFeature.NEXT_TRACK | 
            MediaPlayerEntityFeature.PREVIOUS_TRACK | MediaPlayerEntityFeature.SEEK | 
            MediaPlayerEntityFeature.SHUFFLE_SET
        )

        self._attr_device_info = {
            "identifiers": {("russound", f"{ip}_{source}")},
            "name": display_name,
            "manufacturer": "Russound",
            "model": "Russound Media Player"
        }

    @property
    def shuffle(self):
        return self._attr_shuffle

    @property
    def extra_state_attributes(self):
        return {
            "streaming_provider": self._streaming_provider, 
            "repeat_status": self._attr_repeat_mode
        }

    async def async_added_to_hass(self):
        """Start tasks when entity is added."""
        self._main_task = asyncio.create_task(self._io_loop())
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

    async def async_will_remove_from_hass(self):
        """Cleanup when entity is removed."""
        if self._main_task: self._main_task.cancel()
        if self._heartbeat_task: self._heartbeat_task.cancel()
        await self._close_connection()

    async def _close_connection(self):
        """Close TCP connection."""
        if self._writer:
            self._writer.close()
            try: await self._writer.wait_closed()
            except: pass
        self._reader = self._writer = None
        self._attr_available = False
        self.async_write_ha_state()

    async def _queue_metadata_refresh(self):
        """Request metadata updates."""
        fields = ["playStatus", "songName", "artistName", "mode", "coverArtURL", 
                  "playTime", "trackTime", "shuffleMode", "repeatMode"]
        for field in fields:
            await self._cmd_queue.put(f"GET S[{self._source}].{field}")

    async def _heartbeat_loop(self):
        """Periodic sync while playing."""
        while True:
            try:
                if self._attr_available and self._attr_state == MediaPlayerState.PLAYING:
                    await self._queue_metadata_refresh()
            except Exception: pass
            await asyncio.sleep(2)

    async def _io_loop(self):
        """Main communication loop."""
        while True:
            try:
                _LOGGER.info("Connecting to Russound at %s:%s", self._ip, self._port)
                self._reader, self._writer = await asyncio.wait_for(
                    asyncio.open_connection(self._ip, self._port), timeout=15
                )
                self._attr_available = True
                
                # Subscribe to source updates
                self._writer.write(f"WATCH S[{self._source}] ON\r\n".encode())
                await self._writer.drain()
                await self._queue_metadata_refresh()

                while True:
                    try:
                        line = await asyncio.wait_for(self._reader.readline(), timeout=0.1)
                        if not line: break 
                        self._parse_response(line.decode().strip())
                        self.async_write_ha_state()
                    except asyncio.TimeoutError: pass

                    while not self._cmd_queue.empty():
                        cmd = self._cmd_queue.get_nowait()
                        if self._writer:
                            self._writer.write(f"{cmd}\r\n".encode())
                            await self._writer.drain()
                            await asyncio.sleep(0.01)

            except Exception as err:
                _LOGGER.error("Russound connection lost: %s. Retrying in 30s...", err)
                self._attr_available = False
                await self._close_connection()
                await asyncio.sleep(30)

    def _parse_response(self, resp):
        """Parse RIO responses with fallback for legacy firmware."""
        # Handle error messages from older firmware (e.g., VERSION 01.11.00)
        if resp.startswith("E "):
            _LOGGER.debug("Legacy firmware error ignored: %s", resp)
            return
            
        if "=" not in resp: 
            return
            
        raw_upper = resp.upper()
        parts = resp.split('=', 1)
        val = parts[1].replace('"', '').strip()
        val_upper = val.upper()

        if "SONGNAME" in raw_upper: 
            self._attr_media_title = val
            # FALLBACK: If song name is provided, assume it's playing 
            # as legacy units may not support playStatus.
            if self._attr_state != MediaPlayerState.PLAYING:
                self._attr_state = MediaPlayerState.PLAYING
                
        elif "ARTISTNAME" in raw_upper: 
            self._attr_media_artist = val
            
        elif "COVERARTURL" in raw_upper:
            self._attr_media_image_url = val if val.startswith("http") else None

        elif "PLAYSTATUS" in raw_upper:
            if "PLAYING" in val_upper: self._attr_state = MediaPlayerState.PLAYING
            elif "PAUSED" in val_upper: self._attr_state = MediaPlayerState.PAUSED
            else: self._attr_state = MediaPlayerState.IDLE

        elif "SHUFFLEMODE" in raw_upper:
            self._attr_shuffle = ("ON" in val_upper)
            
        elif "REPEATMODE" in raw_upper:
            self._attr_repeat_mode = val
            
        elif ".MODE" in raw_upper:
            self._streaming_provider = val
            
        elif "PLAYTIME" in raw_upper:
            now = dt_util.as_timestamp(dt_util.utcnow())
            if now > self._ignore_updates_until:
                try:
                    self._attr_media_position = int(val)
                    self._attr_media_position_updated_at = dt_util.utcnow()
                except: pass            
                
        elif "TRACKTIME" in raw_upper:
            try: self._attr_media_duration = int(val)
            except: pass

    async def _send_event(self, event):
        """Send key events to the device."""
        await self._cmd_queue.put(f"EVENT S[{self._source}]!KeyRelease {event}")
        await asyncio.sleep(0.5)
        await self._queue_metadata_refresh()

    async def async_media_play(self): await self._send_event("Play")
    async def async_media_pause(self): await self._send_event("Pause")
    async def async_media_stop(self): await self._send_event("Stop")
    
    async def async_media_next_track(self):
        self._ignore_updates_until = dt_util.as_timestamp(dt_util.utcnow()) + 3
        await self._send_event("Next")
        
    async def async_media_previous_track(self):
        self._ignore_updates_until = dt_util.as_timestamp(dt_util.utcnow()) + 3
        await self._send_event("Previous")
        
    async def async_set_shuffle(self, shuffle):
        await self._cmd_queue.put(f"EVENT S[{self._source}]!KeyRelease Shuffle")
        await asyncio.sleep(0.5)
        await self._queue_metadata_refresh()
        
    async def async_media_seek(self, position):
        await self._cmd_queue.put(f"EVENT S[{self._source}]!SetSeekTime {int(position)}")
        self._attr_media_position = int(position)
        self._attr_media_position_updated_at = dt_util.utcnow()
        self.async_write_ha_state()
