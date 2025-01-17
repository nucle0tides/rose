"""
The virtualfs module renders a virtual filesystem from the read cache. It is written in an
Object-Oriented style, against my typical sensibilities, because that's how the FUSE libraries tend
to be implemented. But it's OK :)

Since this is a pretty hefty module, we'll cover the organization. This module contains 6 classes:

1. VirtualPath: A semantic representation of a path in the virtual filesystem along with a parser.
   All virtual filesystem paths are parsed by this class into a far more ergonomic dataclass.

2. "CanShow"er: An abstraction that encapsulates the logic of whether an artist, genre, or label
   should be shown in their respective virtual views, based on the whitelist/blacklist configuration
   parameters.

3. FileHandleGenerator: A class that keeps generates new file handles. It is a counter that wraps
   back to 4 when the file handles exceed ~10k, as to avoid any overflows.

4. RoseLogicalFS: A logical representation of Rose's filesystem logic, freed from the annoying
   implementation details that a low-level library like `llfuse` comes with.

5. INodeManager: A class that tracks the INode <-> Path mappings. It is used to convert inodes to
   paths in VirtualFS.

6. VirtualFS: The main Virtual Filesystem class, which manages the annoying implementation details
   of a low-level virtual filesystem, and delegates logic to the above classes. It uses INodeManager
   and VirtualPath to translate inodes into semantically useful dataclasses, and then passes them
   into RoseLogicalFS.
"""

from __future__ import annotations

import contextlib
import errno
import logging
import os
import random
import re
import stat
import subprocess
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import cachetools
import llfuse

from rose.audiotags import SUPPORTED_AUDIO_EXTENSIONS, AudioTags
from rose.cache import (
    artist_exists,
    collage_exists,
    cover_exists,
    genre_exists,
    get_playlist,
    get_release,
    get_release_source_path_from_id,
    label_exists,
    list_artists,
    list_collage_releases,
    list_collages,
    list_genres,
    list_labels,
    list_playlists,
    list_releases,
    release_exists,
    track_exists,
    update_cache_for_releases,
)
from rose.collages import (
    add_release_to_collage,
    create_collage,
    delete_collage,
    remove_release_from_collage,
    rename_collage,
)
from rose.common import RoseError
from rose.config import Config
from rose.playlists import (
    add_track_to_playlist,
    create_playlist,
    delete_playlist,
    remove_playlist_cover_art,
    remove_track_from_playlist,
    rename_playlist,
    set_playlist_cover_art,
)
from rose.releases import (
    ReleaseDoesNotExistError,
    delete_release,
    remove_release_cover_art,
    set_release_cover_art,
    toggle_release_new,
)

logger = logging.getLogger(__name__)

# In collages, playlists, and releases, we print directories with position of the release/track in
# the collage. When parsing, strip it out. Otherwise we will have to handle this parsing in every
# method.
POSITION_REGEX = re.compile(r"^([^.]+)\. ")
# In recently added, we print the date that the release was added to the library. When parsing,
# strip it out.
ADDED_AT_REGEX = re.compile(r"^\[[\d-]{10}\] ")


@dataclass
class VirtualPath:
    view: (
        Literal[
            "Root",
            "Releases",
            "Artists",
            "Genres",
            "Labels",
            "Collages",
            "Playlists",
            "New",
            "Recently Added",
        ]
        | None
    )
    artist: str | None = None
    genre: str | None = None
    label: str | None = None
    collage: str | None = None
    playlist: str | None = None
    release: str | None = None
    release_position: str | None = None
    file: str | None = None
    file_position: str | None = None

    @classmethod
    def parse(cls, path: Path, *, parse_release_position: bool = True) -> VirtualPath:
        parts = str(path.resolve()).split("/")[1:]  # First part is always empty string.

        if len(parts) == 1 and parts[0] == "":
            return cls(view="Root")

        if parts[0] == "1. Releases":
            if len(parts) == 1:
                return cls(view="Releases")
            if len(parts) == 2:
                return cls(view="Releases", release=parts[1])
            if len(parts) == 3:
                return cls(
                    view="Releases",
                    release=parts[1],
                    file=POSITION_REGEX.sub("", parts[2]),
                    file_position=m[1] if (m := POSITION_REGEX.match(parts[2])) else None,
                )
            raise llfuse.FUSEError(errno.ENOENT)

        if parts[0] == "2. Releases - New":
            if len(parts) == 1:
                return cls(view="New")
            if len(parts) == 2:
                return cls(view="New", release=parts[1])
            if len(parts) == 3:
                return cls(
                    view="New",
                    release=parts[1],
                    file=POSITION_REGEX.sub("", parts[2]),
                    file_position=m[1] if (m := POSITION_REGEX.match(parts[2])) else None,
                )
            raise llfuse.FUSEError(errno.ENOENT)

        if parts[0] == "3. Releases - Recently Added":
            if len(parts) == 1:
                return cls(view="Recently Added")
            if len(parts) == 2 and ADDED_AT_REGEX.match(parts[1]):
                return cls(view="Recently Added", release=ADDED_AT_REGEX.sub("", parts[1]))
            if len(parts) == 3 and ADDED_AT_REGEX.match(parts[1]):
                return cls(
                    view="Recently Added",
                    release=ADDED_AT_REGEX.sub("", parts[1]),
                    file=POSITION_REGEX.sub("", parts[2]),
                    file_position=m[1] if (m := POSITION_REGEX.match(parts[2])) else None,
                )
            raise llfuse.FUSEError(errno.ENOENT)

        if parts[0] == "4. Artists":
            if len(parts) == 1:
                return cls(view="Artists")
            if len(parts) == 2:
                return cls(view="Artists", artist=parts[1])
            if len(parts) == 3:
                return cls(view="Artists", artist=parts[1], release=parts[2])
            if len(parts) == 4:
                return cls(
                    view="Artists",
                    artist=parts[1],
                    release=parts[2],
                    file=POSITION_REGEX.sub("", parts[3]),
                    file_position=m[1] if (m := POSITION_REGEX.match(parts[3])) else None,
                )
            raise llfuse.FUSEError(errno.ENOENT)

        if parts[0] == "5. Genres":
            if len(parts) == 1:
                return cls(view="Genres")
            if len(parts) == 2:
                return cls(view="Genres", genre=parts[1])
            if len(parts) == 3:
                return cls(view="Genres", genre=parts[1], release=parts[2])
            if len(parts) == 4:
                return cls(
                    view="Genres",
                    genre=parts[1],
                    release=parts[2],
                    file=POSITION_REGEX.sub("", parts[3]),
                    file_position=m[1] if (m := POSITION_REGEX.match(parts[3])) else None,
                )
            raise llfuse.FUSEError(errno.ENOENT)

        if parts[0] == "6. Labels":
            if len(parts) == 1:
                return cls(view="Labels")
            if len(parts) == 2:
                return cls(view="Labels", label=parts[1])
            if len(parts) == 3:
                return cls(view="Labels", label=parts[1], release=parts[2])
            if len(parts) == 4:
                return cls(
                    view="Labels",
                    label=parts[1],
                    release=parts[2],
                    file=POSITION_REGEX.sub("", parts[3]),
                    file_position=m[1] if (m := POSITION_REGEX.match(parts[3])) else None,
                )
            raise llfuse.FUSEError(errno.ENOENT)

        if parts[0] == "7. Collages":
            if len(parts) == 1:
                return cls(view="Collages")
            if len(parts) == 2:
                return cls(view="Collages", collage=parts[1])
            if len(parts) == 3:
                return cls(
                    view="Collages",
                    collage=parts[1],
                    release=POSITION_REGEX.sub("", parts[2])
                    if parse_release_position
                    else parts[2],
                    release_position=m[1]
                    if parse_release_position and (m := POSITION_REGEX.match(parts[2]))
                    else None,
                )
            if len(parts) == 4:
                return cls(
                    view="Collages",
                    collage=parts[1],
                    release=POSITION_REGEX.sub("", parts[2])
                    if parse_release_position
                    else parts[2],
                    release_position=m[1]
                    if parse_release_position and (m := POSITION_REGEX.match(parts[2]))
                    else None,
                    file=POSITION_REGEX.sub("", parts[3]),
                    file_position=m[1] if (m := POSITION_REGEX.match(parts[3])) else None,
                )
            raise llfuse.FUSEError(errno.ENOENT)

        if parts[0] == "8. Playlists":
            if len(parts) == 1:
                return cls(view="Playlists")
            if len(parts) == 2:
                return cls(view="Playlists", playlist=parts[1])
            if len(parts) == 3:
                return cls(
                    view="Playlists",
                    playlist=parts[1],
                    file=POSITION_REGEX.sub("", parts[2]),
                    file_position=m[1] if (m := POSITION_REGEX.match(parts[2])) else None,
                )
            raise llfuse.FUSEError(errno.ENOENT)

        raise llfuse.FUSEError(errno.ENOENT)


class CanShower:
    """
    I'm great at naming things. This is "can show"-er, determining whether we can show an
    artist/genre/label based on the configured whitelists and blacklists.
    """

    def __init__(self, config: Config):
        self._config = config
        self._artist_w = None
        self._artist_b = None
        self._genre_w = None
        self._genre_b = None
        self._label_w = None
        self._label_b = None

        if config.fuse_artists_whitelist:
            self._artist_w = set(config.fuse_artists_whitelist)
        if config.fuse_artists_blacklist:
            self._artist_b = set(config.fuse_artists_blacklist)
        if config.fuse_genres_whitelist:
            self._genre_w = set(config.fuse_genres_whitelist)
        if config.fuse_genres_blacklist:
            self._genre_b = set(config.fuse_genres_blacklist)
        if config.fuse_labels_whitelist:
            self._label_w = set(config.fuse_labels_whitelist)
        if config.fuse_labels_blacklist:
            self._label_b = set(config.fuse_labels_blacklist)

    def artist(self, artist: str) -> bool:
        if self._artist_w:
            return artist in self._artist_w
        elif self._artist_b:
            return artist not in self._artist_b
        return True

    def genre(self, genre: str) -> bool:
        if self._genre_w:
            return genre in self._genre_w
        elif self._genre_b:
            return genre not in self._genre_b
        return True

    def label(self, label: str) -> bool:
        if self._label_w:
            return label in self._label_w
        elif self._label_b:
            return label not in self._label_b
        return True


class UnknownFileHandleError(RoseError):
    pass


class FileHandleManager:
    """
    FileDescriptorGenerator generates file descriptors and handles wrapping so that we do not go
    over the int size. Assumes that we do not cycle 10k file descriptors before the first descriptor
    is released.
    """

    def __init__(self) -> None:
        self._state = 10
        # Fake sentinel for file handler. The VirtualFS class implements this file handle as a black
        # hole.
        self.dev_null = 9
        # We translate Rose's Virtual Filesystem file handles to the host machine file handles. This
        # means that every file handle from the host system has a corresponding "wrapper" file
        # handle in Rose, and we only return Rose's file handles from the virtual fs.
        #
        # When we receive a Rose file handle that maps to a host filesystem operation, we "unwrap"
        # the file handle back to the host file handle, and then use it.
        #
        # This prevents any accidental collisions, where Rose generates a file handle that ends up
        # being the same number as a file handle that the host system generates.
        self._rose_to_host_map: dict[int, int] = {}

    def next(self) -> int:
        self._state = max(10, self._state + 1 % 10_000)
        return self._state

    def wrap_host(self, host_fh: int) -> int:
        rose_fh = self.next()
        self._rose_to_host_map[rose_fh] = host_fh
        return rose_fh

    def unwrap_host(self, rose_fh: int) -> int:
        try:
            return self._rose_to_host_map[rose_fh]
        except KeyError as e:
            raise llfuse.FUSEError(errno.EBADF) from e


# These are tight regexes for the prefixes that may be applied to releases in the virtual
# filesystem. When we want to use a release name that originated from another view (e.g. because of
# a `cp -p`) command, we need these regexes in order to support names that originated from views
# like Recently Added or Collages.
#
# And if these happen to match an artist name... they're probably not worth listening to anyways
# lol. Probably some vaporwave bullshit.
RELEASE_PREFIX_STRIPPERS = [
    re.compile(r"^"),
    re.compile(r"^\[\d{4}-\d{2}-\d{2}\] "),
    re.compile(r"^\d+\. "),
]

FileCreationSpecialOp = Literal["add-track-to-playlist", "new-cover-art"]


class RoseLogicalCore:
    def __init__(self, config: Config, fhandler: FileHandleManager):
        self.config = config
        self.fhandler = fhandler
        self.can_show = CanShower(config)
        # This map stores the state for "file creation" operations. We currently have two file
        # creation operations:
        #
        # 1. Add Track to Playlist: Because track filenames are not globally unique, the best way to
        #    figure out the track ID is to record the data written, and then parse the written bytes
        #    to find the track ID.
        # 2. New Cover Art: When replacing the cover art of a release or playlist, the new cover art
        #    may have a different "filename" from the virtual `cover.{ext}` filename. We accept any
        #    of the supported filenames as configured by the user. When a new file matching the
        #    cover art filenames is written, it replaces the existing cover art.
        #
        # In order to be able to inspect the written bytes, we must store state across several
        # syscalls (open, write, release). So the process goes:
        #
        # 1. Upon file open, if the syscall matches one of the supported file creation operations,
        #    store the file descriptor in this map instead.
        # 2. On subsequent write requests to the same path and sentinel file descriptor, take the
        #    bytes-to-write and store them in the map.
        # 3. On release, process the written bytes and execute the real operation against the music
        #    library.
        #
        # The state is a mapping of fh -> (operation, identifier, ext, bytes). Identifier is typed
        # based on the operation, and is used to identify the playlist/release being modified.
        self.file_creation_special_ops: dict[
            int, tuple[FileCreationSpecialOp, Any, str, bytearray]
        ] = {}
        # We want to trigger a cache update whenever we notice that a file has been updated through
        # the virtual filesystem. To do this, we insert the file handle and release ID on open, and
        # then trigger the cache update on release. We use this variable to transport that state
        # between the two syscalls.
        self.update_release_on_fh_close: dict[int, str] = {}
        super().__init__()

    @staticmethod
    def stat(mode: Literal["dir", "file"], realpath: Path | None = None) -> dict[str, Any]:
        attrs: dict[str, Any] = {}
        attrs["st_mode"] = (stat.S_IFDIR | 0o755) if mode == "dir" else (stat.S_IFREG | 0o644)
        attrs["st_nlink"] = 4
        attrs["st_uid"] = os.getuid()
        attrs["st_gid"] = os.getgid()

        attrs["st_size"] = 4096
        attrs["st_atime_ns"] = 0.0
        attrs["st_mtime_ns"] = 0.0
        attrs["st_ctime_ns"] = 0.0
        if realpath:
            s = realpath.stat()
            attrs["st_size"] = s.st_size
            attrs["st_atime_ns"] = s.st_atime
            attrs["st_mtime_ns"] = s.st_mtime
            attrs["st_ctime_ns"] = s.st_ctime

        return attrs

    def getattr(self, p: VirtualPath) -> dict[str, Any]:
        logger.debug(f"LOGICAL: Received getattr for {p=}")

        # Common logic that gets called for each release.
        def getattr_release(rp: Path) -> dict[str, Any]:
            assert p.release is not None
            # If no file, return stat for the release dir.
            if not p.file:
                return self.stat("dir", rp)
            # If there is a file, getattr the file.
            if tp := track_exists(self.config, p.release, p.file):
                return self.stat("file", tp)
            if cp := cover_exists(self.config, p.release, p.file):
                return self.stat("file", cp)
            # If no file matches, return errno.ENOENT.
            raise llfuse.FUSEError(errno.ENOENT)

        # 8. Playlists
        if p.playlist:
            try:
                playlist, tracks = get_playlist(self.config, p.playlist)  # type: ignore
            except TypeError as e:
                raise llfuse.FUSEError(errno.ENOENT) from e
            if p.file:
                if p.file_position:
                    for idx, track in enumerate(tracks):
                        if track.virtual_filename == p.file and idx + 1 == int(p.file_position):
                            return self.stat("file", track.source_path)
                if playlist.cover_path and f"cover{playlist.cover_path.suffix}" == p.file:
                    return self.stat("file", playlist.cover_path)
                raise llfuse.FUSEError(errno.ENOENT)
            return self.stat("dir")

        # 7. Collages
        if p.collage:
            if not collage_exists(self.config, p.collage):
                raise llfuse.FUSEError(errno.ENOENT)
            if p.release:
                for _, virtual_dirname, src_path in list_collage_releases(self.config, p.collage):
                    if virtual_dirname == p.release:
                        return getattr_release(src_path)
                raise llfuse.FUSEError(errno.ENOENT)
            return self.stat("dir")

        # 6. Labels
        if p.label:
            if not label_exists(self.config, p.label) or not self.can_show.label(p.label):
                raise llfuse.FUSEError(errno.ENOENT)
            if p.release:
                for r in list_releases(self.config, sanitized_label_filter=p.label):
                    if r.virtual_dirname == p.release:
                        return getattr_release(r.source_path)
                raise llfuse.FUSEError(errno.ENOENT)
            return self.stat("dir")

        # 5. Genres
        if p.genre:
            if not genre_exists(self.config, p.genre) or not self.can_show.genre(p.genre):
                raise llfuse.FUSEError(errno.ENOENT)
            if p.release:
                for r in list_releases(self.config, sanitized_genre_filter=p.genre):
                    if r.virtual_dirname == p.release:
                        return getattr_release(r.source_path)
                raise llfuse.FUSEError(errno.ENOENT)
            return self.stat("dir")

        # 4. Artists
        if p.artist:
            if not artist_exists(self.config, p.artist) or not self.can_show.artist(p.artist):
                raise llfuse.FUSEError(errno.ENOENT)
            if p.release:
                for r in list_releases(self.config, sanitized_artist_filter=p.artist):
                    if r.virtual_dirname == p.release:
                        return getattr_release(r.source_path)
                raise llfuse.FUSEError(errno.ENOENT)
            return self.stat("dir")

        # {1,2,3}. Releases
        if p.release:
            if p.view == "New" and not p.release.startswith("{NEW} "):
                raise llfuse.FUSEError(errno.ENOENT)
            if rp := release_exists(self.config, p.release):
                return getattr_release(rp)
            raise llfuse.FUSEError(errno.ENOENT)

        # 0. Root
        elif p.view:
            return self.stat("dir")

        # -1. Wtf are you doing here?
        raise llfuse.FUSEError(errno.ENOENT)

    def readdir(self, p: VirtualPath) -> Iterator[tuple[str, dict[str, Any]]]:
        logger.debug(f"LOGICAL: Received readdir for {p=}")

        # Call getattr to validate existence. We can now assume that the provided path exists. This
        # for example includes checks that a given album belongs to the artist/genre/label/collage
        # its nested under.
        logger.debug(f"LOGICAL: Invoking getattr in readdir to validate existence of {p}")
        self.getattr(p)

        yield from [
            (".", self.stat("dir")),
            ("..", self.stat("dir")),
        ]

        if p.view == "Root":
            yield from [
                ("1. Releases", self.stat("dir")),
                ("2. Releases - New", self.stat("dir")),
                ("3. Releases - Recently Added", self.stat("dir")),
                ("4. Artists", self.stat("dir")),
                ("5. Genres", self.stat("dir")),
                ("6. Labels", self.stat("dir")),
                ("7. Collages", self.stat("dir")),
                ("8. Playlists", self.stat("dir")),
            ]
            return

        if p.release:
            if cachedata := get_release(self.config, p.release):
                release, tracks = cachedata
                for track in tracks:
                    filename = f"{track.formatted_release_position}. {track.virtual_filename}"
                    yield filename, self.stat("file", track.source_path)
                if release.cover_image_path:
                    yield release.cover_image_path.name, self.stat("file", release.cover_image_path)
                return
            raise llfuse.FUSEError(errno.ENOENT)

        if p.artist or p.genre or p.label or p.view == "Releases" or p.view == "New":
            for release in list_releases(
                self.config,
                sanitized_artist_filter=p.artist,
                sanitized_genre_filter=p.genre,
                sanitized_label_filter=p.label,
                new=True if p.view == "New" else None,
            ):
                yield release.virtual_dirname, self.stat("dir", release.source_path)
            return

        if p.view == "Recently Added":
            for release in list_releases(self.config):
                dirname = f"[{release.added_at[:10]}] {release.virtual_dirname}"
                yield dirname, self.stat("dir", release.source_path)
            return

        elif p.view == "Artists":
            for artist, sanitized_artist in list_artists(self.config):
                if not self.can_show.artist(artist):
                    continue
                yield sanitized_artist, self.stat("dir")
            return

        if p.view == "Genres":
            for genre, sanitized_genre in list_genres(self.config):
                if not self.can_show.genre(genre):
                    continue
                yield sanitized_genre, self.stat("dir")
            return

        if p.view == "Labels":
            for label, sanitized_label in list_labels(self.config):
                if not self.can_show.label(label):
                    continue
                yield sanitized_label, self.stat("dir")
            return

        if p.view == "Collages" and p.collage:
            releases = list(list_collage_releases(self.config, p.collage))
            # Two zeros because `max(single_arg)` assumes that the single_arg is enumerable.
            pad_size = max(0, 0, *[len(str(r[0])) for r in releases])
            for idx, virtual_dirname, source_dir in releases:
                v = f"{str(idx).zfill(pad_size)}. {virtual_dirname}"
                yield v, self.stat("dir", source_dir)
            return

        if p.view == "Collages":
            # Don't need to sanitize because the collage names come from filenames.
            for collage in list_collages(self.config):
                yield collage, self.stat("dir")
            return

        if p.view == "Playlists" and p.playlist:
            pdata = get_playlist(self.config, p.playlist)
            if pdata is None:
                raise llfuse.FUSEError(errno.ENOENT)
            playlist, tracks = pdata
            pad_size = max(0, 0, *[len(str(i + 1)) for i, _ in enumerate(tracks)])
            for idx, track in enumerate(tracks):
                v = f"{str(idx+1).zfill(pad_size)}. {track.virtual_filename}"
                yield v, self.stat("file", track.source_path)
            if playlist.cover_path:
                v = f"cover{playlist.cover_path.suffix}"
                yield v, self.stat("file", playlist.cover_path)
            return

        if p.view == "Playlists":
            # Don't need to sanitize because the playlist names come from filenames.
            for pname in list_playlists(self.config):
                yield pname, self.stat("dir")
            return

        raise llfuse.FUSEError(errno.ENOENT)

    def unlink(self, p: VirtualPath) -> None:
        logger.debug(f"LOGICAL: Received unlink for {p=}")

        # Possible actions:
        # 1. Delete a playlist.
        # 2. Delete a track from a playlist.
        # 3. Delete cover art from a playlist.
        # 4. Delete cover art from a release.
        if p.view == "Playlists" and p.playlist and p.file is None:
            delete_playlist(self.config, p.playlist)
            return
        if (
            p.view == "Playlists"
            and p.playlist
            and p.file
            and p.file_position
            and (pdata := get_playlist(self.config, p.playlist))
        ):
            for idx, track in enumerate(pdata[1]):
                if track.virtual_filename == p.file and idx + 1 == int(p.file_position):
                    remove_track_from_playlist(self.config, p.playlist, track.id)
                    return
            raise llfuse.FUSEError(errno.ENOENT)
        if (
            p.view == "Playlists"
            and p.playlist
            and p.file
            and p.file.lower() in self.config.valid_cover_arts
            and (pdata := get_playlist(self.config, p.playlist))
        ):
            remove_playlist_cover_art(self.config, pdata[0].name)
        if (
            p.release
            and p.file
            and p.file.lower() in self.config.valid_cover_arts
            and (rdata := get_release(self.config, p.release))
        ):
            remove_release_cover_art(self.config, rdata[0].id)

        # Otherwise, noop. If we return an error, that prevents rmdir from being called when we rm.

    def mkdir(self, p: VirtualPath) -> None:
        logger.debug(f"LOGICAL: Received mkdir for {p=}")

        # Possible actions:
        # 1. Add a release to an existing collage.
        # 2. Create a new collage.
        # 3. Create a new playlist.
        if p.collage and p.release is None:
            create_collage(self.config, p.collage)
            return
        if p.collage and p.release:
            err = None
            # Because some releases have prefixes, attempt multiple times with each different prefix
            # stripper.
            seen: set[str] = set()
            for prefix_stripper in RELEASE_PREFIX_STRIPPERS:
                rls = prefix_stripper.sub("", p.release)
                # But don't waste effort if nothing changed.
                if rls in seen:
                    continue
                seen.add(rls)
                try:
                    add_release_to_collage(self.config, p.collage, rls)
                    return
                except ReleaseDoesNotExistError as e:
                    err = e
            logger.debug(
                f"LOGICAL: Failed adding release {p.release} to collage {p.collage}: "
                "release not found"
            )
            raise llfuse.FUSEError(errno.ENOENT) from err
        if p.playlist and p.file is None:
            create_playlist(self.config, p.playlist)
            return

        raise llfuse.FUSEError(errno.EACCES)

    def rmdir(self, p: VirtualPath) -> None:
        logger.debug(f"LOGICAL: Received rmdir for {p=}")

        # Possible actions:
        # 1. Delete a collage.
        # 2. Delete a release from an existing collage.
        # 3. Delete a playlist.
        # 4. Delete a release.
        if p.view == "Collages" and p.collage and p.release is None:
            delete_collage(self.config, p.collage)
            return
        if p.view == "Collages" and p.collage and p.release:
            remove_release_from_collage(self.config, p.collage, p.release)
            return
        if p.view == "Playlists" and p.playlist and p.file is None:
            delete_playlist(self.config, p.playlist)
            return
        if p.view != "Collages" and p.release is not None:
            delete_release(self.config, p.release)
            return

        raise llfuse.FUSEError(errno.EACCES)

    def rename(self, old: VirtualPath, new: VirtualPath) -> None:
        logger.debug(f"LOGICAL: Received rename for {old=} {new=}")

        # Possible actions:
        # 1. Toggle a release's new status.
        # 2. Rename a collage.
        # 3. Rename a playlist.
        # TODO: Consider allowing renaming artist/genre/label here?
        if (
            (old.release and new.release)
            and old.release.removeprefix("{NEW} ") == new.release.removeprefix("{NEW} ")
            and (not old.file and not new.file)
            and old.release.startswith("{NEW} ") != new.release.startswith("{NEW} ")
        ):
            toggle_release_new(self.config, old.release)
            return
        if (
            old.view == "Collages"
            and new.view == "Collages"
            and (old.collage and new.collage)
            and old.collage != new.collage
            and (not old.release and not new.release)
        ):
            rename_collage(self.config, old.collage, new.collage)
            return
        if (
            old.view == "Playlists"
            and new.view == "Playlists"
            and (old.playlist and new.playlist)
            and old.playlist != new.playlist
            and (not old.file and not new.file)
        ):
            rename_playlist(self.config, old.playlist, new.playlist)
            return

        raise llfuse.FUSEError(errno.EACCES)

    def open(self, p: VirtualPath, flags: int) -> int:
        logger.debug(f"LOGICAL: Received open for {p=} {flags=}")

        err = errno.ENOENT
        if flags & os.O_CREAT == os.O_CREAT:
            err = errno.EACCES

        if p.release and p.file and (rdata := get_release(self.config, p.release)):
            release, tracks = rdata
            # If the file is a music file, handle it as a music file.
            pf = Path(p.file)
            if pf.suffix.lower() in SUPPORTED_AUDIO_EXTENSIONS:
                for track in tracks:
                    if track.virtual_filename == p.file:
                        fh = self.fhandler.wrap_host(os.open(str(track.source_path), flags))
                        if flags & os.O_WRONLY == os.O_WRONLY or flags & os.O_RDWR == os.O_RDWR:
                            self.update_release_on_fh_close[fh] = track.release_id
                        return fh
            # If the file matches the current cover image, then simply pass it through.
            if release.cover_image_path and p.file == f"cover{release.cover_image_path.suffix}":
                return self.fhandler.wrap_host(os.open(str(release.cover_image_path), flags))
            # Otherwise, if we are writing a brand new cover image, initiate the "new-cover-art"
            # sequence.
            if p.file.lower() in self.config.valid_cover_arts and flags & os.O_CREAT == os.O_CREAT:
                fh = self.fhandler.next()
                logger.debug(
                    f"LOGICAL: Begin new cover art sequence for release "
                    f"{release.virtual_dirname=}, {p.file=}, and {fh=}"
                )
                self.file_creation_special_ops[fh] = (
                    "new-cover-art",
                    ("release", release.id),
                    pf.suffix,
                    bytearray(),
                )
                return fh
            raise llfuse.FUSEError(err)
        if p.playlist and p.file:
            try:
                playlist, tracks = get_playlist(self.config, p.playlist)  # type: ignore
            except TypeError as e:
                raise llfuse.FUSEError(errno.ENOENT) from e
            # If we are trying to create an audio file in the playlist, enter the
            # "add-track-to-playlist" operation sequence. See the __init__ for more details.
            pf = Path(p.file)
            if pf.suffix.lower() in SUPPORTED_AUDIO_EXTENSIONS and flags & os.O_CREAT == os.O_CREAT:
                fh = self.fhandler.next()
                logger.debug(
                    f"LOGICAL: Begin playlist addition operation sequence for "
                    f"{playlist.name=}, {p.file=}, and {fh=}"
                )
                self.file_creation_special_ops[fh] = (
                    "add-track-to-playlist",
                    p.playlist,
                    pf.suffix,
                    bytearray(),
                )
                return fh
            # If we are trying to create a cover image in the playlist, enter the "new-cover-art"
            # sequence for the playlist.
            if p.file.lower() in self.config.valid_cover_arts and flags & os.O_CREAT == os.O_CREAT:
                fh = self.fhandler.next()
                logger.debug(
                    f"LOGICAL: Begin new cover art sequence for playlist"
                    f"{playlist.name=}, {p.file=}, and {fh=}"
                )
                self.file_creation_special_ops[fh] = (
                    "new-cover-art",
                    ("playlist", p.playlist),
                    pf.suffix,
                    bytearray(),
                )
                return fh
            # Otherwise, continue on...
            if p.file_position:
                for idx, track in enumerate(tracks):
                    if track.virtual_filename == p.file and idx + 1 == int(p.file_position):
                        fh = self.fhandler.wrap_host(os.open(str(track.source_path), flags))
                        if flags & os.O_WRONLY == os.O_WRONLY or flags & os.O_RDWR == os.O_RDWR:
                            self.update_release_on_fh_close[fh] = track.release_id
                        return fh
            if playlist.cover_path and f"cover{playlist.cover_path.suffix}" == p.file:
                return self.fhandler.wrap_host(os.open(playlist.cover_path, flags))
            raise llfuse.FUSEError(err)

        raise llfuse.FUSEError(err)

    def read(self, fh: int, offset: int, length: int) -> bytes:
        logger.debug(f"LOGICAL: Received read for {fh=} {offset=} {length=}")
        if sop := self.file_creation_special_ops.get(fh, None):
            logger.debug("LOGICAL: Matched read to a file creation special op")
            _, _, _, b = sop
            return b[offset : offset + length]
        fh = self.fhandler.unwrap_host(fh)
        os.lseek(fh, offset, os.SEEK_SET)
        return os.read(fh, length)

    def write(self, fh: int, offset: int, data: bytes) -> int:
        logger.debug(f"LOGICAL: Received write for {fh=} {offset=} {len(data)=}")
        if sop := self.file_creation_special_ops.get(fh, None):
            logger.debug("LOGICAL: Matched write to a file creation special op")
            _, _, _, b = sop
            del b[offset:]
            b.extend(data)
            return len(data)
        fh = self.fhandler.unwrap_host(fh)
        os.lseek(fh, offset, os.SEEK_SET)
        return os.write(fh, data)

    def release(self, fh: int) -> None:
        logger.debug(f"LOGICAL: Received release for {fh=}")
        if sop := self.file_creation_special_ops.get(fh, None):
            logger.debug("LOGICAL: Matched release to a file creation special op")
            operation, ident, ext, b = sop
            if not b:
                logger.debug("LOGICAL: Aborting file creation special oprelease: no bytes to write")
                return
            if operation == "add-track-to-playlist":
                logger.debug("LOGICAL: Narrowed file creation special op to add track to playlist")
                playlist = ident
                with tempfile.TemporaryDirectory() as tmpdir:
                    audiopath = Path(tmpdir) / f"f{ext}"
                    with audiopath.open("wb") as fp:
                        fp.write(b)
                    audiofile = AudioTags.from_file(audiopath)
                    track_id = audiofile.id
                if not track_id:
                    logger.warning(
                        "LOGICAL: Failed to parse track_id from file in playlist addition "
                        f"operation sequence: {track_id=} {fh=} {playlist=} {audiofile}"
                    )
                    return
                add_track_to_playlist(self.config, playlist, track_id)
                del self.file_creation_special_ops[fh]
                return
            if operation == "new-cover-art":
                entity_type, entity_id = ident
                if entity_type == "release":
                    logger.debug(
                        "LOGICAL: Narrowed file creation special op to write release cover art"
                    )
                    with tempfile.TemporaryDirectory() as tmpdir:
                        imagepath = Path(tmpdir) / f"f{ext}"
                        with imagepath.open("wb") as fp:
                            fp.write(b)
                        set_release_cover_art(self.config, entity_id, imagepath)
                    del self.file_creation_special_ops[fh]
                    return
                if entity_type == "playlist":
                    logger.debug(
                        "LOGICAL: Narrowed file creation special op to write playlist cover art"
                    )
                    with tempfile.TemporaryDirectory() as tmpdir:
                        imagepath = Path(tmpdir) / f"f{ext}"
                        with imagepath.open("wb") as fp:
                            fp.write(b)
                        set_playlist_cover_art(self.config, entity_id, imagepath)
                    del self.file_creation_special_ops[fh]
                    return
            raise RoseError(f"Impossible: unknown file creation special op: {operation=} {ident=}")
        if release_id := self.update_release_on_fh_close.get(fh, None):
            logger.debug(
                f"LOGICAL: Triggering cache update for release {release_id} after release syscall"
            )
            if source_path := get_release_source_path_from_id(self.config, release_id):
                update_cache_for_releases(self.config, [source_path])
        fh = self.fhandler.unwrap_host(fh)
        os.close(fh)


class INodeManager:
    """
    INodeManager manages the mapping of inodes to paths in our filesystem. We have this because the
    llfuse library makes us manage the inodes...
    """

    def __init__(self, config: Config):
        self.config = config

        self._inode_to_path_map: dict[int, Path] = {llfuse.ROOT_INODE: Path("/")}
        self._path_to_inode_map: dict[str, int] = {"/": llfuse.ROOT_INODE}
        self._next_inode_ctr: int = llfuse.ROOT_INODE + 1

    def _next_inode(self) -> int:
        # Increment to infinity.
        cur = self._next_inode_ctr
        self._next_inode_ctr += 1
        return cur

    def get_path(self, inode: int, name: bytes | None = None) -> Path:
        """
        Raises ENOENT if the inode doesn't exist. If the inode is of a directory, you can optionally
        pass `name`, which will be concatenated to the directory.
        """
        try:
            path = self._inode_to_path_map[inode]
            if not name or name == b".":
                return path
            if name == b"..":
                return path.parent
            return path / name.decode()
        except KeyError as e:
            raise llfuse.FUSEError(errno.ENOENT) from e

    def calc_inode(self, path: Path) -> int:
        """
        Get the inode of a path. If we've seen the path before, return the cached inode. Otherwise,
        generate a new inode and cache it for future accesses.
        """
        path = path.resolve()
        spath = str(path)
        try:
            return self._path_to_inode_map[spath]
        except KeyError:
            inode = self._next_inode()
            self._path_to_inode_map[spath] = inode
            self._inode_to_path_map[inode] = path
            return inode

    def remove_path(self, path: Path) -> None:
        spath = str(path.resolve())
        try:
            inode = self._path_to_inode_map[spath]
        except KeyError:
            return
        del self._path_to_inode_map[spath]
        del self._inode_to_path_map[inode]

    def rename_path(self, old_path: Path, new_path: Path) -> None:
        sold = str(old_path.resolve())
        snew = str(new_path.resolve())
        try:
            inode = self._path_to_inode_map[sold]
        except KeyError:
            return
        self._inode_to_path_map[inode] = new_path
        self._path_to_inode_map[snew] = inode
        del self._path_to_inode_map[sold]


class VirtualFS(llfuse.Operations):  # type: ignore
    """
    This is the virtual filesystem class, which implements commands by delegating the Rose-specific
    logic to RoseLogicalFS and the inode/fd<->path tracking to INodeManager. This architecture
    allows us to have a fairly clean logical implementation for Rose despite a fairly low-level
    llfuse library.
    """

    def __init__(self, config: Config):
        self.fhandler = FileHandleManager()
        self.rose = RoseLogicalCore(config, self.fhandler)
        self.inodes = INodeManager(config)
        self.default_attrs = {
            # Well, this should be ok for now. I really don't want to track this... we indeed change
            # inodes across FS restarts.
            "generation": random.randint(0, 1000000),
            # Have a 30 second entry timeout by default.
            "entry_timeout": 30,
        }
        # We cache some items for getattr and lookup for performance reasons--after a ls, getattr is
        # serially called for each item in the directory, and sequential 1k SQLite reads is quite
        # slow in any universe. So whenever we have a readdir, we do a batch read and populate the
        # getattr and lookup caches. The cache is valid for only 2 seconds, which prevents stale
        # results from being read from it.
        #
        # The dict is a map of paths to entry attributes.
        self.getattr_cache: cachetools.TTLCache[int, llfuse.EntryAttributes]
        self.lookup_cache: cachetools.TTLCache[tuple[int, bytes], llfuse.EntryAttributes]
        self.reset_getattr_caches()
        # We handle state for readdir calls here. Because programs invoke readdir multiple times
        # with offsets, we end up with many readdir calls for a single directory. However, we do not
        # want to actually invoke the logical Rose readdir call that many times. So we load it once
        # in `opendir`, associate the results with a file handle, and yield results from that handle
        # in `readdir`. We delete the state in `releasedir`.
        #
        # Map of file handle -> (parent inode, child name, child attributes).
        self.readdir_cache: dict[int, list[tuple[int, bytes, llfuse.EntryAttributes]]] = {}
        # Ghost Files: We pretend some files exist in the filesystem, despite them not actually
        # existing. We do this in order to be compatible with the expectations that tools have for
        # filesystems.
        #
        # For example, when we use file writing to add a file to a playlist, that file is
        # immediately renamed to its correct playlist-specific filename upon release. However, `cp`
        # exits with an error, for it followed up the release with an attempt to set file
        # permissions and attributes on a now non-existent file.
        #
        # In order to pretend to tools that we are a Real Filesystem and not some shitty hack of a
        # filesystem, we have these ghost files that exist for a period of time following an
        # operation.
        #
        # There are 2 types of ghost files right now:
        #
        # 1. Ghost Existing File: We pretend these files exist.
        # 2. Ghost Writable Empty Directory: We pretend these directories are empty and can have
        #    arbitrary files created in them.
        #
        # Ghost Existing Files take precedence over Ghost Writeable Empty Directory, for we also
        # flag files as Ghost Existing Files _after_ they've been written to in a Ghost Writable
        # Empty Directory.
        #
        # These maps are Rob Pike style set of paths...
        self.ghost_existing_files: cachetools.TTLCache[str, bool] = cachetools.TTLCache(
            maxsize=9999, ttl=2
        )
        self.ghost_writable_empty_directory: cachetools.TTLCache[str, bool] = cachetools.TTLCache(
            maxsize=9999, ttl=5
        )

    def reset_getattr_caches(self) -> None:
        # When a write happens, clear these caches. These caches are very short-lived and intended
        # to make readdir's subsequent getattrs more performant, so this is harmless.
        self.getattr_cache = cachetools.TTLCache(maxsize=99999, ttl=1)
        self.lookup_cache = cachetools.TTLCache(maxsize=99999, ttl=1)

    def make_entry_attributes(self, attrs: dict[str, Any]) -> llfuse.EntryAttributes:
        for k, v in self.default_attrs.items():
            if k not in attrs:
                attrs[k] = v
        entry = llfuse.EntryAttributes()
        for k, v in attrs.items():
            setattr(entry, k, v)
        return entry

    def getattr(self, inode: int, _: Any) -> llfuse.EntryAttributes:
        logger.debug(f"FUSE: Received getattr for {inode=}")
        # For performance, pull from the getattr cache if possible.
        with contextlib.suppress(KeyError):
            attrs = self.getattr_cache[inode]
            logger.debug(f"FUSE: Resolved getattr for {inode=} to {attrs.__getstate__()=}")
            return attrs
        spath = self.inodes.get_path(inode)
        logger.debug(f"FUSE: Resolved getattr {inode=} to {spath=}")
        # If this path is a ghost file path; pretend here!
        if self.ghost_existing_files.get(str(spath), False):
            logger.debug(f"FUSE: Resolved getattr for {spath=} as ghost existing file")
            attrs = self.rose.stat("file")
            attrs["st_ino"] = inode
            return self.make_entry_attributes(attrs)

        vpath = VirtualPath.parse(spath)
        logger.debug(f"FUSE: Parsed getattr {spath=} to {vpath=}")
        try:
            attrs = self.rose.getattr(vpath)
        except OSError as e:
            raise llfuse.FUSEError(e.errno) from e
        attrs["st_ino"] = inode
        return self.make_entry_attributes(attrs)

    def lookup(self, parent_inode: int, name: bytes, _: Any) -> llfuse.EntryAttributes:
        logger.debug(f"FUSE: Received lookup for {parent_inode=}/{name=}")
        # For performance, pull from the lookup cache if possible.
        with contextlib.suppress(KeyError):
            attrs = self.lookup_cache[(parent_inode, name)]
            logger.debug(
                f"FUSE: Resolved lookup {parent_inode=}/{name=} to {attrs.__getstate__()=}"
            )
            return attrs
        spath = self.inodes.get_path(parent_inode, name)
        inode = self.inodes.calc_inode(spath)
        logger.debug(f"FUSE: Resolved lookup {parent_inode=}/{name=} to {spath=}")
        # If this path is a ghost file path; pretend here!
        if self.ghost_existing_files.get(str(spath), False):
            logger.debug(f"FUSE: Resolved getattr for {spath=} as ghost existing file")
            attrs = self.rose.stat("file")
            attrs["st_ino"] = inode
            return self.make_entry_attributes(attrs)
        # If this directory is a ghost directory path; pretend here!
        if self.ghost_writable_empty_directory.get(str(spath.parent), False):
            logger.debug(f"FUSE: Resolved lookup for {spath=} as ghost writeable directory")
            raise llfuse.FUSEError(errno.ENOENT)

        vpath = VirtualPath.parse(spath)
        logger.debug(f"FUSE: Parsed lookup {spath=} to {vpath=}")
        try:
            attrs = self.rose.getattr(vpath)
        except OSError as e:
            raise llfuse.FUSEError(e.errno) from e
        attrs["st_ino"] = inode
        return self.make_entry_attributes(attrs)

    def opendir(self, inode: int, _: Any) -> int:
        logger.debug(f"FUSE: Received opendir for {inode=}")
        spath = self.inodes.get_path(inode)
        logger.debug(f"FUSE: Resolved opendir {inode=} to {spath=}")
        # If this directory is a ghost directory path; pretend here!
        if self.ghost_writable_empty_directory.get(str(spath), False):
            logger.debug(f"FUSE: Resolved lookup for {spath=} as ghost writeable directory")
            entries: list[tuple[int, bytes, llfuse.EntryAttributes]] = []
            for node in [".", ".."]:
                attrs = self.rose.stat("dir")
                attrs["st_ino"] = self.inodes.calc_inode(spath / node)
                entry = self.make_entry_attributes(attrs)
                entries.append((inode, node.encode(), entry))
            fh = self.fhandler.next()
            self.readdir_cache[fh] = entries
            return fh

        vpath = VirtualPath.parse(spath)
        logger.debug(f"FUSE: Parsed opendir {spath=} to {vpath=}")
        entries = []
        try:
            for namestr, attrs in self.rose.readdir(vpath):
                name = namestr.encode()
                attrs["st_ino"] = self.inodes.calc_inode(spath / namestr)
                entry = self.make_entry_attributes(attrs)
                entries.append((inode, name, entry))
        except OSError as e:
            raise llfuse.FUSEError(e.errno) from e
        fh = self.fhandler.next()
        self.readdir_cache[fh] = entries
        logger.debug(f"FUSE: Stored {len(entries)=} nodes into the readdir cache for {fh=}")
        return fh

    def releasedir(self, fh: int) -> None:
        with contextlib.suppress(KeyError):
            del self.readdir_cache[fh]

    def readdir(
        self,
        fd: int,
        offset: int = 0,
    ) -> Iterator[tuple[bytes, llfuse.EntryAttributes, int]]:
        logger.debug(f"FUSE: Received readdir for {fd=} {offset=}")
        try:
            entries = self.readdir_cache[fd]
        except KeyError:
            return
        for i, (parent_inode, name, entry) in enumerate(entries[offset:]):
            self.getattr_cache[entry.st_ino] = entry
            self.lookup_cache[(parent_inode, name)] = entry
            yield name, entry, i + offset + 1
            logger.debug(f"FUSE: Yielded entry {i + offset=} in readdir of {fd=}")

    def open(self, inode: int, flags: int, _: Any) -> int:
        logger.debug(f"FUSE: Received open for {inode=} {flags=}")
        spath = self.inodes.get_path(inode)
        logger.debug(f"FUSE: Resolved open {inode=} to {spath=}")
        if self.ghost_writable_empty_directory.get(str(spath.parent), False):
            logger.debug(f"FUSE: Resolved open for {spath=} as ghost writeable directory")
            self.ghost_existing_files[str(spath)] = True
            return self.fhandler.dev_null
        vpath = VirtualPath.parse(spath)
        logger.debug(f"FUSE: Parsed open {spath=} to {vpath=}")
        try:
            fh = self.rose.open(vpath, flags)
        except OSError as e:
            raise llfuse.FUSEError(e.errno) from e
        # If this was a create operation, and Rose succeeded, flag the filepath as a ghost file and
        # _always_ pretend it exists for the following short duration.
        if flags & os.O_CREAT == os.O_CREAT:
            logger.debug(f"FUSE: Setting {spath=} as ghost existing file for next 3 seconds")
            self.ghost_existing_files[str(spath)] = True
        return fh

    def read(self, fh: int, offset: int, length: int) -> bytes:
        logger.debug(f"FUSE: Received read for {fh=} {offset=} {length=}")
        if fh == self.fhandler.dev_null:
            logger.debug(f"FUSE: Matched {fh=} to /dev/null sentinel")
            return b""
        try:
            return self.rose.read(fh, offset, length)
        except OSError as e:
            raise llfuse.FUSEError(e.errno) from e

    def write(self, fh: int, offset: int, data: bytes) -> int:
        logger.debug(f"FUSE: Received write for {fh=} {offset=} {len(data)=}")
        if fh == self.fhandler.dev_null:
            logger.debug(f"FUSE: Matched {fh=} to /dev/null sentinel")
            return len(data)
        try:
            return self.rose.write(fh, offset, data)
        except OSError as e:
            raise llfuse.FUSEError(e.errno) from e

    def release(self, fh: int) -> None:
        logger.debug(f"FUSE: Received release for {fh=}")
        if fh == self.fhandler.dev_null:
            logger.debug(f"FUSE: Matched {fh=} to /dev/null sentinel")
            return
        try:
            self.rose.release(fh)
        except OSError as e:
            raise llfuse.FUSEError(e.errno) from e

    def ftruncate(self, fh: int, length: int = 0) -> None:
        logger.debug(f"FUSE: Received ftruncate for {fh=} {length=}")
        if fh == self.fhandler.dev_null:
            logger.debug(f"FUSE: Matched {fh=} to /dev/null sentinel")
            return
        fh = self.fhandler.unwrap_host(fh)
        return os.ftruncate(fh, length)

    def create(
        self,
        parent_inode: int,
        name: bytes,
        _mode: int,
        flags: int,
        ctx: Any,
    ) -> tuple[int, llfuse.EntryAttributes]:
        logger.debug(f"FUSE: Received create for {parent_inode=}/{name=} {flags=}")
        path = self.inodes.get_path(parent_inode, name)
        logger.debug(f"FUSE: Resolved create {parent_inode=}/{name=} to {path=}")
        inode = self.inodes.calc_inode(path)
        logger.debug(f"FUSE: Created inode {inode=} for {path=}; now delegating to open call")
        try:
            fh = self.open(inode, flags, ctx)
        except OSError as e:
            raise llfuse.FUSEError(e.errno) from e
        self.reset_getattr_caches()
        attrs = self.rose.stat("file")
        attrs["st_ino"] = inode
        return fh, self.make_entry_attributes(attrs)

    def unlink(self, parent_inode: int, name: bytes, _: Any) -> None:
        logger.debug(f"FUSE: Received unlink for {parent_inode=}/{name=}")
        spath = self.inodes.get_path(parent_inode, name)
        logger.debug(f"FUSE: Resolved unlink {parent_inode=}/{name=} to {spath=}")
        vpath = VirtualPath.parse(spath)
        logger.debug(f"FUSE: Parsed unlink {spath=} to {vpath=}")
        try:
            self.rose.unlink(vpath)
        except OSError as e:
            raise llfuse.FUSEError(e.errno) from e
        self.reset_getattr_caches()
        self.inodes.remove_path(spath)

    def mkdir(self, parent_inode: int, name: bytes, _mode: int, _: Any) -> llfuse.EntryAttributes:
        logger.debug(f"FUSE: Received mkdir for {parent_inode=}/{name=}")
        spath = self.inodes.get_path(parent_inode, name)
        logger.debug(f"FUSE: Resolved mkdir {parent_inode=}/{name=} to {spath=}")
        vpath = VirtualPath.parse(spath, parse_release_position=False)
        logger.debug(f"FUSE: Parsed mkdir {spath=} to {vpath=}")
        try:
            self.rose.mkdir(vpath)
        except OSError as e:
            raise llfuse.FUSEError(e.errno) from e
        self.reset_getattr_caches()
        inode = self.inodes.calc_inode(spath)
        # If this was an add to collage operation, then flag the directory as a ghost writeable
        # directory for the following short duration.
        if vpath.collage:
            logger.debug(f"FUSE: Setting {spath=} as ghost writeable directory for next 3 seconds")
            self.ghost_writable_empty_directory[str(spath)] = True
        attrs = self.rose.stat("dir")
        attrs["st_ino"] = inode
        return self.make_entry_attributes(attrs)

    def rmdir(self, parent_inode: int, name: bytes, _: Any) -> None:
        logger.debug(f"FUSE: Received rmdir for {parent_inode=}/{name=}")
        spath = self.inodes.get_path(parent_inode, name)
        logger.debug(f"FUSE: Resolved rmdir {parent_inode=}/{name=} to {spath=}")
        vpath = VirtualPath.parse(spath)
        logger.debug(f"FUSE: Parsed rmdir {spath=} to {vpath=}")
        try:
            self.rose.rmdir(vpath)
        except OSError as e:
            raise llfuse.FUSEError(e.errno) from e
        self.reset_getattr_caches()
        self.inodes.remove_path(spath)

    def rename(
        self,
        old_parent_inode: int,
        old_name: bytes,
        new_parent_inode: int,
        new_name: bytes,
        _: Any,
    ) -> None:
        logger.debug(
            f"FUSE: Received rename for {old_parent_inode=}/{old_name=} "
            f"to {new_parent_inode=}/{new_name=}"
        )
        old_spath = self.inodes.get_path(old_parent_inode, old_name)
        new_spath = self.inodes.get_path(new_parent_inode, new_name)
        logger.debug(
            f"FUSE: Received rename for {old_parent_inode=}/{old_name=} to {old_spath=}"
            f"and for {new_parent_inode=}/{new_name=} to {new_spath=}"
        )
        old_vpath = VirtualPath.parse(old_spath)
        new_vpath = VirtualPath.parse(new_spath)
        logger.debug(
            f"FUSE: Parsed rmdir {old_spath=} to {old_vpath=} and {old_vpath=} to {new_vpath=}"
        )
        try:
            self.rose.rename(old_vpath, new_vpath)
        except OSError as e:
            raise llfuse.FUSEError(e.errno) from e
        self.reset_getattr_caches()
        self.inodes.rename_path(old_spath, new_spath)

    # ============================================================================================
    # Unimplemented stubs. Tools expect these syscalls to exist, so we implement versions of them
    # that do not error, but also do not do anything.
    # ============================================================================================

    def forget(self, inode_list: list[tuple[int, int]]) -> None:
        logger.debug(f"FUSE: Received forget for {inode_list=}")
        # Clear the cache in case someone makes a request later...
        self.reset_getattr_caches()

    def mknod(self, parent_inode: int, name: bytes, _mode: int, _: Any) -> llfuse.EntryAttributes:
        logger.debug(f"FUSE: Received mknod for {parent_inode=}/{name=}")
        attrs = self.rose.stat("file")
        attrs["st_ino"] = self.inodes.calc_inode(self.inodes.get_path(parent_inode, name))
        return self.make_entry_attributes(attrs)

    def flush(self, fh: int) -> None:
        logger.debug(f"FUSE: Received flush for {fh=}")
        pass

    def setattr(
        self,
        inode: int,
        attr: llfuse.EntryAttributes,
        fields: llfuse.SetattrFields,
        fh: int | None,
        ctx: Any,
    ) -> llfuse.EntryAttributes:
        logger.debug(f"FUSE: Received setattr for {inode=} {attr=} {fields=} {fh=}")
        return self.getattr(inode, ctx)

    def getxattr(self, inode: int, name: bytes, _: Any) -> bytes:
        logger.debug(f"FUSE: Received getxattr for {inode=} {name=}")
        raise llfuse.FUSEError(llfuse.ENOATTR)

    def setxattr(self, inode: int, name: bytes, value: bytes, _: Any) -> None:
        logger.debug(f"FUSE: Received setxattr for {inode=} {name=} {value=}")

    def listxattr(self, inode: int, _: Any) -> Iterator[bytes]:
        logger.debug(f"FUSE: Received listxattr for {inode=}")
        return iter([])

    def removexattr(self, inode: int, name: bytes, _: Any) -> None:
        logger.debug(f"FUSE: Received removexattr for {inode=} {name=}")
        raise llfuse.FUSEError(llfuse.ENOATTR)


def mount_virtualfs(c: Config, debug: bool = False) -> None:
    options = set(llfuse.default_options)
    options.add("fsname=rose")
    if debug:
        options.add("debug")
    llfuse.init(VirtualFS(c), str(c.fuse_mount_dir), options)
    try:
        llfuse.main(workers=c.max_proc)
    except:
        llfuse.close()
        raise
    llfuse.close()


def unmount_virtualfs(c: Config) -> None:
    subprocess.run(["umount", str(c.fuse_mount_dir)])
