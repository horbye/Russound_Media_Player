# Russound Media Player (Source Mode) for Home Assistant

A Home Assistant integration designed to control **Russound Media Streamers** (like the MBX-PRE and MBX-AMP) using the RIO (TCP/IP) protocol. 

This integration is optimized for **Source Mode**, providing detailed metadata and transport control for your streaming sources.

## Features
* **Full Metadata Sync:** Real-time display of Song Title, Artist, and Album.
* **Cover Art Support:** Automatically fetches and displays album art directly from the Russound streamer.
* **Extended Info:** View "Playlist Name", and "Channel Name" as extra entity attributes.
* **Dynamic Icons:** The entity icon automatically changes based on the source (Spotify, AirPlay, Radio, or Bluetooth).
* **Transport Controls:** Play, Pause, Stop, Next/Previous, Shuffle, and Repeat.
* **User-Defined Naming:** Custom name assignment during setup for easy identification in your dashboard.

## Installation

### Manual Installation
1. Download this repository.
2. Copy the `russound_media_player` folder into your Home Assistant `custom_components` directory.
3. Restart Home Assistant.

### Configuration
1. Navigate to **Settings** > **Devices & Services**.
2. Click **Add Integration** and search for **Russound Media Player**.
3. Fill in the following details:
   * **Host:** The IP address of your Russound device.
   * **Port:** Default is `9621`.
   * **Source:** The source number you wish to control (e.g., `5`).
   * **Name:** Your preferred display name (e.g., "Media Streamer").

## Supported Metadata Keys
The integration pulls data from the Russound RIO Source GET Key Table, including:
* `songName`, `artistName`, `albumName`
* `coverArtURL`
* `radioText`, `playlistName`, `channelName`
* `playStatus`, `shuffleMode`, `repeatMode`

---
**Developed by [@horbye](https://github.com/horbye)**
