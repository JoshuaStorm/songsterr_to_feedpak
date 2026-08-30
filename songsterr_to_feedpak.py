# Copyright (c) 2026 Kevin Lu

from typing import Callable, Optional, Tuple, Union
from functools import cmp_to_key
import os
import tempfile
import json
import io
import zipfile
import asyncio
import argparse
import shutil
import re

import yaml
import bs4
import httpx
import yt_dlp
import ffmpeg

# Minimum number of frets in anchor
CONFIG_ANCHOR_MIN_WIDTH = 4
# Amount of time to move the anchor back, measured in beats
CONFIG_ANCHOR_MARGIN_BEATS = 0.1
# Amount of sustain to remove at the end, measured in beats
CONFIG_SUSTAIN_MARGIN_BEATS = 0.2
# Number of frets to slide for unpitched slides
CONFIG_UNPITCHED_SLIDE_WIDTH = 5

def get_note_name(note: int):
    return [
        'E',
        'F',
        'Gb',
        'G',
        'Ab',
        'A',
        'Bb',
        'B',
        'C',
        'Db',
        'D',
        'Eb',
    ][note % 12]

class _TuningShape:
    def __init__(self, deltas: list[int], formatter: Callable[[list[int]], str]):
        self.deltas = deltas
        self.formatter = formatter

_TuningShape.STD = _TuningShape([-5, -4, -5, -5, -5],
                    lambda strings: f"{get_note_name(strings[5])} STD")
_TuningShape.DROP = _TuningShape([-5, -4, -5, -5, -7],
                    lambda strings: f"{get_note_name(strings[0])} DROP {get_note_name(strings[5])}")
_TuningShape.BASS_STD = _TuningShape([-5, -5, -5],
                    lambda strings: f"Bass {get_note_name(strings[3])} STD")

_TUNING_SHAPES = [
    _TuningShape.STD,
    _TuningShape.DROP,
    _TuningShape.BASS_STD,
]

class Tuning:
    def __init__(self, strings: list[int]):
        """
        strings: Top string first. 0 = Low E string of E STD.
        """
        self.strings = strings
        deltas = [strings[i + 1] - strings[i] for i in range(len(strings) - 1)]
        for shape in _TUNING_SHAPES:
            if deltas == shape.deltas:
                shape = shape
                break
        else:
            shape = _TuningShape(deltas,
                                 lambda strings: " ".join(get_note_name(n) for n in reversed(strings)))
        self.name = shape.formatter(strings)

# Only support up to 8 strings
_STANDARD_TUNING = [-10, -5, 0, 5, 10, 15, 19, 24]

def _tuning_subtract(lhs: list[int], rhs: list[int]) -> list[int]:
    cmp_len = min(len(lhs), len(rhs))
    return [l - r for l, r in zip(lhs[-cmp_len:], rhs[-cmp_len:])]

def _get_song_url(song_id: int) -> str:
    return f"https://www.songsterr.com/a/wsa/s{song_id}"

def _get_track_url(song: str, revision: str, image: str, part: int) -> str:
    return f"https://dqsljvtekg760.cloudfront.net/{song}/{revision}/{image}/{part}.json"

def _get_video_sync_url(song: str, revision: str) -> str:
    return f"https://www.songsterr.com/api/video-points/{song}/{revision}/list"

def _get_search_url(query: str, from_index: int, count: int) -> str:
    return f"https://www.songsterr.com/api/search?pattern={query}&size={count}&from={from_index}"

def _to_valid_filename(name: str) -> str:
    return "".join(c for c in name if c.isalnum() or c in " ._-").rstrip()

class _SongData:
    def __init__(self,
                 song_id: int,
                 revision: int,
                 image: str,
                 names: list[str],
                 instruments: list[str],
                 track_difficulties: list[Optional[int]],
                 tags: list[str],
                 artist: str,
                 title: str):
            self.song_id = song_id
            self.revision = revision
            self.image = image
            self.names = names
            self.instruments = instruments
            self.track_difficulties = track_difficulties
            self.tags = tags
            self.artist = artist
            self.title = title

    @staticmethod
    def extract(html_text: str) -> "_SongData":
        soup = bs4.BeautifulSoup(html_text, "html.parser")
        j = soup.find(id="state").text
        data = json.loads(j)["meta"]["current"]
        return _SongData(
            song_id=data["songId"],
            revision=data["revisionId"],
            image=data["image"],
            names=[t["name"] for t in data["tracks"]],
            instruments=[t["instrument"] for t in data["tracks"]],
            track_difficulties=[t.get("difficulty") for t in data["tracks"]],
            tags=data["tags"],
            artist=data["artist"],
            title=data["title"],
        )

class _TrackData:
    def __init__(self,
                 string_count: int,
                 tuning: Optional[Tuning],
                 capo: int,
                 measures: list[dict]):
        self.string_count = string_count
        self.tuning = tuning
        self.capo = capo
        self.measures = measures

    @staticmethod
    def extract(text: str) -> "_TrackData":
        json_data = json.loads(text)
        return _TrackData(
            string_count=json_data["strings"],
            tuning=Tuning([n - 40 for n in json_data["tuning"]])
                        if json_data.get("tuning") else None,
            capo=json_data.get("capo", 0),
            measures=json_data["measures"],
        )

class _VideoSyncData:
    def __init__(self, video_id: str, measure_times: list[float]):
        self.video_id = video_id
        self.measure_times = measure_times

    @staticmethod
    def extract(text: str) -> list["_VideoSyncData"]:
        def _cmp_video(lhs, rhs):
            """
            Prefer non feature videos, then order by index.
            """
            lhs_has_feature = bool(lhs.get("feature", False))
            rhs_has_feature = bool(rhs.get("feature", False))
            if lhs_has_feature != rhs_has_feature:
                return int(lhs_has_feature) - int(rhs_has_feature)
            return lhs["_index"] - rhs["_index"]
            
        videos = json.loads(text)
        for i, video in enumerate(videos):
            video["_index"] = i

        return [_VideoSyncData(
            video_id=video["videoId"],
            measure_times=video["points"],
        ) for video in sorted(videos, key=cmp_to_key(_cmp_video))]

class Instrument:
    GUITAR = "guitar"
    BASS = "bass"
    OTHER = "other"

    @staticmethod
    def get(instrument: str) -> str:
        if re.search(r"\bguitar\b", instrument, flags=re.IGNORECASE):
            return Instrument.GUITAR
        elif re.search(r"\bbass\b", instrument, flags=re.IGNORECASE):
            return Instrument.BASS
        else:
            return Instrument.OTHER

class SongsterrTrackSearchResult:
    def __init__(self, name: str, instrument: str, tuning: Optional[Tuning], difficulty: Optional[int]):
        self.name = name or instrument
        self.instrument = instrument
        self.tuning = tuning
        self.difficulty = difficulty

class SongsterrSongSearchResult:
    def __init__(self, title: str, artist: str, song_id: int, tracks: list[SongsterrTrackSearchResult]):
        self.title = title
        self.artist = artist
        self.song_id = song_id
        self.tracks = tracks

class SongsterrTrack:
    def __init__(self,
                 song: "SongsterrSong",
                 track_id: int,
                 name: str,
                 instrument: str,
                 tuning: Optional[Tuning],
                 capo: int,
                 difficulty: Optional[int],
                 measures: list[dict]):
        self.song = song
        self.track_id = track_id
        self.name = name or instrument
        self.instrument = instrument
        self.tuning = tuning
        self.capo = capo
        self.difficulty = difficulty
        self.measures = measures

class SongsterrSong:
    def __init__(self,
                 song_id: int,
                 title: str,
                 artist: str,
                 tracks: list[SongsterrTrack],
                 yt_video_id: str,
                 video_sync_times: list[float]):
        self.song_id = song_id
        self.title = title
        self.artist = artist
        self.tracks = tracks
        self.yt_video_id = yt_video_id
        self.video_sync_times = video_sync_times

class Mp3:
    def __init__(self, data: bytes, duration: float):
        self.data = data
        self.duration = duration

async def _fetch(url) -> str:
    print(f"Fetching {url}")
    async with httpx.AsyncClient() as client:
        response = await client.get(url, follow_redirects=True)
        return response.text

async def download_songsterr_song(song_id: int) -> list[SongsterrSong]:
    html_url = _get_song_url(song_id)
    html_text = await _fetch(html_url)
    song_data = _SongData.extract(html_text)

    video_sync_url = _get_video_sync_url(song_data.song_id, song_data.revision)
    video_sync_datas = _VideoSyncData.extract(await _fetch(video_sync_url))

    download_tasks = []
    for i in range(len(song_data.names)):
        track_url = _get_track_url(song_data.song_id, song_data.revision, song_data.image, i)
        download_tasks.append(_fetch(track_url))
    track_data_text = await asyncio.gather(*download_tasks)
    track_datas = [_TrackData.extract(text) for text in track_data_text]

    songs = []
    for video_sync_data in video_sync_datas:
        song = SongsterrSong(
            song_id=song_data.song_id,
            title=song_data.title,
            artist=song_data.artist,
            tracks=[],
            yt_video_id=video_sync_data.video_id,
            video_sync_times=video_sync_data.measure_times,
        )
        zipped = zip(song_data.names, song_data.instruments, song_data.track_difficulties, track_datas)
        for i, (name, instrument, track_difficulty, track_data) in enumerate(zipped):
            song.tracks.append(SongsterrTrack(
                song=song,
                track_id=i,
                name=name,
                instrument=instrument,
                tuning=track_data.tuning,
                difficulty=track_difficulty,
                capo=track_data.capo,
                measures=track_data.measures,
            ))
        songs.append(song)
    return songs

async def search_songsterr(query: str, from_index: int = 0, count: int = 10) -> list[SongsterrSongSearchResult]:
    search_url = _get_search_url(query, from_index, count)
    search_results = json.loads(await _fetch(search_url))

    results = []
    for result in search_results["records"]:
        tracks = []
        for track in result["tracks"]:
            tracks.append(SongsterrTrackSearchResult(
                name=track["name"],
                instrument=track["instrument"],
                tuning=Tuning([n - 40 for n in track["tuning"]]) if track.get("tuning") else None,
                difficulty=track.get("difficulty"),
            ))
        results.append(SongsterrSongSearchResult(
            title=result["title"],
            artist=result["artist"],
            song_id=result["songId"],
            tracks=tracks,
        ))
    return results

async def download_youtube_mp3(video_id: str) -> Mp3:
    # TODO: make async
    try:
        print(f"==== BEGIN YOUTUBE DOWNLOAD {video_id} ====")
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_file =  os.path.join(tmp_dir, video_id)
            ydl_opts = {
                'format': 'bestaudio/best',
                'postprocessors': [{
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'mp3',
                    'preferredquality': '0',
                }],
                'outtmpl': tmp_file,
            }
            tmp_file += '.mp3'

            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([f'https://www.youtube.com/watch?v={video_id}'])

            duration = float(ffmpeg.probe(tmp_file)['format']['duration'])

            with open(tmp_file, 'rb') as f:
                return Mp3(data=f.read(), duration=duration)
    finally:
        print(f"==== END YOUTUBE DOWNLOAD {video_id} ====")

def _get_arrangement_filename(track: SongsterrTrack) -> str:
    return f"arrangements/{track.track_id}_{_to_valid_filename(track.name)}.json"

def _get_stem_filename() -> str:
    return "stems/full.mp3"

def build_feedpak_arrangement(track: SongsterrTrack) -> str:
    fp_notes = []
    fp_chords = []
    fp_anchors = []
    fp_sections = []
    fp_beats = []

    DEFAULT_BPM = 100
    secs_per_semibreve = 15 / DEFAULT_BPM
    t = 0
    anchor_min_fret = -1
    anchor_max_fret = -1
    hopo_from = {} # Map of string -> the fret we are hopo-ing from
    slides = set() # Strings that are currently sliding
    prev_notes = {} # Map of string -> the previous note on that string

    for measure_num, measure in enumerate(track.measures):
        # Calculate the length of measure
        beats = measure["voices"][0]["beats"]
        semibreves_in_measure = sum(
            beat["duration"][0] / beat["duration"][1]
            for beat in beats
        )

        # Update current time
        if measure_num < len(track.song.video_sync_times):
            t = track.song.video_sync_times[measure_num]

        # Calculate BPM (actually secs per semibreve since that is more natural)
        if measure_num + 1 < len(track.song.video_sync_times):
            next_measure_t = track.song.video_sync_times[measure_num + 1]
        else:
            next_measure_t = t + (semibreves_in_measure * secs_per_semibreve)
        secs_per_semibreve = (next_measure_t - t) / semibreves_in_measure

        # Add section
        if "marker" in measure:
            section_name = measure["marker"]["text"]
            fp_sections.append({
                "name": section_name,
                "number": len(fp_sections) + 1,
                "time": t,
            })

        # Add beat lines
        fp_beats.append({
            "time": t,
            "measure": measure_num + 1,
        })
        for i in range(1, int(semibreves_in_measure * 4)):
            fp_beats.append({
                "time": t + (i * secs_per_semibreve / 4),
                "measure": -1,
            })

        # Process notes
        for beat in beats:
            duration_semibreves = (beat["duration"][0] / beat["duration"][1]) if "duration" in beat else 0

            palm_mute = beat.get("palmMute", False)
            tremelo = beat.get("tremolo", False)
            tap = beat.get("tapping", False)

            pick_dir = -1
            if "pickStroke" in beat:
                if beat["pickStroke"] == "down":
                    pick_dir = 0
                elif beat["pickStroke"] == "up":
                    pick_dir = 1

            simultaneous_notes = []
            for note in beat["notes"]:
                # Skip rests and unpitched notes
                if ("rest" in note) or ("fret" not in note):
                    continue

                # Skip ties (but update the sustain duration)
                string = len(track.tuning.strings) - note["string"] - 1
                if "tie" in note:
                    prev_notes[string]["sus"] += duration_semibreves * secs_per_semibreve
                    continue

                fret = note["fret"]
                hopo_delta = fret - hopo_from.get(string, fret)

                # Set previous note's slide to this note's fret
                if string in slides:
                    prev_notes[string]["sl"] = fret
                    slides.remove(string)

                simultaneous_notes.append({
                    "s": string, # String number
                    "f": fret, # Fret number
                    "sus": duration_semibreves * secs_per_semibreve, # Sustain in seconds
                    "spsb": secs_per_semibreve, # [Internal] seconds per semibreve
                    "sl": -1, # Pitched slide to fret (filled in later)
                    "slu": fret - CONFIG_UNPITCHED_SLIDE_WIDTH if note.get("slide") == "downwards" else -1, # Unpitched slide to fret
                    "bn": (note["bend"]["tone"] / 50) if note.get("bend") else 0, # Bend amount in semitones
                    "ho": hopo_delta > 0, # Hammer-on
                    "po": hopo_delta < 0, # Pull-off
                    "hm": note.get("harmonic") == "natural", # Natural harmonic
                    "hp": note.get("harmonic") in ("pinch", "artificial"), # Pinch harmonic
                    "pm": palm_mute, # Palm mute
                    "mt": note.get("dead", False), # String mute
                    "vb": note.get("vibrato", False), # Vibrato
                    "tr": tremelo, # Tremolo
                    "ac": note.get("accentuated", False) or note.get("staccato", False), # Accent (also do staccato)
                    "pkd": pick_dir, # Pick direction
                    "tap": tap, # Tap
                })

                # Hopo handled on the next note
                if string in hopo_from:
                    del hopo_from[string]
                if note.get("hp", False):
                    hopo_from[string] = fret

                # Work out slide destination later
                if note.get("slide") in ("legato", "shift"):
                    slides.add(string)

                prev_notes[string] = simultaneous_notes[-1]

            if len(simultaneous_notes) == 1:
                simultaneous_notes[0]["t"] = t
                fp_notes.append(simultaneous_notes[0])
            elif len(simultaneous_notes) > 1:
                fp_chords.append({
                    "t": t,
                    "id": 0,
                    "hd": False,
                    "notes": simultaneous_notes,
                })

            # Update anchor if requested anchor does not fit in the current anchor
            non_open_notes = [note for note in simultaneous_notes if note["f"]]
            if non_open_notes:
                req_anchor_min_fret = min(note["f"] for note in non_open_notes)
                req_anchor_max_fret = max(note["f"] for note in non_open_notes)
                if not (anchor_min_fret <= req_anchor_min_fret <= req_anchor_max_fret <= anchor_max_fret):
                    anchor_min_fret = req_anchor_min_fret
                    anchor_max_fret = max(req_anchor_max_fret, req_anchor_min_fret + CONFIG_ANCHOR_MIN_WIDTH - 1)
                    fp_anchors.append({
                        "time": t - secs_per_semibreve / 4  * CONFIG_ANCHOR_MARGIN_BEATS,
                        "fret": anchor_min_fret,
                        "width": anchor_max_fret - anchor_min_fret + 1,
                    })

            # Update time
            t += beat["duration"][0] / beat["duration"][1] * secs_per_semibreve

    # Post-process sustains
    for note in fp_notes + [n for chord in fp_chords for n in chord["notes"]]:
        secs_per_beat = note["spsb"] / 4
        # Do not sustain if duration is less than a beat.
        # Slides/bends/vibrato/tremolo are always sustains.
        if (note["sus"] <= secs_per_beat
                and note["sl"] == -1
                and note["slu"] == -1
                and note["bn"] == 0
                and not note["vb"]
                and not note["tr"]):
            note["sus"] = 0
        # Visually shorten the sustain slightly unless it is a slide
        elif (note["sl"] == -1 and note["slu"] == -1):
            note["sus"] -= secs_per_beat * CONFIG_SUSTAIN_MARGIN_BEATS
        del note["spsb"]

    # Add a default section if there are no sections
    if not fp_sections:
        fp_sections.append({
            "name": "Song",
            "number": 1,
            "time": 0,
        })

    return json.dumps({
        "name": track.name,
        "tuning": _tuning_subtract(list(reversed(track.tuning.strings)), _STANDARD_TUNING),
        "capo": track.capo,
        "notes": fp_notes,
        "chords": fp_chords,
        "anchors": fp_anchors,
        "handshapes": [],
        "templates": [],
        "beats": fp_beats,
        "sections": fp_sections,
    })

def build_feedpak_manifest(song: SongsterrSong, duration: float) -> str:
    manifest = {
        "songsterr_to_feedpak_version": "0.0.0",
        "feedpak_version": "1.0.0",
        "title": song.title,
        "artist": song.artist,
        "duration": duration,
        "arrangements": [
            {
                "id": f"{track.track_id}_{_to_valid_filename(track.name)}",
                "name": track.name,
                "file": _get_arrangement_filename(track),
                "tuning": _tuning_subtract(list(reversed(track.tuning.strings)), _STANDARD_TUNING),
                "capo": track.capo,
                "centOffset": 0,
            } for track in song.tracks
        ],
        "stems": [{
            "id": "full",
            "file": _get_stem_filename(),
            "default": True,
        }],
    }
    return yaml.dump(manifest)

def build_zip(files: dict[str, Union[str, bytes]]) -> bytes:
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
        for filename, data in files.items():
            zip_file.writestr(filename, data)
    return zip_buffer.getvalue()

def build_feedpak(song: SongsterrSong, mp3: Mp3) -> dict[str, Union[str, bytes]]:
    if any(not track.tuning for track in song.tracks):
        raise ValueError("All tracks must have a valid tuning")
    files = {}
    files["manifest.yaml"] = build_feedpak_manifest(song, mp3.duration)
    for track in song.tracks:
        files[_get_arrangement_filename(track)] = build_feedpak_arrangement(track)
    files[_get_stem_filename()] = mp3.data
    return files

async def download_songsterr_song_to_feedpak(song_id: int) -> Tuple[SongsterrSong, Mp3, dict[str, Union[str, bytes]]]:
    exc = None
    songs = await download_songsterr_song(song_id)
    for song in songs:
        if song != songs[0]:
            print(f"Trying next alternative youtube video")
        song.tracks = [
            track for track in song.tracks
            if track.tuning and Instrument.get(track.instrument) in (Instrument.GUITAR, Instrument.BASS)]
        try:
            mp3 = await download_youtube_mp3(song.yt_video_id)
        except Exception as e:
            exc = e
        else:
            feedpak = build_feedpak(song, mp3)
            return song, mp3, feedpak
    raise exc or ValueError("No valid tracks found for this song")

def has_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None

async def _handle_download(args):
    print(f"==== DOWNLOAD SONG ====")

    # Need ffmpeg for youtube download
    if not has_ffmpeg():
        raise RuntimeError("ffmpeg is not installed")

    song, _, feedpak = await download_songsterr_song_to_feedpak(args.download)

    default_filename = _to_valid_filename(f"{song.artist} - {song.title} - {song.song_id}.feedpak")
    if args.output:
        if os.path.isdir(args.output):
            feedpak_dst = os.path.join(args.output, default_filename)
        else:
            feedpak_dst = args.output
    else:
        feedpak_dst = default_filename

    if args.folder:
        for filename, data in feedpak.items():
            full_path = os.path.join(feedpak_dst, filename)
            os.makedirs(os.path.dirname(full_path), exist_ok=True)
            with open(full_path, 'wb') as f:
                f.write(data if isinstance(data, bytes) else data.encode('utf-8'))
    else:
        with open(feedpak_dst, 'wb') as f:
            f.write(build_zip(feedpak))

async def _handle_search(args) -> list[SongsterrSongSearchResult]:
    print("==== SEARCH ====")
    results = await search_songsterr(args.search)
    print(f"Search results for '{args.search}':")
    for result in results:
        print(f"  - {result.title} by {result.artist} (ID: {result.song_id})")
        for track in result.tracks:
            if Instrument.get(track.instrument) in (Instrument.GUITAR, Instrument.BASS):
                tuning_name = f" ({track.tuning.name})" if track.tuning else ""
                print(f"    - {track.name}{tuning_name}")
    return results

async def main():
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--download", "-D", metavar="SONG_ID", type=int, help="Download and create a feedpak from the given Songsterr song ID.")
    group.add_argument("--search", "-s", metavar="QUERY", type=str, help="Search Songsterr for a song.")
    group.add_argument("--search-and-download", "-d", metavar="QUERY", type=str, help="Search Songsterr for a song and download the first result. This is a convenience option that combines --search and --download.")
    parser.add_argument("--output", "-o", type=str, help="The output feedpak path. If this refers to an existing folder, the feedpak will be placed in that folder. Otherwise, this will be used as the filename of the feedpak.")
    parser.add_argument("--folder", "-f", action="store_true", help="Save feedpak as a folder instead of a single file.")
    args = parser.parse_args()

    if args.download:
        await _handle_download(args)
    elif args.search:
        await _handle_search(args)
    elif args.search_and_download:
        args.search = args.search_and_download
        results = await _handle_search(args)
        args.download = results[0].song_id
        await _handle_download(args)
    print("==== DONE ====")
        
if __name__ == "__main__":
    asyncio.run(main())
