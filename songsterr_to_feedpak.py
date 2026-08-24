from typing import Callable, Optional, Tuple, Union
import os
import tempfile
import json
import io
import zipfile
import asyncio
import argparse

import yaml
import bs4
import httpx
import yt_dlp
import ffmpeg

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

class Tuning:
    def __init__(self, strings: list[int]):
        """
        strings: Top string first. 0 = Low E string of E STD.
        """
        self.strings = strings
        deltas = [strings[i + 1] - strings[i] for i in range(len(strings) - 1)]
        for shape in [_TuningShape.STD, _TuningShape.DROP, _TuningShape.BASS_STD]:
            if deltas == shape.deltas:
                shape = shape
                break
        else:
            shape = _TuningShape(deltas,
                                      lambda strings: " ".join(get_note_name(n) for n in strings))
        self.name = shape.formatter(strings)

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
                 track_names: list[str],
                 track_difficulties: list[Optional[int]],
                 tags: list[str],
                 artist: str,
                 title: str):
            self.song_id = song_id
            self.revision = revision
            self.image = image
            self.track_names = track_names
            self.track_difficulties = track_difficulties
            self.tags = tags
            self.artist = artist
            self.title = title

class _TrackData:
    def __init__(self, string_count: int, tuning: Optional[Tuning], measures: list[dict]):
        self.string_count = string_count
        self.tuning = tuning
        self.measures = measures

class _VideoSyncData:
    def __init__(self, video_id: str, measure_times: list[float]):
        self.video_id = video_id
        self.measure_times = measure_times

def _extract_song_data(html_text: str):
    soup = bs4.BeautifulSoup(html_text, "html.parser")
    j = soup.find(id="state").text
    data = json.loads(j)["meta"]["current"]
    return _SongData(
        song_id=data["songId"],
        revision=data["revisionId"],
        image=data["image"],
        track_names=[t["instrument"] for t in data["tracks"]],
        track_difficulties=[t.get("difficulty") for t in data["tracks"]],
        tags=data["tags"],
        artist=data["artist"],
        title=data["title"],
    )

def _extract_track_data(text: str):
    json_data = json.loads(text)
    return _TrackData(
        string_count=json_data["strings"],
        tuning=Tuning([n - 40 for n in json_data["tuning"]])
                      if json_data.get("tuning") else None,
        measures=json_data["measures"],
    )

def _extract_video_sync_data(text: str):
    json_data = json.loads(text)
    non_feature_videos = [v for v in json_data if not v.get("feature")]
    video = non_feature_videos[0] if non_feature_videos else json_data[0]
    return _VideoSyncData(
        video_id=video["videoId"],
        measure_times=video["points"],
    )

class SongsterrTrackSearchResult:
    def __init__(self, track_name: str, tuning: Optional[Tuning], difficulty: Optional[int]):
        self.track_name = track_name
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
                 track_name: str,
                 tuning: Optional[Tuning],
                 difficulty: Optional[int],
                 measures: list[dict]):
        self.song = song
        self.track_id = track_id
        self.track_name = track_name
        self.tuning = tuning
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

async def _fetch(url):
    print(f"Fetching {url}")
    async with httpx.AsyncClient() as client:
        response = await client.get(url, follow_redirects=True)
        return response.text

async def _download_songsterr_song(song_id: int) -> SongsterrSong:
    html_url = _get_song_url(song_id)
    html_text = await _fetch(html_url)
    song_data = _extract_song_data(html_text)

    video_sync_url = _get_video_sync_url(song_data.song_id, song_data.revision)
    video_sync_data = _extract_video_sync_data(await _fetch(video_sync_url))

    song = SongsterrSong(
        song_id=song_data.song_id,
        title=song_data.title,
        artist=song_data.artist,
        tracks=[],
        yt_video_id=video_sync_data.video_id,
        video_sync_times=video_sync_data.measure_times
    )

    download_tasks = []
    for i in range(len(song_data.track_names)):
        track_url = _get_track_url(song_data.song_id, song_data.revision, song_data.image, i)
        download_tasks.append(_fetch(track_url))
    track_datas = await asyncio.gather(*download_tasks)

    zipped = zip(song_data.track_names, song_data.track_difficulties, track_datas)
    for i, (track_name, track_difficulty, track_data_text) in enumerate(zipped):
        track_data = _extract_track_data(track_data_text)
        song.tracks.append(SongsterrTrack(
            song=song,
            track_id=i,
            track_name=track_name,
            tuning=track_data.tuning,
            difficulty=track_difficulty,
            measures=track_data.measures,
        ))
    return song

async def search_songsterr(query: str, from_index: int = 0, count: int = 10) -> list[SongsterrSongSearchResult]:
    search_url = _get_search_url(query, from_index, count)
    search_results = json.loads(await _fetch(search_url))

    results = []
    for result in search_results["records"]:
        tracks = []
        for track in result["tracks"]:
            tracks.append(SongsterrTrackSearchResult(
                track_name=track["instrument"],
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
    url = f'https://www.youtube.com/watch?v={video_id}'
    print(f"Downloading {url}")

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
            ydl.download([url])

        duration = float(ffmpeg.probe(tmp_file)['format']['duration'])

        with open(tmp_file, 'rb') as f:
            return Mp3(data=f.read(), duration=duration)

def _get_arrangement_filename(track: SongsterrTrack) -> str:
    return f"arrangements/{track.track_id}_{_to_valid_filename(track.track_name)}.json"

def _get_stem_filename() -> str:
    return "stems/full.mp3"

def build_feedpak_arrangement(track: SongsterrTrack) -> str:
    fp_notes = []
    fp_chords = []
    fp_anchors = []
    fp_sections = []
    fp_beats = []

    secs_per_semibreve = 15 / 100 # 100 bpm
    t = 0
    anchor_min_width = 4
    anchor_min_fret = -1
    anchor_max_fret = -1
    hopo_from = {}

    for measure_num, measure in enumerate(track.measures):
        beats = measure["voices"][0]["beats"]
        semibreves_in_measure = sum(
            beat["duration"][0] / beat["duration"][1]
            for beat in beats
        )

        if measure_num < len(track.song.video_sync_times):
            t = track.song.video_sync_times[measure_num]
        if measure_num + 1 < len(track.song.video_sync_times):
            next_measure_t = track.song.video_sync_times[measure_num + 1]
        else:
            next_measure_t = t + (semibreves_in_measure * secs_per_semibreve)

        secs_per_semibreve = (next_measure_t - t) / semibreves_in_measure

        if "marker" in measure:
            section_name = measure["marker"]["text"]
            fp_sections.append({
                "name": section_name,
                "number": len(fp_sections) + 1,
                "time": t,
            })

        fp_beats.append({
            "time": t,
            "measure": measure_num + 1,
        })
        for i in range(1, int(semibreves_in_measure * 4)):
            fp_beats.append({
                "time": t + (i * secs_per_semibreve / 4),
                "measure": -1,
            })

        for beat in beats:
            simultaneous_notes = []
            for note in beat["notes"]:
                if note.get("rest"):
                    continue
                string = len(track.tuning.strings) - note["string"] - 1
                if "fret" not in note or "tie" in note:
                    continue
                fret = note["fret"]
                duration_semibreves = (note["duration"][0] / note["duration"][1]) if note.get("duration") else 0
                hopo_delta = fret - hopo_from.get(string, fret)
                simultaneous_notes.append({
                    "s": string, # String number
                    "f": fret, # Fret number
                    "sus": duration_semibreves * secs_per_semibreve if duration_semibreves >= 0.5 else 0, # Sustain in seconds TODO: fix
                    "sl": -1, # Pitched slide to fret
                    "slu": -1, # Unpitched slide to fret
                    "bn": (note["bend"]["tone"] / 50) if note.get("bend") else 0, # Bend amount in semitones
                    "ho": hopo_delta > 0, # Hammer-on
                    "po": hopo_delta < 0, # Pull-off
                    "hm": note.get("harmonic") == "natural", # Natural harmonic
                    "hp": note.get("harmonic") in ("pinch", "artificial"), # Pinch harmonic
                    "pm": note.get("palmMute", False), # Palm mute
                    "mt": note.get("dead", False), # String mute
                    "vb": note.get("vibrato", False), # Vibrato
                    "tr": False, # Tremolo
                    "ac": note.get("accentuated", False) or note.get("stoccato", False), # Accent
                })
                if string in hopo_from:
                    del hopo_from[string]
                if note.get("hp", False):
                    hopo_from[string] = fret

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

            non_open_notes = [note for note in simultaneous_notes if note["f"]]
            if non_open_notes:
                req_anchor_min_fret = min(note["f"] for note in non_open_notes)
                req_anchor_max_fret = max(note["f"] for note in non_open_notes)
                if not (anchor_min_fret <= req_anchor_min_fret <= req_anchor_max_fret <= anchor_max_fret):
                    anchor_min_fret = req_anchor_min_fret
                    anchor_max_fret = max(req_anchor_max_fret, req_anchor_min_fret + anchor_min_width - 1)
                    fp_anchors.append({
                        "time": t,
                        "fret": anchor_min_fret,
                        "width": anchor_max_fret - anchor_min_fret + 1,
                    })

            t += beat["duration"][0] / beat["duration"][1] * secs_per_semibreve

    return json.dumps({
        "name": track.track_name,
        "tuning": _tuning_subtract(list(reversed(track.tuning.strings)), _STANDARD_TUNING),
        "capo": 0, # TODO
        "notes": fp_notes,
        "chords": fp_chords,
        "anchors": fp_anchors,
        "handshapes": [],
        "templates": [],
        "beats": fp_beats,
        "sections": fp_sections,
    }, indent=2)

def build_feedpak_manifest(song: SongsterrSong, duration: float) -> str:
    manifest = {
        "feedpak_version": "1.0.0",
        "title": song.title,
        "artist": song.artist,
        "duration": duration,
        "arrangements": [
            {
                "id": f"{track.track_id}_{_to_valid_filename(track.track_name)}",
                "name": track.track_name,
                "file": _get_arrangement_filename(track),
                "tuning": _tuning_subtract(list(reversed(track.tuning.strings)), _STANDARD_TUNING),
                "capo": 0,
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
    song = await _download_songsterr_song(song_id)
    song.tracks = [
        track for track in song.tracks
        if track.tuning and ("guitar" in track.track_name.lower() or "bass" in track.track_name.lower())]
    mp3 = await download_youtube_mp3(song.yt_video_id)
    feedpak = build_feedpak(song, mp3)
    return song, mp3, feedpak

async def _handle_download(args):
    song, _, feedpak = await download_songsterr_song_to_feedpak(args.download)
    feedpak_dst = args.output if args.output else f"{_to_valid_filename(song.artist)} - {_to_valid_filename(song.title)} - {song.song_id}.feedpak"

    if args.folder:
        for filename, data in feedpak.items():
            full_path = os.path.join(feedpak_dst, filename)
            os.makedirs(os.path.dirname(full_path), exist_ok=True)
            with open(full_path, 'wb') as f:
                f.write(data if isinstance(data, bytes) else data.encode('utf-8'))
    else:
        with open(feedpak_dst, 'wb') as f:
            f.write(build_zip(feedpak))

async def _handle_search(args):
    results = await search_songsterr(args.search)
    print(f"Search results for '{args.search}':")
    for result in results:
        print(f"  - {result.title} by {result.artist} (ID: {result.song_id})")
        for track in result.tracks:
            if "guitar" in track.track_name.lower() or "bass" in track.track_name.lower():
                tuning_name = f" ({track.tuning.name})" if track.tuning else ""
                print(f"    - {track.track_name}{tuning_name}")

async def main():
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--download", "-d", type=int, help="The Songsterr song ID to download.")
    group.add_argument("--search", "-s", type=str, help="The search query to find songs on Songsterr.")
    parser.add_argument("--output", "-o", type=str, help="The output file to save the downloaded song data (JSON format). Only valid with --download.")
    parser.add_argument("--folder", "-f", action="store_true", help="Save feedpak to a folder instead of a single file. Only valid with --download.")
    args = parser.parse_args()

    if args.download:
        await _handle_download(args)
    elif args.search:
        await _handle_search(args)

if __name__ == "__main__":
    asyncio.run(main())
