import socket
import logging
import time
from datetime import timedelta
from homeassistant.components.media_player import (
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
    RepeatMode
)
from homeassistant.const import CONF_HOST, CONF_PORT, CONF_SOURCE, CONF_NAME
from homeassistant.util import dt as dt_util

_LOGGER = logging.getLogger(__name__)
SCAN_INTERVAL = timedelta(seconds=1)

async def async_setup_entry(hass, config_entry, async_add_entities):
    config = config_entry.data
    display_name = config.get(CONF_NAME, "Russound MBX")
    async_add_entities([
        RussoundSourceEntity(
            config[CONF_HOST], 
            config[CONF_PORT], 
            config[CONF_SOURCE],
            display_name
        )
    ])

class RussoundSourceEntity(MediaPlayerEntity):
    def __init__(self, ip, port, source, display_name):
        self._ip = ip
        self._port = port
        self._source = source
        self._attr_name = display_name 
        # Dette sikrer, at entiteten kan administreres i UI
        self._attr_unique_id = f"russound_{ip.replace('.', '_')}_source_{source}"
        self._attr_available = True 
        
        self._streaming_provider = "Unknown"
        self._radio_text = None
        
        self._attr_supported_features = (
            MediaPlayerEntityFeature.PLAY | 
            MediaPlayerEntityFeature.PAUSE | 
            MediaPlayerEntityFeature.STOP |
            MediaPlayerEntityFeature.NEXT_TRACK |
            MediaPlayerEntityFeature.PREVIOUS_TRACK |
            MediaPlayerEntityFeature.BROWSE_MEDIA |
            MediaPlayerEntityFeature.SEEK | 
            MediaPlayerEntityFeature.SHUFFLE_SET |
            MediaPlayerEntityFeature.REPEAT_SET
        )

    @property
    def extra_state_attributes(self):
        return {
            "streaming_provider": self._streaming_provider,
            "radio_text": self._radio_text,
            "last_synced": time.strftime("%H:%M:%S")
        }

    def send_command(self, cmd_string):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(0.8)
                s.connect((self._ip, self._port))
                s.sendall(f"{cmd_string}\r\n".encode())
                response = s.recv(1024).decode().strip()
                self._attr_available = True 
                return response
        except (socket.timeout, OSError):
            self._attr_available = False 
            return None

    def _parse(self, response):
        if not response or "E " in response: return None
        if "=" in response:
            response = response.split('=', 1)[1]
        return response.replace('"', '').strip()

    def update(self):
        status = self.send_command(f"GET S[{self._source}].playStatus")
        
        if status:
            song = self.send_command(f"GET S[{self._source}].songName")
            artist = self.send_command(f"GET S[{self._source}].artistName")
            provider = self.send_command(f"GET S[{self._source}].mode")
            cover = self.send_command(f"GET S[{self._source}].coverArtURL")
            cur_pos = self.send_command(f"GET S[{self._source}].playTime")
            dur_time = self.send_command(f"GET S[{self._source}].trackTime")
            shuffle_stat = self.send_command(f"GET S[{self._source}].shuffleMode")
            repeat_stat = self.send_command(f"GET S[{self._source}].repeatMode")

            self._attr_media_title = self._parse(song)
            self._attr_media_artist = self._parse(artist)
            self._streaming_provider = self._parse(provider)
            self._attr_media_content_type = "music"
            
            try:
                p_val = self._parse(cur_pos)
                d_val = self._parse(dur_time)
                if p_val and d_val:
                    self._attr_media_position = int(p_val)
                    self._attr_media_duration = int(d_val)
                    self._attr_media_position_updated_at = dt_util.utcnow()
            except: pass

            self._attr_shuffle = (self._parse(shuffle_stat) == "ON")
            self._attr_repeat_mode = RepeatMode.ALL if self._parse(repeat_stat) == "ON" else RepeatMode.OFF

            # Viser album coveret korrekt i UI
            c_url = self._parse(cover)
            self._attr_media_image_url = c_url if c_url and c_url.startswith("http") else None
            
            stat_clean = status.lower()
            if "playing" in stat_clean: self._attr_state = MediaPlayerState.PLAYING
            elif "paused" in stat_clean: self._attr_state = MediaPlayerState.PAUSED
            else: self._attr_state = MediaPlayerState.IDLE
        else:
            self._attr_state = MediaPlayerState.OFF

    def media_play(self): self.send_command(f"EVENT S[{self._source}]!KeyRelease Play")
    def media_pause(self): self.send_command(f"EVENT S[{self._source}]!KeyRelease Pause")
    def media_stop(self): self.send_command(f"EVENT S[{self._source}]!KeyRelease Stop")
    def media_next_track(self): self.send_command(f"EVENT S[{self._source}]!KeyRelease Next")
    def media_previous_track(self): self.send_command(f"EVENT S[{self._source}]!KeyRelease Previous")
    
    def set_shuffle(self, shuffle):
        cmd = "ShuffleOn" if shuffle else "ShuffleOff"
        self.send_command(f"EVENT S[{self._source}]!KeyRelease {cmd}")

    def set_repeat_mode(self, repeat_mode):
        cmd = "RepeatOn" if repeat_mode != RepeatMode.OFF else "RepeatOff"
        self.send_command(f"EVENT S[{self._source}]!KeyRelease {cmd}")
