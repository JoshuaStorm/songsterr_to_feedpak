# Songsterr to feedpak

Convert songs from [Songsterr](https://www.songsterr.com/) to feedpaks for [fee[dB]ack](https://github.com/got-feedBack/feedBack). Corresponding audio is pulled from YouTube.

# Prerequisites

- Install Python.

- Install the packages from `requirements.txt`.
```
python3 -m pip install -r requirements.txt
```

- You should always be using the newest version of the `yt-dlp` package. If you have issues downloading from YouTube, ensure this package is up to date.

```
python3 -m pip install --upgrade yt-dlp
```

- Install ffmpeg.

```
winget install -e --id Gyan.FFmpeg
```

# Usage

## Search Songsterr

```sh
# Search for master of puppets.
python3 songsterr_to_feedpak.py search "master of puppets"
```

## Download by song ID

```sh
# Download from https://www.songsterr.com/a/wsa/metallica-master-of-puppets-tab-s455118.
# Recommend adding --artist-folder and --substitute-empty-sections options.
python3 songsterr_to_feedpak.py download-by-id 455118
```

## Search Songsterr and download first result

```sh
# Search and download master of puppets.
# Recommend adding --artist-folder and --substitute-empty-sections options.
python3 songsterr_to_feedpak.py download "master of puppets"
```
