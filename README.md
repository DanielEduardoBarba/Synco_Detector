# Synco Detector

Live microphone listener that:

1. Reads sung or spoken lyrics into large on-screen text.
2. Measures **where the groove puts its weight** — beat 1 (downbeat), beats 2 and 4 (backbeat), beat 3, or off-beat syncopation.
3. Places the clip on a spectrum from **simple 1-emphasis** (the pulse of a kids-church hymn such as *Jesus Loves Me*) to **syncopated 2/4 emphasis** (backbeat / off-beat grooves common in hip-hop, rap, funk, and related styles), with a confidence score.

This is a **rhythmic measurement**, not a spiritual or legal verdict. The detector reports pulse placement, meter, tempo, and how sure it is.

## Ubuntu (bare machine)

```bash
chmod +x build.sh
./build.sh --setup    # packages, venv, Whisper model, self-test
./build.sh --run      # open http://127.0.0.1:8080
./build.sh --test     # synthetic grooves + pytest
```

`--setup` installs `python3`, PortAudio, ffmpeg, libsndfile, BlueZ tools, and a local `.venv`. First run downloads the Whisper model into `.models/`.

Play a song (or sing) into the default microphone. Lyrics appear in the top panel; the needle shows 1-emphasis vs 2/4 syncopation. You can also drop a WAV/FLAC/OGG onto **Analyze a file**, or click **Run self-test**.

### Bluetooth speaker mode (iPhone → this machine)

```bash
./build.sh --run --audiosink
# or:  python -m synco_detector --audiosink
```

This advertises the laptop as a Bluetooth A2DP **speaker** named **Synco Detector** (override with `SYNCO_BT_ALIAS`). On the iPhone: Settings → Bluetooth → connect to that name, then play audio. Synco analyzes the Bluetooth stream directly; **the microphone is not used**.

Requirements: working BlueZ + PipeWire/PulseAudio Bluetooth (`parec`, `pactl`, `bluetoothctl`). Your adapter already needs the Audio Sink profile (most Linux laptops with PipeWire have this).

## What it measures

| Pole | Musical meaning | Typical examples |
| --- | --- | --- |
| Simple 1-emphasis | Accents land on beat 1 (and often 3). Melody sits on the beat. Little backbeat or off-beat kick. | Hymns, kids-church songs, marches, waltzes anchored on 1 |
| Syncopated 2/4 | Snare/clap on 2 and 4, kicks off the grid, hats on 16ths, or emphasis on the “and”. | Hip-hop, rap, rock backbeat, funk, reggae skank, trap half-time |

Each live window (~8 s) estimates tempo, meter (2/4, 3/4, 4/4), a 16th-note accent map, kick vs snare placement, Longuet-Higgins–Lee-style syncopation, and a 0–100 spectrum with confidence.

## Environment

- `SYNCO_HOST` / `SYNCO_PORT` — bind address (default `127.0.0.1:8080`)
- `SYNCO_WHISPER_MODEL` — `tiny.en`, `base.en`, `small.en` (default), or larger
- `SYNCO_MODEL_DIR` — Whisper cache (default `.models/`)
- `SYNCO_AUDIO_SINK=1` — Bluetooth A2DP sink mode (same as `--audiosink`)
- `SYNCO_BT_ALIAS` — name shown to phones (default `Synco Detector`)
# Synco_Detector
