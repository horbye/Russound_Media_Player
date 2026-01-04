# Russound Media Player (Source Mode) for Home Assistant

An advanced Home Assistant integration designed to control **Russound Media Streamers** using the RIO (TCP/IP) protocol. 

This integration operates in **Source Mode**, providing deep metadata synchronization and full transport control directly at the source level. It has been verified and tested on the following hardware:
* **XSource** Streaming Audio Player
* **MBX-PRE** & **MBX-AMP** Wi-Fi Streaming Series
* **MCA-88X** Multi-Zone Controller (Internal Streamer)

## Features
* **Real-Time Metadata:** Instant sync of Song Title, Artist, and Album Name.
* **Cover Art Support:** Automatically fetches and displays high-resolution album art directly from the Russound streamer.
* **Smart State Management:** Intelligent handling of "IDLE" vs "OFF" states based on real-time system status.
* **Resilient Connectivity:** Advanced reconnection logic that keeps the session alive or wakes it instantly upon user interaction.
* **Legacy Support:** Optimized refresh intervals and compatibility for older Russound hardware (e.g., MCA-series).
* **Transport Controls:** Play, Pause, Stop, Next/Previous, Shuffle, and Repeat.
* **Extended Attributes:** Access to "Playlist Name", "Streaming Provider", and precise "Track Position/Duration" within Home Assistant.

## Installation

### Manual Installation
1. Download this repository.
2. Copy the `russound_source` folder into your Home Assistant `custom_components` directory.
3. Restart Home Assistant.

### Configuration
1. Navigate to **Settings** > **Devices & Services**.
2. Click **Add Integration** and search for **Russound Media Player**.
3. Fill in the details for your **XSource**, **MBX**, or **MCA** device:
    * **Host:** The IP address of your Russound device.
    * **Port:** Default is `9621`.
    * **Source:** The source number you wish to control (e.g., `1`).
    * **Name:** Your preferred display name (e.g., "Kitchen Streamer").

## Advanced Logic
* **Queue-Based Communication:** Commands are processed through an internal queue (`cmd_q`) to prevent race conditions and ensure stability during network latency.
* **Power Management:** The integration automatically manages TCP sessions, closing connections when the system is off and re-establishing them instantly when commands are sent.
* **Playlist Transport Fallback:** If a source fails to report standard transport states, the integration can automatically apply playlist-based transport logic to maintain UI responsiveness.

## Supported Metadata Keys
The integration pulls data from the Russound RIO Source Key Table, including:
* `SONGNAME`, `ARTISTNAME`, `ALBUMNAME`
* `COVERARTURL`
* `PLAYSTATUS`, `PLAYTIME`, `TRACKTIME`
* `SHUFFLEMODE`, `REPEATMODE`
* `SYSTEM.STATUS` (for power state synchronization)

---
**Developed by [@horbye](https://github.com/horbye)**
