#!/usr/bin/env python3
# Copied from the DDS-port repo (private), commit 9187ef0, tools/player_data.py.
"""The Vita build's music and movie folders from a player's own disc image, in one command (session 30; for the test builds' README).

Besides the disc image (ux0:data/dds1/disc.iso) the Vita build reads two folders made ahead of time from the same disc:
  at3/     the streamed music: the /soundat3/*.at3 files of the game's archive (DDS3.DDT, the file table, and DDS3.IMG, the data), decoded by ffmpeg
           (at3_pcm.py, next to this file)
  movies/  the movies: the /MOVIE* files of the disc (plain ISO 9660 files) as H.264 + AAC .mp4 (movie_convert.py, next to this file)
This script reads the image, takes the .at3 files out of the archive into a temporary folder, and runs both tools into one output folder. Needs Python 3.8 or
newer and ffmpeg with the atrac3 decoder and the libx264 and aac encoders (the usual full builds have them).

  player_data.py --iso <your disc image> [--out <folder>] [--ffmpeg <ffmpeg executable>] [--skip-movies] [--movies E635_003,END01] [--jobs 2]

The archive's table: entries of 12 bytes, little endian: u32 name offset (a zero-terminated ASCII name in the table), u32 location, s32 count. The entry at
offset 0 is the root. count 0 or more: a file of count bytes at sector `location` of DDS3.IMG; count below 0: a folder of -count entries, the first at table
offset `location`. (The format as the decomp's extractor reads it, decomp/tools/extract.py:38-102; this code is written here.)
The output folder is refused inside this repo (no game data in git).
"""
from __future__ import annotations

import argparse
import shutil
import struct
import sys
import tempfile
from pathlib import Path
from typing import Iterator, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
import at3_pcm  # noqa: E402
import movie_convert as mc  # noqa: E402

GAMES = {"SLUS_209.74": "dds1", "SLUS_211.52": "dds2"}   # the executable at the disc's root names the game (db/facts/project.json)
AT3_FOLDER = "/soundat3/"


class DataError(Exception):
	pass


def ddt_walk(ddt: bytes) -> Iterator[Tuple[str, int, int]]:
	"""Every file of the archive's table as (path, sector in DDS3.IMG, size); folders are walked, a folder that contains itself is refused."""
	def entry(offset: int) -> Tuple[int, int, int]:
		if offset < 0 or offset + 12 > len(ddt):
			raise DataError(f"DDS3.DDT: entry 0x{offset:x} is past the table's end")
		return struct.unpack_from("<IIi", ddt, offset)

	def name_at(offset: int) -> str:
		end = ddt.find(b"\0", offset)
		if offset >= len(ddt) or end < 0:
			raise DataError(f"DDS3.DDT: the name at 0x{offset:x} has no end")
		return ddt[offset:end].decode("ascii")

	open_folders = set()

	def visit(offset: int, parent: str) -> Iterator[Tuple[str, int, int]]:
		name_off, location, count = entry(offset)
		name = name_at(name_off)
		path = f"{parent}/{name}" if name else parent
		if count >= 0:
			yield (path or "/", location, count)
			return
		if offset in open_folders:
			raise DataError(f"DDS3.DDT: the folder at 0x{offset:x} contains itself")
		open_folders.add(offset)
		for i in range(-count):
			yield from visit(location + 12 * i, path)
		open_folders.discard(offset)

	yield from visit(0, "")


def root_file(iso: mc.Iso, name: str) -> Optional[mc.IsoEntry]:
	for e in iso.walk():
		if e.path.upper() == "/" + name.upper():
			return e
	return None


def which_game(iso: mc.Iso) -> str:
	for exe, game in GAMES.items():
		if root_file(iso, exe):
			return game
	raise DataError(f"{iso.path}: neither {' nor '.join(GAMES)} is at the disc's root: not a Digital Devil Saga (USA) disc image")


def extract_at3(iso: mc.Iso, dest: Path) -> int:
	"""Writes every /soundat3/*.at3 file of the archive into dest (flat, by name); returns how many."""
	ddt_e, img_e = root_file(iso, "DDS3.DDT"), root_file(iso, "DDS3.IMG")
	if not ddt_e or not img_e:
		raise DataError(f"{iso.path}: no DDS3.DDT or DDS3.IMG at the disc's root")
	ddt = iso.read(ddt_e.lba * mc.SECTOR, ddt_e.size)
	dest.mkdir(parents=True, exist_ok=True)
	n = 0
	for path, sector, size in ddt_walk(ddt):
		low = path.lower()
		if not (low.startswith(AT3_FOLDER) and low.endswith(".at3")):
			continue
		if sector * mc.SECTOR + size > img_e.size:
			raise DataError(f"{path}: {size} bytes at sector {sector} go past DDS3.IMG's end")
		data = iso.read((img_e.lba + sector) * mc.SECTOR, size)
		if len(data) != size:
			raise DataError(f"{path}: the image ends inside the file")
		(dest / path.rsplit("/", 1)[1]).write_bytes(data)
		n += 1
	return n


def main(argv=None) -> int:
	ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
	ap.add_argument("--iso", type=Path, required=True, help="your Digital Devil Saga (USA) disc image (a plain .iso)")
	ap.add_argument("--out", type=Path, help="the output folder (default: <game>_vita_data next to the image)")
	ap.add_argument("--ffmpeg", help="the ffmpeg executable (default: the one on the PATH)")
	ap.add_argument("--skip-movies", action="store_true", help="make the music only")
	ap.add_argument("--movies", help="comma separated movie names to make (default: all)")
	ap.add_argument("--jobs", type=int, default=2, help="movies made side by side (default 2)")
	a = ap.parse_args(argv)
	ffmpeg = mc.find_ffmpeg(a.ffmpeg)
	if not ffmpeg:
		print("player_data: ffmpeg not found: install it (with libx264), or name it with --ffmpeg", file=sys.stderr)
		return 2
	try:
		with mc.Iso(a.iso) as iso:
			game = which_game(iso)
			out = (a.out or a.iso.resolve().parent / f"{game}_vita_data").resolve()
			if mc.inside_repo(out):
				raise DataError(f"refused: {out} is inside this repo (game data stays out of git)")
			tmp = Path(tempfile.mkdtemp(prefix="dds_at3_"))
			try:
				count = extract_at3(iso, tmp)
				print(f"player_data: {count} music files taken out of the archive")
				for line in at3_pcm.convert(tmp, out / "at3", at3_pcm.ffmpeg_decoder(ffmpeg)):
					print("  " + line)
			finally:
				shutil.rmtree(tmp, ignore_errors=True)
	except (DataError, mc.MovieError) as ex:
		print(f"player_data: {ex}", file=sys.stderr)
		return 2
	if not a.skip_movies:
		args = ["--game", game, "--iso", str(a.iso), "--out", str(out / "movies"), "--ffmpeg", ffmpeg, "--jobs", str(a.jobs)]
		if a.movies:
			args += ["--only", a.movies]
		rc = mc.main(args)
		if rc:
			return rc
		shutil.rmtree(out / "movies" / ".work", ignore_errors=True)
	print(f"player_data: done. Copy the folders in {out} (at3{'' if a.skip_movies else ', movies'}) into ux0:data/{game}/ on the Vita, next to disc.iso.")
	return 0


if __name__ == "__main__":
	sys.exit(main())
