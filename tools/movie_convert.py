#!/usr/bin/env python3
# Copied from the DDS-port repo (private), commit 9187ef0, tools/movie_convert.py.
"""The games' movies as Vita video files, made ahead of time from the player's own disc (PLAN.md night 12, H1; docs/QUESTIONS.md Q27, kb/game/movies.md).

The PS2 plays its movies with the IPU (a video chip) and an IOP sound stream. The Vita has neither, so this tool turns every file of the disc's /MOVIE*
folders into one H.264 + AAC .mp4 that the Vita's own player (platform/vita/movie.c, sceAvPlayer) can play. The movies are NOT in DDS3.DDT: they are
plain ISO 9660 files in /MOVIE, /MOVIE2 and (dds2) /MOVIE3. Two kinds:
  .IPU   a raw "ipum" video stream (16 byte header: "ipum", u32 size, u16 width, u16 height, u32 frames), no sound
  .IPS   a container: packet header (command 0x0C, "IPS0"), a header word block, an APCM packet header, a one-bit-per-block mask, then the data blocks.
         A block whose mask bit is SET is PS2 ADPCM sound, a block whose bit is clear is "ipum" video. The game reads it the same way:
         decomp/src/dds2/sdf/sdfMovie.c:133-249 (movieReadChunk: packetBytes at header +0x18, blockBytes at +0x1C, the mask is the
         rest of the packet, bit set goes to the ring the sound reads, bit clear to the buffer the IPU reads).
Header words (little endian; offsets measured on the IPS files of both discs, kb/game/movies.md; the first three are the game's own reading above):
  +0x00 u8 0x0C, u32 payload size at +4 (the file size), "IPS0" at +8
  +0x11 u8 frames a second (0x1E = 30 on every file looked at; the descriptor tables of the games say 30 too: playbackCadenceStep 0x1E, one frame
        every 60/30 game frames, decomp/src/dds2/game/code_003425B0.c:1421-1456 and code_002A8048.c:49)
  +0x13 u8 sound channels (1 or 2), +0x14 u32 sound rate (44100 or 48000)
  +0x18 u32 packetBytes (header + mask; the data starts here), +0x1C u32 blockBytes (0x1000)
  +0x20 u8 0x0B, u32 at +0x24 = 0x20 + the sound bytes, "APCM" at +0x28; +0x30 u32 sound bytes per channel, +0x34 u32 interleave (0x100)
  +0x40 the block mask (bit i of byte i/8, least significant bit first, is block i)
The sound is stereo in chunks of `interleave` bytes (left chunk, right chunk, ...): 0x100 gave a left/right correlation of 0.68 on dds1 E635_003,
every other chunk size between 0x40 and 0x1000 gave under 0.11 (and the two channels' end blocks sit 0x100 bytes apart). It is shorter than the
video (the sound is not the whole movie); the tool pads it with silence to the video's length.

Sound decoder: PS2 SPU ADPCM, 16 byte blocks of 28 samples (shift and filter in the first byte, flags in the second), the usual 16 bit integer way
(the five predictor pairs every decoder uses). Checked against ffmpeg's adpcm_psx on the game's data (kb/game/movies.md): the same length, and the
samples differ by at most 61 steps of 16 bit (mean 6 to 13) because ffmpeg keeps more bits between samples; a block with flags 7 is the end marker
that ffmpeg turns into silence, and so does this decoder.

  movie_convert.py --game dds1 [--iso <disc image>] [--out <folder>] [--only E635_003,END01] [--list] [--jobs 2] [--crf 20] [--force]

Output: <out>/<NAME>.mp4 for every movie (NAME = the file name without folder and extension, upper case: E635_003) and <out>/manifest.json (name, source,
size, frames, fps, seconds, whether there is sound). The default out is G:/Temp/dds-movies/<game>; a folder inside the repo is refused (CLAUDE.md golden
rule 1). To use them: copy the folder's .mp4 files to ux0:data/<game>/movies/ on the Vita. Game data stays out of tests: tools/tests/test_movie_convert.py
builds synthetic discs and movies.
"""
from __future__ import annotations

import argparse
import array
import concurrent.futures
import glob
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, Callable, Dict, Iterator, List, Optional, Sequence, Tuple

REPO = Path(__file__).resolve().parents[1]
SECTOR = 2048                                   # ISO 9660 logical block (ECMA-119 8.1: 2048 here, the discs are cooked images)
DISCS = {"dds1": "Digital Devil Saga (USA).iso", "dds2": "Digital Devil Saga 2 (USA).iso"}
NATIVE_WINDOWS = os.name == "nt"
ISO_DIR = Path("G:/Games/PS2") if NATIVE_WINDOWS else Path("/mnt/g/Games/PS2")
OUT_DIR = Path("G:/Temp/dds-movies") if NATIVE_WINDOWS else Path("/mnt/g/Temp/dds-movies")
MOVIE_FOLDER_PREFIX = "MOVIE"                   # /MOVIE, /MOVIE2, /MOVIE3 (the paths the executables name: cdrom0:/movie2/e645_002.IPS)
MOVIE_KINDS = (".IPS", ".IPU")
DEFAULT_FPS = 30                                # playbackCadenceStep 0x1E in every row of the games' movie tables, 60 game frames a second
IPS_FPS_OFFSET = 0x11
IPS_CHANNELS_OFFSET = 0x13
IPS_RATE_OFFSET = 0x14
IPS_PACKET_BYTES_OFFSET = 0x18                  # sdf_movie_stream.h MovPacStream.packetBytes (+0x18) and blockBytes (+0x1C)
IPS_BLOCK_BYTES_OFFSET = 0x1C
IPS_APCM_OFFSET = 0x20
IPS_AUDIO_TOTAL_OFFSET = 0x24                   # 0x20 + the sound bytes
IPS_AUDIO_PER_CHANNEL_OFFSET = 0x30
IPS_INTERLEAVE_OFFSET = 0x34
IPS_MASK_OFFSET = 0x40
IPUM_HEADER_BYTES = 16
OUT_RATE = 48000                                # the Vita's main audio port runs at 48000 (psp2/audioout.h)
ADPCM_BLOCK = 16
ADPCM_SAMPLES = 28
FILTER_F0 = (0, 60, 115, 98, 122)               # the five predictor pairs of PS ADPCM, in 1/64 (the table every decoder uses; ffmpeg libavcodec/adpcm_data.c xa_adpcm_table)
FILTER_F1 = (0, 0, -52, -55, -60)


class MovieError(Exception):
	"""A movie that cannot be converted, with the reason in plain words."""


def inside_repo(path: Path) -> bool:
	p = path.resolve()
	return p == REPO or REPO in p.parents


# ---- ISO 9660 (ECMA-119), just enough to list and read files ----

@dataclass(frozen=True)
class IsoEntry:
	path: str          # "/MOVIE2/E645_002.IPS" (version suffix ";1" removed)
	lba: int
	size: int


class Iso:
	"""A cooked ISO 9660 image. Reads the primary volume descriptor and walks the directory tree. No other dependency (pycdlib is not in every venv)."""

	def __init__(self, path: Path):
		self.path = Path(path)
		self.fp = open(self.path, "rb")
		try:
			pvd = self.read(16 * SECTOR, SECTOR)
			if len(pvd) < SECTOR or pvd[0] != 1 or pvd[1:6] != b"CD001":
				raise MovieError(f"{self.path}: no ISO 9660 primary volume descriptor at sector 16 (a raw or compressed image is not supported)")
			if struct.unpack_from("<H", pvd, 128)[0] != SECTOR:
				raise MovieError(f"{self.path}: logical block size is not 2048")
			self.root = self.parse_record(pvd[156:190])
		except Exception:
			self.fp.close()
			raise

	def close(self) -> None:
		self.fp.close()

	def __enter__(self):
		return self

	def __exit__(self, *exc):
		self.close()

	def read(self, offset: int, size: int) -> bytes:
		self.fp.seek(offset)
		return self.fp.read(size)

	@staticmethod
	def parse_record(rec: bytes) -> Tuple[int, int, bool, str]:
		"""(lba, size, is_directory, name) of one directory record (ECMA-119 9.1)."""
		lba, = struct.unpack_from("<I", rec, 2)
		size, = struct.unpack_from("<I", rec, 10)
		flags = rec[25]
		name_len = rec[32]
		name = rec[33:33 + name_len]
		if name == b"\x00":
			text = "."
		elif name == b"\x01":
			text = ".."
		else:
			text = name.decode("ascii", "replace").split(";")[0]
		return lba, size, bool(flags & 2), text

	def read_dir(self, lba: int, size: int) -> List[Tuple[int, int, bool, str]]:
		data = self.read(lba * SECTOR, size)
		out = []
		pos = 0
		while pos < len(data):
			length = data[pos]
			if length == 0:                        # records never straddle a sector: a zero length skips to the next sector
				pos = (pos // SECTOR + 1) * SECTOR
				continue
			rec = data[pos:pos + length]
			if len(rec) < 34:
				break
			entry = self.parse_record(rec)
			if entry[3] not in (".", ".."):
				out.append(entry)
			pos += length
		return out

	def walk(self) -> Iterator[IsoEntry]:
		"""Every file of the image, depth first, with its path."""
		stack = [("", self.root[0], self.root[1])]
		while stack:
			prefix, lba, size = stack.pop()
			for e_lba, e_size, is_dir, name in self.read_dir(lba, size):
				if is_dir:
					stack.append((prefix + "/" + name, e_lba, e_size))
				else:
					yield IsoEntry(prefix + "/" + name, e_lba, e_size)

	def movies(self) -> List[IsoEntry]:
		"""The movie files: .IPS and .IPU in a folder whose name starts with MOVIE, sorted by path."""
		out = []
		for e in self.walk():
			parts = e.path.upper().split("/")
			if len(parts) >= 3 and parts[1].startswith(MOVIE_FOLDER_PREFIX) and parts[-1].endswith(MOVIE_KINDS):
				out.append(e)
		return sorted(out, key=lambda e: e.path.upper())

	def open_file(self, entry: IsoEntry) -> "IsoFile":
		return IsoFile(self, entry)


class IsoFile:
	"""Sequential and random reads of one file of the image."""

	def __init__(self, iso: Iso, entry: IsoEntry):
		self.iso, self.entry = iso, entry

	def read(self, offset: int, size: int) -> bytes:
		if offset >= self.entry.size:
			return b""
		size = min(size, self.entry.size - offset)
		return self.iso.read(self.entry.lba * SECTOR + offset, size)


def movie_name(path: str) -> str:
	"""The name a movie goes by on the Vita: the file name without folder and extension, upper case (the player does the same with the game's own path, platform/vita/movie.c)."""
	base = path.replace("\\", "/").rsplit("/", 1)[-1].split(";")[0]
	return base.rsplit(".", 1)[0].upper() if "." in base else base.upper()


def check_unique(entries: Sequence[IsoEntry]) -> None:
	seen: Dict[str, str] = {}
	for e in entries:
		name = movie_name(e.path)
		if name in seen:
			raise MovieError(f"two movies would be called {name}: {seen[name]} and {e.path}")
		seen[name] = e.path


# ---- the IPS container ----

@dataclass
class IpsHeader:
	file_size: int
	fps: int
	channels: int
	rate: int
	packet_bytes: int          # header and mask; the data blocks start here
	block_bytes: int
	audio_bytes: int           # sound bytes in all channels
	audio_per_channel: int
	interleave: int
	mask: bytes
	warnings: List[str] = field(default_factory=list)

	@property
	def has_audio(self) -> bool:
		return self.channels in (1, 2) and self.audio_bytes > 0 and self.interleave > 0 and 8000 <= self.rate <= 96000


def parse_ips_header(head: bytes, file_size: int) -> IpsHeader:
	"""The header words of an IPS file; `head` holds at least the first packet_bytes bytes (the mask is its tail)."""
	if len(head) < IPS_MASK_OFFSET or head[0] != 0x0C or head[8:12] != b"IPS0":
		raise MovieError("not an IPS file (no 0x0C command with the tag IPS0 at the start)")
	if head[IPS_APCM_OFFSET] != 0x0B or head[IPS_APCM_OFFSET + 8:IPS_APCM_OFFSET + 12] != b"APCM":
		raise MovieError("no APCM sound packet header at +0x20")
	fps = head[IPS_FPS_OFFSET]
	channels = head[IPS_CHANNELS_OFFSET]
	rate, = struct.unpack_from("<I", head, IPS_RATE_OFFSET)
	packet_bytes, block_bytes = struct.unpack_from("<II", head, IPS_PACKET_BYTES_OFFSET)
	total, = struct.unpack_from("<I", head, IPS_AUDIO_TOTAL_OFFSET)
	per_channel, interleave = struct.unpack_from("<II", head, IPS_AUDIO_PER_CHANNEL_OFFSET)
	if block_bytes == 0 or block_bytes % SECTOR:
		raise MovieError(f"blockBytes {block_bytes:#x} is not a multiple of 0x800")
	if packet_bytes < IPS_MASK_OFFSET or packet_bytes > file_size or packet_bytes > len(head):
		raise MovieError(f"packetBytes {packet_bytes:#x} does not fit the file")
	warnings = []
	audio_bytes = max(total - 0x20, 0)
	if channels in (1, 2) and per_channel * channels != audio_bytes:
		warnings.append(f"sound bytes per channel {per_channel:#x} x {channels} channels is not the packet's {audio_bytes:#x}")
	if not 1 <= fps <= 120:
		warnings.append(f"frame rate byte {fps} is not usable, {DEFAULT_FPS} assumed")
		fps = DEFAULT_FPS
	return IpsHeader(file_size, fps, channels, rate, packet_bytes, block_bytes, audio_bytes, per_channel, interleave, bytes(head[IPS_MASK_OFFSET:packet_bytes]), warnings)


def block_is_audio(mask: bytes, index: int) -> bool:
	byte = index >> 3
	return byte < len(mask) and bool(mask[byte] >> (index & 7) & 1)


def demux_ips(read: Callable[[int, int], bytes], hdr: IpsHeader, video_out: BinaryIO, chunk_blocks: int = 256) -> bytes:
	"""Splits the data blocks by the mask: the video blocks go to `video_out` in order, the sound blocks are returned joined. A last block shorter than
	blockBytes (a file that does not end on a block) keeps the side its mask bit says."""
	audio = bytearray()
	pos = hdr.packet_bytes
	index = 0
	step = hdr.block_bytes * chunk_blocks
	while pos < hdr.file_size:
		data = read(pos, min(step, hdr.file_size - pos))
		if not data:
			raise MovieError(f"short read at {pos:#x} of {hdr.file_size:#x}")
		for off in range(0, len(data), hdr.block_bytes):
			block = data[off:off + hdr.block_bytes]
			if block_is_audio(hdr.mask, index):
				audio += block
			else:
				video_out.write(block)
			index += 1
		pos += len(data)
	return bytes(audio[:hdr.audio_bytes]) if hdr.audio_bytes else b""


@dataclass
class IpuInfo:
	width: int
	height: int
	frames: int
	stream_bytes: int          # the header and the `size` bytes it announces


def parse_ipum(head: bytes) -> IpuInfo:
	"""The 16 byte header of an "ipum" stream (the game reads the same fields: include/sdf_stream_read.h SdfStreamFrameHeader, width at +8, height at +0xA, frame count at +0xC)."""
	if len(head) < IPUM_HEADER_BYTES or head[:4] != b"ipum":
		raise MovieError("the video stream does not start with the tag ipum")
	size, width, height, frames = struct.unpack_from("<IHHI", head, 4)
	if width == 0 or height == 0 or frames == 0:
		raise MovieError(f"ipum header with a zero size or frame count ({width}x{height}, {frames} frames)")
	return IpuInfo(width, height, frames, IPUM_HEADER_BYTES + size)


# ---- the sound ----

def deinterleave(data: bytes, channels: int, interleave: int) -> List[bytes]:
	"""Splits chunks of `interleave` bytes (channel 0, channel 1, ...) into one byte string per channel. A last incomplete round is split as far as it goes."""
	if channels == 1:
		return [bytes(data)]
	out = [bytearray() for _ in range(channels)]
	for pos in range(0, len(data), interleave * channels):
		for c in range(channels):
			out[c] += data[pos + c * interleave:pos + (c + 1) * interleave]
	return [bytes(o) for o in out]


_SHIFTED: List[List[int]] = [[((n - 16 if n & 8 else n) << 12) >> s for n in range(16)] for s in range(16)]   # nibble -> signed value scaled by the block's shift


def decode_adpcm(data: bytes, state: Tuple[int, int] = (0, 0)) -> Tuple["array.array[int]", Tuple[int, int]]:
	"""PS2 SPU ADPCM to 16 bit samples. A block is 16 bytes: byte 0 = filter << 4 | shift, byte 1 = flags, bytes 2 to 15 = 28 four bit samples, the low nibble
	of a byte first. sample = (nibble << 12) >> shift + (older1 * f0 + older2 * f1 + 32) >> 6, clamped to 16 bits. A shift above 12 acts as 9 and a filter above 4
	as 4 (the same limits ffmpeg's decoder applies). A block with flags 7 is the end marker the encoder appends: silence, the state is kept (what ffmpeg does).
	`state` is the last two samples (newest first); the new state is returned so a long stream can be decoded in parts with the same result."""
	out = array.array("h")
	o1, o2 = state
	for b in range(0, len(data) - ADPCM_BLOCK + 1, ADPCM_BLOCK):
		head = data[b]
		if data[b + 1] == 7:
			out.extend([0] * ADPCM_SAMPLES)
			continue
		shift = head & 15
		if shift > 12:
			shift = 9
		filt = head >> 4
		if filt > 4:
			filt = 4
		f0, f1 = FILTER_F0[filt], FILTER_F1[filt]
		table = _SHIFTED[shift]
		for byte in data[b + 2:b + ADPCM_BLOCK]:
			for nib in (byte & 15, byte >> 4):
				s = table[nib] + ((o1 * f0 + o2 * f1 + 32) >> 6)
				if s > 32767:
					s = 32767
				elif s < -32768:
					s = -32768
				out.append(s)
				o2 = o1
				o1 = s
	return out, (o1, o2)


def decode_channels(channels: List[bytes]) -> List["array.array[int]"]:
	"""Each channel decoded; the channels of one movie come out the same length (the longer one is cut)."""
	pcm = [decode_adpcm(c)[0] for c in channels]
	n = min(len(p) for p in pcm)
	return [p[:n] for p in pcm]


def interleave_pcm(pcm: List["array.array[int]"]) -> "array.array[int]":
	if len(pcm) == 1:
		return pcm[0]
	out = array.array("h", [0]) * (len(pcm[0]) * len(pcm))
	for c, p in enumerate(pcm):
		out[c::len(pcm)] = p
	return out


def write_wav(path: Path, samples: "array.array[int]", channels: int, rate: int) -> None:
	if sys.byteorder == "big":
		samples = array.array("h", samples)
		samples.byteswap()
	with wave.open(str(path), "wb") as w:
		w.setnchannels(channels)
		w.setsampwidth(2)
		w.setframerate(rate)
		w.writeframes(samples.tobytes())


# ---- ffmpeg ----

def find_ffmpeg(explicit: Optional[str] = None) -> Optional[str]:
	"""--ffmpeg, then $FFMPEG, then the PATH, then the WinGet install (Gyan.FFmpeg) on this PC, from Windows or from WSL."""
	for cand in (explicit, os.environ.get("FFMPEG")):
		if cand:
			return cand if Path(cand).is_file() else None
	hit = shutil.which("ffmpeg")
	if hit:
		return hit
	patterns = []
	if NATIVE_WINDOWS:
		base = os.environ.get("LOCALAPPDATA", "")
		patterns.append(os.path.join(base, "Microsoft", "WinGet", "Packages", "Gyan.FFmpeg*", "ffmpeg-*", "bin", "ffmpeg.exe"))
	else:
		patterns.append("/mnt/c/Users/*/AppData/Local/Microsoft/WinGet/Packages/Gyan.FFmpeg*/ffmpeg-*/bin/ffmpeg.exe")
	for pattern in patterns:
		found = sorted(glob.glob(pattern))
		if found:
			return found[-1]
	return None


class Ffmpeg:
	"""Runs ffmpeg and ffprobe. A Windows executable started from WSL gets its file arguments as Windows paths (wslpath -w)."""

	def __init__(self, exe: str):
		self.exe = exe
		self.probe_exe = str(Path(exe).with_name("ffprobe" + Path(exe).suffix))
		self.windows_exe_from_wsl = (not NATIVE_WINDOWS) and exe.lower().endswith(".exe")

	def arg(self, path: Path) -> str:
		if not self.windows_exe_from_wsl:
			return str(path)
		return subprocess.run(["wslpath", "-w", str(path)], capture_output=True, text=True, check=True).stdout.strip()

	def encode_command(self, video: Path, audio: Optional[Path], out: Path, fps: int, crf: int, preset: str = "medium") -> List[str]:
		"""One run: the raw IPU video at `fps` (the ipum stream carries no frame rate and ffmpeg assumes 25), H.264 baseline yuv420p (the profile every Vita
		player build is documented to take), AAC at 48000 stereo padded with silence to the video's end, faststart so the player finds its index at once."""
		cmd = [self.exe, "-hide_banner", "-v", "error", "-nostdin", "-y", "-r", str(fps), "-f", "ipu", "-i", self.arg(video)]
		if audio is not None:
			cmd += ["-i", self.arg(audio)]
		cmd += ["-map", "0:v:0", "-c:v", "libx264", "-profile:v", "baseline", "-level", "3.0", "-pix_fmt", "yuv420p", "-preset", preset, "-crf", str(crf), "-r", str(fps)]
		if audio is not None:
			cmd += ["-map", "1:a:0", "-c:a", "aac", "-b:a", "128k", "-ar", str(OUT_RATE), "-ac", "2", "-af", "apad", "-shortest"]
		cmd += ["-movflags", "+faststart", self.arg(out)]
		return cmd

	def probe(self, path: Path) -> dict:
		r = subprocess.run([self.probe_exe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", self.arg(path)], capture_output=True, text=True)
		if r.returncode:
			raise MovieError(f"ffprobe failed on {path.name}: {r.stderr.strip()[-300:]}")
		return json.loads(r.stdout)


# ---- one movie ----

@dataclass
class Result:
	name: str
	source: str
	source_bytes: int
	kind: str
	status: str = "ok"
	width: int = 0
	height: int = 0
	frames: int = 0
	fps: int = DEFAULT_FPS
	seconds: float = 0.0
	has_audio: bool = False
	audio_channels: int = 0
	audio_rate: int = 0
	audio_seconds: float = 0.0
	bytes: int = 0
	notes: List[str] = field(default_factory=list)


def plan_movie(iso: Iso, entry: IsoEntry) -> Tuple[Result, Optional[IpsHeader], IpuInfo]:
	"""Reads the headers only: what the movie is, before any work is done."""
	f = iso.open_file(entry)
	kind = entry.path.rsplit(".", 1)[-1].upper()
	res = Result(movie_name(entry.path), entry.path, entry.size, kind)
	if kind == "IPU":
		info = parse_ipum(f.read(0, IPUM_HEADER_BYTES))
		res.width, res.height, res.frames, res.fps = info.width, info.height, info.frames, DEFAULT_FPS
		res.seconds = round(info.frames / res.fps, 3)
		return res, None, info
	first = f.read(0, IPS_MASK_OFFSET)
	packet_bytes, = struct.unpack_from("<I", first, IPS_PACKET_BYTES_OFFSET) if len(first) >= IPS_MASK_OFFSET else (0,)
	hdr = parse_ips_header(f.read(0, min(max(packet_bytes, IPS_MASK_OFFSET), entry.size)), entry.size)
	res.fps = hdr.fps
	res.notes += hdr.warnings
	return res, hdr, IpuInfo(0, 0, 0, 0)


def convert_movie(iso_path: Path, entry: IsoEntry, out_dir: Path, work_root: Path, ff: Optional[Ffmpeg], crf: int, with_audio: bool = True, preset: str = "medium") -> Result:
	"""One movie to <out_dir>/<NAME>.mp4. Own file handle, so movies can run side by side."""
	with Iso(iso_path) as iso:
		res, hdr, ipu = plan_movie(iso, entry)
		f = iso.open_file(entry)
		work = Path(tempfile.mkdtemp(prefix=res.name + "-", dir=str(work_root)))
		try:
			video = work / "video.ipu"
			audio_bytes = b""
			with open(video, "wb") as vo:
				if hdr is None:
					pos = 0
					while True:
						data = f.read(pos, 8 << 20)
						if not data:
							break
						vo.write(data)
						pos += len(data)
				else:
					audio_bytes = demux_ips(f.read, hdr, vo)
			with open(video, "rb") as vf:
				info = parse_ipum(vf.read(IPUM_HEADER_BYTES))
			if info.stream_bytes < video.stat().st_size:
				with open(video, "r+b") as vf:          # the bytes after the stream's announced end are padding the decoder reports as an error
					vf.truncate(info.stream_bytes)
			res.width, res.height, res.frames = info.width, info.height, info.frames
			res.seconds = round(info.frames / res.fps, 3)
			wav = None
			if hdr is not None and hdr.has_audio and with_audio:
				pcm = decode_channels(deinterleave(audio_bytes, hdr.channels, hdr.interleave))
				wav = work / "audio.wav"
				write_wav(wav, interleave_pcm(pcm), hdr.channels, hdr.rate)
				res.has_audio, res.audio_channels, res.audio_rate = True, hdr.channels, hdr.rate
				res.audio_seconds = round(len(pcm[0]) / hdr.rate, 3)
			elif hdr is not None and hdr.audio_bytes and not hdr.has_audio:
				res.notes.append(f"sound packet present ({hdr.audio_bytes} bytes) but its header is unusable (channels {hdr.channels}, rate {hdr.rate}, interleave {hdr.interleave:#x}): silent")
			if ff is None:
				res.status = "not-encoded (no ffmpeg)"
				return res
			out = out_dir / (res.name + ".mp4")
			tmp = out_dir / (res.name + ".part.mp4")
			r = subprocess.run(ff.encode_command(video, wav, tmp, res.fps, crf, preset), capture_output=True, text=True)
			if r.returncode:
				tmp.unlink(missing_ok=True)
				raise MovieError(f"ffmpeg failed on {res.name}: {r.stderr.strip()[-400:]}")
			warning = r.stderr.strip()
			probe = ff.probe(tmp)
			vstream = next((s for s in probe["streams"] if s.get("codec_type") == "video"), None)
			if vstream is None:
				tmp.unlink(missing_ok=True)
				raise MovieError(f"{res.name}: the output has no video stream")
			try:
				count = int(vstream.get("nb_frames"))
			except (TypeError, ValueError):
				count = res.frames                      # the container gave no count: nothing to compare
				res.notes.append("ffprobe gave no frame count")
			if count != res.frames:
				res.status = "frame-count-differs"
				res.notes.append(f"the header says {res.frames} frames, the output has {count}")
				if warning:
					res.notes.append("ffmpeg: " + warning[-200:])
			elif warning:
				# ffmpeg's ipu decoder reports "Invalid data" once at the end of most streams (the bytes after the last frame); every frame is there
				res.notes.append("ffmpeg reported a decode error at the end of the stream; the frame count is right")
			os.replace(tmp, out)
			res.bytes = out.stat().st_size
			return res
		finally:
			shutil.rmtree(work, ignore_errors=True)


# ---- the manifest ----

def manifest_path(out_dir: Path) -> Path:
	return out_dir / "manifest.json"


def load_manifest(out_dir: Path) -> Dict[str, dict]:
	p = manifest_path(out_dir)
	if not p.is_file():
		return {}
	try:
		return {m["name"]: m for m in json.loads(p.read_text(encoding="utf-8")).get("movies", [])}
	except (OSError, ValueError, KeyError):
		return {}


def save_manifest(out_dir: Path, game: str, movies: Dict[str, dict], settings: dict) -> None:
	doc = {"game": game, "settings": settings, "movies": [movies[k] for k in sorted(movies)]}
	tmp = manifest_path(out_dir).with_suffix(".json.part")
	tmp.write_text(json.dumps(doc, indent=1) + "\n", encoding="utf-8")
	os.replace(tmp, manifest_path(out_dir))


def result_dict(r: Result) -> dict:
	return {k: getattr(r, k) for k in ("name", "source", "source_bytes", "kind", "status", "width", "height", "frames", "fps", "seconds", "has_audio", "audio_channels", "audio_rate", "audio_seconds", "bytes", "notes")}


def up_to_date(entry: IsoEntry, done: Optional[dict], out_dir: Path) -> bool:
	"""A movie already converted from a file of this size, whose output is still there, is not done again."""
	name = movie_name(entry.path)
	return bool(done and done.get("status") == "ok" and done.get("source_bytes") == entry.size and (out_dir / (name + ".mp4")).is_file())


def pick(entries: Sequence[IsoEntry], only: Optional[Sequence[str]], limit: Optional[int]) -> List[IsoEntry]:
	wanted = {o.strip().upper() for o in only if o.strip()} if only else None
	chosen = [e for e in entries if wanted is None or movie_name(e.path) in wanted]
	if wanted:
		missing = wanted - {movie_name(e.path) for e in chosen}
		if missing:
			raise MovieError("not on the disc: " + ", ".join(sorted(missing)))
	return chosen[:limit] if limit else chosen


def checked_out_dir(path: Path) -> Path:
	if inside_repo(path):
		raise MovieError(f"{path} is inside the repo: converted movies are game data and go elsewhere (CLAUDE.md golden rule 1)")
	return path


def find_disc(game: str, iso: Optional[Path]) -> Path:
	p = Path(iso) if iso else ISO_DIR / DISCS[game]
	if not p.is_file():
		raise MovieError(f"{game}: no disc image at {p} (use --iso)")
	return p


def run_game(game: str, a: argparse.Namespace, ff: Optional[Ffmpeg]) -> int:
	iso_path = find_disc(game, a.iso)
	out_dir = checked_out_dir((a.out or OUT_DIR / game).resolve())
	with Iso(iso_path) as iso:
		entries = iso.movies()
	check_unique(entries)
	chosen = pick(entries, a.only.split(",") if a.only else None, a.limit)
	if a.list:
		for e in chosen:
			print(f"{movie_name(e.path):12} {e.size:>12,}  {e.path}")
		print(f"{len(chosen)} movies, {sum(e.size for e in chosen):,} bytes")
		return 0
	if ff is None:
		raise MovieError("no ffmpeg found (--ffmpeg, $FFMPEG, the PATH or the WinGet Gyan.FFmpeg install)")
	out_dir.mkdir(parents=True, exist_ok=True)
	work_root = checked_out_dir((a.work or out_dir / ".work").resolve())
	work_root.mkdir(parents=True, exist_ok=True)
	manifest = load_manifest(out_dir)
	settings = {"crf": a.crf, "preset": a.preset, "audio": not a.no_audio, "audio_rate": OUT_RATE, "ffmpeg": ff.exe}
	todo = [e for e in chosen if a.force or not up_to_date(e, manifest.get(movie_name(e.path)), out_dir)]
	print(f"{game}: {len(chosen)} movies, {len(todo)} to convert -> {out_dir}")
	failures = 0
	t0 = time.time()
	with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, a.jobs)) as pool:
		futures = {pool.submit(convert_movie, iso_path, e, out_dir, work_root, ff, a.crf, not a.no_audio, a.preset): e for e in todo}
		for n, fut in enumerate(concurrent.futures.as_completed(futures), 1):
			e = futures[fut]
			try:
				r = fut.result()
				manifest[r.name] = result_dict(r)
				audio = f"audio {r.audio_seconds:.2f} s" if r.has_audio else "no sound"
				print(f"[{n}/{len(todo)}] {r.name}: {r.frames} frames {r.seconds:.2f} s {r.width}x{r.height}, {audio}, {r.bytes:,} bytes, {r.status} ({time.time() - t0:.0f} s)")
			except MovieError as ex:
				failures += 1
				manifest[movie_name(e.path)] = {"name": movie_name(e.path), "source": e.path, "source_bytes": e.size, "status": "failed: " + str(ex)}
				print(f"[{n}/{len(todo)}] {movie_name(e.path)}: FAILED {ex}")
			save_manifest(out_dir, game, manifest, settings)
	if not a.keep_work:
		shutil.rmtree(work_root, ignore_errors=True)
	total = sum(m.get("bytes", 0) for m in manifest.values())
	print(f"{game}: done, {failures} failed, {len(manifest)} in the manifest, {total:,} bytes of mp4 in {out_dir}")
	return 1 if failures else 0


def main(argv: Optional[Sequence[str]] = None) -> int:
	ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
	ap.add_argument("--game", nargs="+", choices=sorted(DISCS), required=True)
	ap.add_argument("--iso", type=Path, help="the disc image (default: the game's disc in G:/Games/PS2); one game only")
	ap.add_argument("--out", type=Path, help="the output folder, outside the repo (default G:/Temp/dds-movies/<game>)")
	ap.add_argument("--work", type=Path, help="scratch folder for the video of one movie (default <out>/.work); outside the repo")
	ap.add_argument("--only", help="comma separated movie names, e.g. E635_003,END01")
	ap.add_argument("--limit", type=int, help="convert at most this many (of those chosen)")
	ap.add_argument("--list", action="store_true", help="list the movies of the disc, write nothing")
	ap.add_argument("--jobs", type=int, default=2, help="movies converted side by side (default 2; ffmpeg uses all cores for each)")
	ap.add_argument("--crf", type=int, default=20, help="x264 quality, lower is better and bigger (default 20)")
	ap.add_argument("--preset", default="medium", help="x264 preset (default medium)")
	ap.add_argument("--no-audio", action="store_true", help="leave the sound out of every movie")
	ap.add_argument("--force", action="store_true", help="convert movies again that the manifest says are done")
	ap.add_argument("--keep-work", action="store_true")
	ap.add_argument("--ffmpeg", help="the ffmpeg executable")
	a = ap.parse_args(argv)
	if a.iso and len(a.game) > 1:
		ap.error("--iso names one disc: give one --game")
	if a.out and len(a.game) > 1:
		ap.error("--out names one folder: give one --game")
	exe = find_ffmpeg(a.ffmpeg)
	ff = Ffmpeg(exe) if exe else None
	rc = 0
	try:
		for game in a.game:
			rc |= run_game(game, a, ff)
	except MovieError as ex:
		print(f"movie_convert: {ex}", file=sys.stderr)
		return 2
	return rc


if __name__ == "__main__":
	sys.exit(main())
