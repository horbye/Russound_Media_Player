# Russound Media Player for Home Assistant

This integration allows you to control Russound streaming devices (such as the MBX-PRE and MBX-AMP) directly from Home Assistant using the Russound RIO (TCP/IP) protocol.

It is specifically designed for users running their Russound units in **Source Mode**, providing a clean interface focused on media information and transport control.

## Key Features
* **Real-time Metadata:** Instantly fetches Song Title, Artist, and Album info.
* **High Performance:** Updates every second to ensure the dashboard is always in sync with the device.
* **Dynamic Iconry:** Automatically changes the entity icon based on the active streaming provider (Spotify, AirPlay, Internet Radio, etc.).
* **Full Transport Control:** Play, Pause, Stop, Next/Previous Track.
* **Smart Logic:** Support for Shuffle and Repeat modes.
* **Easy Setup:** Full support for Home Assistant Config Flow (no YAML required).

## Installation

### Manual Installation
1. Download the `russound_media_player` folder from this repository.
2. Copy the folder into your Home Assistant `custom_components` directory.
3. Restart Home Assistant.
4. Go to **Settings** -> **Devices & Services** -> **Add Integration**.
5. Search for **Russound Media Player**.
6. Enter your device's IP address (e.g., `192.168.40.102`), Port (`9621`), and the Source number (e.g., `5`).

## Technical Details
The integration communicates via Telnet on port 9621. By default, it skips volume control as this is typically handled by the downstream amplifier in a high-end Source Mode setup.

---
**Developed by [@horbye](https://github.com/horbye)**
