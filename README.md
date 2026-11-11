# DDS Vita port: test builds

Private test builds of the PS Vita port of Digital Devil Saga (dds1). The latest build is on the [Releases](../../releases) page.

This repo holds the app (the VPK) only. It never holds game files: no disc image, no saves, no music or movies. Those stay on your own Vita in `ux0:data/dds1`.

## Updating to a new build

1. Download the newest `dds1_*.vpk` from Releases.
2. Install it with VitaShell over the build you have. Your `ux0:data/dds1` folder (your disc image, saves and the rest) is not touched.
3. The game needs `libshacccg.suprx` extracted once on your Vita (the shader compiler vitaGL uses). If you ran an earlier build, you have it already.

## Reporting a problem

- Say what happened and where in the game, and which build (the release name, for example `rp322`).
- Right after it happens, copy `ux0:data/dds1/log.txt` from the Vita and send it. The log is written while the game runs and is replaced at the next start.
- For speed: a photo with the on-screen counter (both lines) helps.
