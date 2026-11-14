# DDS Vita port: test builds

Private test builds of the PS Vita port of Digital Devil Saga (dds1). The latest build is on the [Releases](../../releases) page.

This repo holds the app (the VPK) and a small script, nothing from the game: no disc image, no saves, no music or movies. You make those yourself from your own disc, and they stay on your Vita in `ux0:data/dds1`.

## Setting up the game's data folder

The game runs from your own disc image of **Digital Devil Saga (USA)** (a plain `.iso`). Besides the image it reads two folders made from the same disc: `at3` (the streamed music) and `movies` (the cutscene movies, converted to a format the Vita can play). The script `tools/player_data.py` makes both in one go.

You need, on a PC:
- Python 3.8 or newer (python.org);
- ffmpeg, a full build (it needs the ATRAC3 decoder and the libx264 and aac encoders). On Windows: `winget install Gyan.FFmpeg`. The script also finds ffmpeg on your PATH, or name it with `--ffmpeg <path>`.

Then, from this repo's folder:

```
python tools/player_data.py --iso "D:\Games\Digital Devil Saga (USA).iso"
```

It writes a folder `dds1_vita_data` next to the image, with `at3` and `movies` inside. The music takes a few minutes. The movies take much longer, because every one is converted; `--skip-movies` makes the music only, and the game runs without the movies (it skips them).

On the Vita (with VitaShell over FTP or USB), `ux0:data/dds1` should then hold:

```
ux0:data/dds1/disc.iso      your disc image, renamed to disc.iso
ux0:data/dds1/at3/          from dds1_vita_data
ux0:data/dds1/movies/       from dds1_vita_data
```

The game makes its other folders (saves and so on) by itself the first time it runs.

## Updating to a new build

1. Download the newest `dds1_*.vpk` from Releases.
2. Install it with VitaShell over the build you have. Your `ux0:data/dds1` folder (your disc image, saves and the rest) is not touched.
3. The game needs `libshacccg.suprx` extracted once on your Vita (the shader compiler vitaGL uses). If you ran an earlier build, you have it already.
   Also turn on Settings > HENkaku Settings > Enable Unsafe Homebrew, if it is off: the game needs it to read that file on some Vitas.

## Reporting a problem

- Say what happened and where in the game, and which build (the release name, for example `rp322`).
- Right after it happens, copy `ux0:data/dds1/log.txt` from the Vita and send it. The log is written while the game runs and is replaced at the next start.
- For speed: a photo with the on-screen counter (both lines) helps.
