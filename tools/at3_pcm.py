#!/usr/bin/env python3
# Copied from the DDS-port repo (private), commit 9187ef0, tools/at3_pcm.py.
"""ATRAC3 sound files as samples ahead of time, for the decoder stand-in port/standins/at3.c (night 12, PLAN.md task S1).

The games decode their .at3 files (the title music and sounds, /soundat3 on the disc) on the EE with an ATRAC3 library (libat3dec,
db/port/sdk_adapter_plan.json: dds1 at3decReset and at3decDecodeFrame, dds2 at3decReset and at3decDecodeFrame). The port has no such decoder;
this tool decodes every file once on the PC with ffmpeg's atrac3 decoder and writes, into a folder OUTSIDE the repo:
  <name>.pcm   16-bit samples, left and right interleaved, 1024 per channel per frame (ffmpeg's frame size; the game copies 0x1000 bytes a frame,
               decomp/src/dds1/game/code_00269C10.c:180-195)
  index.bin    the hash of every frame's bytes with its file and frame number (layout below), so the stand-in finds a frame by its bytes.

  at3_pcm.py --src <folder of .at3 files> --out <folder> [--ffmpeg <ffmpeg executable>] [--only b_bgm2-2,se01-2] [--format NAME=CODE ...]

Formats (the work block's word 2, code_00269C10.c:24-26 and 408-424): 1 frames of 0x180 bytes, 2 and 3 of 0xC0, 4 of 0x60. As ffmpeg decodes
them (RIFF format 0x0270, extradata words 1, 0x1000, 0, joint, 0, 1, 0): 1 stereo, 2 joint stereo, 3 mono, 4 mono. Format 2 is the one the
title plays (code_00269C10.c:386-387 and 532-535); its decoded title music was confirmed by ear by the human (2026-10-20). Formats 1, 3 and 4
were found on the same music in the other formats (b_bgm2-1, -3, se01-1, -3, -4 of dds1): with these settings no decoder error, and the
loudness of every 1024-sample window follows format 2's with a correlation of 0.993 to 0.999 (G:/Temp/n11/at3_formats.py, night 12). A mono
format is written as stereo with both channels the same (the game copies 0x1000 bytes a frame whatever the format; UNVERIFIED that the
library writes mono that way). A file named <name>-<N>.at3 (N 1 to 4) is format N, as on both discs (the same title music as -1 to -4, the
frame counts fit); others are format 2 unless --format says otherwise. The rate written into the RIFF header is a label only: the samples are
played at the sound chip's 48000 a second.

index.bin, little endian: "DAT3", u32 version 1, u32 files, u32 entries; per file u32 frame bytes, u32 frames, 64 bytes of name (without .pcm,
zero padded); per entry, sorted by hash then file then frame: u64 FNV-1a hash of the frame's bytes, u32 file, u32 frame.
Game data: the output folder is refused inside the repo (CLAUDE.md golden rule 1).
"""
from __future__ import annotations

import argparse
import struct
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable, Dict, List, Sequence, Tuple

REPO = Path(__file__).resolve().parents[1]
SAMPLES = 1024                      # per channel per frame (ffmpeg's atrac3 frame; 0x1000 bytes of 16-bit stereo, as the game copies)
FRAME_BYTES = {1: 0x180, 2: 0xC0, 3: 0xC0, 4: 0x60}
FFMPEG_LAYOUT = {1: (2, 0), 2: (2, 1), 3: (1, 0), 4: (1, 0)}   # format: (channels, joint stereo) for ffmpeg (above)
CONVERTED = set(FFMPEG_LAYOUT)
MAGIC = b"DAT3"
VERSION = 1
NAME_BYTES = 64
FNV_OFFSET = 0xCBF29CE484222325
FNV_PRIME = 0x100000001B3


def fnv1a64(data: bytes) -> int:
	"""The frame hash (the same in port/standins/at3.c)."""
	h = FNV_OFFSET
	for b in data:
		h = ((h ^ b) * FNV_PRIME) & 0xFFFFFFFFFFFFFFFF
	return h


def inside_repo(path: Path) -> bool:
	p = path.resolve()
	return p == REPO or REPO in p.parents


def format_of(name: str, formats: Dict[str, int]) -> int:
	"""The format of <name> (no .at3): --format first, then a -1 to -4 ending, else 2."""
	if name in formats:
		return formats[name]
	tail = name.rsplit("-", 1)
	return int(tail[1]) if len(tail) == 2 and tail[1] in ("1", "2", "3", "4") else 2


def riff_atrac3(frames: bytes, code: int = 2) -> bytes:
	"""Raw ATRAC3 frames of a format in a RIFF WAVE header (format 0x0270) that ffmpeg's wav demuxer hands to its atrac3 decoder."""
	block = FRAME_BYTES[code]
	channels, joint = FFMPEG_LAYOUT[code]
	count = len(frames) // block
	data = frames[: count * block]
	extra = struct.pack("<7H", 1, 0x1000, 0, joint, 0, 1, 0)
	fmt = struct.pack("<HHIIHHH", 0x0270, channels, 44100, 44100 * block // SAMPLES, block, 0, len(extra)) + extra
	body = (b"WAVE" + b"fmt " + struct.pack("<I", len(fmt)) + fmt + b"fact" + struct.pack("<I", 4) + struct.pack("<I", count * SAMPLES)
		+ b"data" + struct.pack("<I", len(data)) + data)
	return b"RIFF" + struct.pack("<I", len(body)) + body


def ffmpeg_decoder(ffmpeg: str) -> Callable[[bytes, int], bytes]:
	"""decode(frames, format): 16-bit stereo samples (a mono format with both channels the same: -ac 2)."""
	def decode(frames: bytes, code: int) -> bytes:
		with tempfile.TemporaryDirectory() as d:
			src = Path(d) / "in.wav"
			src.write_bytes(riff_atrac3(frames, code))
			r = subprocess.run([ffmpeg, "-hide_banner", "-v", "error", "-i", str(src), "-f", "s16le", "-ac", "2", "-"], capture_output=True)
			if r.returncode != 0 or r.stderr.strip():
				raise RuntimeError(f"ffmpeg: {r.stderr.decode(errors='replace')[:400]}")
			return r.stdout
	return decode


def build_index(files: Sequence[Tuple[str, int, bytes]]) -> bytes:
	"""index.bin for (name, frame bytes, the file's bytes) in file order."""
	head = [MAGIC, struct.pack("<I", VERSION)]
	names = b""
	entries: List[Tuple[int, int, int]] = []
	for k, (name, fb, data) in enumerate(files):
		raw = name.encode("utf-8")
		if len(raw) >= NAME_BYTES:
			raise ValueError(f"name too long: {name}")
		frames = len(data) // fb
		names += struct.pack("<II", fb, frames) + raw.ljust(NAME_BYTES, b"\0")
		entries += [(fnv1a64(data[i * fb:(i + 1) * fb]), k, i) for i in range(frames)]
	entries.sort()
	head.append(struct.pack("<II", len(files), len(entries)))
	return b"".join(head) + names + b"".join(struct.pack("<QII", *e) for e in entries)


def read_index(blob: bytes) -> Tuple[List[Tuple[int, int, str]], List[Tuple[int, int, int]]]:
	"""(files as (frame bytes, frames, name), entries as (hash, file, frame)) of an index.bin."""
	if blob[:4] != MAGIC:
		raise ValueError("not an index")
	version, nfiles, nentries = struct.unpack_from("<III", blob, 4)
	if version != VERSION:
		raise ValueError(f"index version {version}")
	pos = 16
	files = []
	for _ in range(nfiles):
		fb, frames = struct.unpack_from("<II", blob, pos)
		files.append((fb, frames, blob[pos + 8:pos + 8 + NAME_BYTES].rstrip(b"\0").decode("utf-8")))
		pos += 8 + NAME_BYTES
	entries = [struct.unpack_from("<QII", blob, pos + 16 * i) for i in range(nentries)]
	return files, entries


def convert(src: Path, out: Path, decode: Callable[[bytes, int], bytes], only: Sequence[str] = (), formats: Dict[str, int] = None) -> List[str]:
	"""Decode the .at3 files of src into out; returns one line per file. A file in a format not converted yet, or whose size is not a whole
	number of frames, is left out and reported."""
	formats = formats or {}
	if inside_repo(out):
		raise SystemExit(f"refused: {out} is inside the repo (game data stays outside, CLAUDE.md rule 1)")
	out.mkdir(parents=True, exist_ok=True)
	report, done = [], []
	for path in sorted(src.glob("*")):
		if path.suffix.lower() != ".at3" or (only and path.stem not in only):
			continue
		code = format_of(path.stem, formats)
		data = path.read_bytes()
		if code not in CONVERTED:
			report.append(f"{path.name}: format {code} not converted yet")
			continue
		# night 17 (the whole /soundat3 folder for the human's device: battle music and jingles were missing): a name with no -N ending and no --format is only
		# assumed format 2; when that does not decode, the other formats are tried in order and the first that decodes cleanly to the right length is used (UNVERIFIED
		# which format the game's work block gives such a file; reported as guessed). A name whose format is known stays strict.
		known = path.stem in formats or format_of(path.stem, {}) != 2 or path.stem.endswith("-2")
		tries = [code] if known else [code] + [c for c in sorted(CONVERTED) if c != code]
		pcm, why = None, ""
		for c in tries:
			fb = FRAME_BYTES.get(c)
			if not data or len(data) % fb:
				why = f"{len(data)} bytes is not a whole number of {fb}-byte frames"
				continue
			try:
				got = decode(data, c)
			except RuntimeError as e:
				why = str(e).splitlines()[0][:120]
				continue
			frames = len(data) // fb
			if len(got) != frames * SAMPLES * 4:
				why = f"the decoder gave {len(got)} bytes for {frames} frames (want {frames * SAMPLES * 4})"
				continue
			pcm, code = got, c
			break
		if pcm is None:
			report.append(f"{path.name}: {why}, left out")
			continue
		(out / f"{path.stem}.pcm").write_bytes(pcm)
		done.append((path.stem, fb, data))
		report.append(f"{path.name}: format {code}{' (guessed)' if code != tries[0] else ''}, {frames} frames, {len(pcm)} bytes of samples")
	(out / "index.bin").write_bytes(build_index(done))
	report.append(f"index.bin: {len(done)} files")
	return report


def main() -> int:
	ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
	ap.add_argument("--src", type=Path, required=True)
	ap.add_argument("--out", type=Path, required=True)
	ap.add_argument("--ffmpeg", default="ffmpeg")
	ap.add_argument("--only", default="", help="comma-separated names without .at3")
	ap.add_argument("--format", action="append", default=[], help="NAME=CODE, the work block's format (default: the -N ending, else 2)")
	a = ap.parse_args()
	formats = {k: int(v) for k, v in (f.split("=", 1) for f in a.format)}
	only = [n for n in a.only.split(",") if n]
	for line in convert(a.src, a.out, ffmpeg_decoder(a.ffmpeg), only, formats):
		print(line)
	return 0


if __name__ == "__main__":
	sys.exit(main())
