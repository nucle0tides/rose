"""
The releases module encapsulates all mutations that can occur on release and track entities.
"""

from __future__ import annotations

import json
import logging
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import click
import tomli_w
import tomllib
from send2trash import send2trash

from rose.artiststr import ArtistMapping
from rose.audiotags import AudioTags
from rose.cache import (
    STORED_DATA_FILE_REGEX,
    CachedRelease,
    CachedTrack,
    get_release,
    get_release_id_from_virtual_dirname,
    get_release_source_path_from_id,
    get_release_virtual_dirname_from_id,
    list_releases,
    lock,
    release_lock_name,
    update_cache_evict_nonexistent_releases,
    update_cache_for_collages,
    update_cache_for_releases,
)
from rose.common import InvalidCoverArtFileError, RoseError, valid_uuid
from rose.config import Config

logger = logging.getLogger()


class ReleaseDoesNotExistError(RoseError):
    pass


class UnknownArtistRoleError(RoseError):
    pass


class CustomJSONEncoder(json.JSONEncoder):
    def default(self, obj: Any) -> Any:
        if isinstance(obj, Path):
            return str(obj)
        return super().default(obj)


def dump_releases(c: Config) -> str:
    releases = [asdict(r) for r in list_releases(c)]
    return json.dumps(releases, cls=CustomJSONEncoder)


def delete_release(c: Config, release_id_or_virtual_dirname: str) -> None:
    release_id, release_dirname = resolve_release_ids(c, release_id_or_virtual_dirname)
    source_path = get_release_source_path_from_id(c, release_id)
    if source_path is None:
        logger.debug(f"Failed to lookup source path for release {release_id} ({release_dirname})")
        return None
    with lock(c, release_lock_name(release_id)):
        send2trash(source_path)
    logger.info(f"Trashed release {source_path}")
    update_cache_evict_nonexistent_releases(c)
    # Update all collages so that the release is removed from whichever collages it was in.
    update_cache_for_collages(c, None, force=True)


def toggle_release_new(c: Config, release_id_or_virtual_dirname: str) -> None:
    release_id, release_dirname = resolve_release_ids(c, release_id_or_virtual_dirname)
    source_path = get_release_source_path_from_id(c, release_id)
    if source_path is None:
        logger.debug(f"Failed to lookup source path for release {release_id} ({release_dirname})")
        return None

    for f in source_path.iterdir():
        if not STORED_DATA_FILE_REGEX.match(f.name):
            continue

        with lock(c, release_lock_name(release_id)):
            with f.open("rb") as fp:
                data = tomllib.load(fp)
            data["new"] = not data["new"]
            with f.open("wb") as fp:
                tomli_w.dump(data, fp)
        logger.info(f"Toggled NEW-ness of release {source_path} to {data['new']=}")
        update_cache_for_releases(c, [source_path], force=True)
        return

    logger.critical(f"Failed to find .rose.toml in {source_path}")


def set_release_cover_art(
    c: Config,
    release_id_or_virtual_dirname: str,
    new_cover_art_path: Path,
) -> None:
    """
    This function removes all potential cover arts in the release source directory and copies the
    file located at the passed in path to `cover.{ext}` in the release source directory.
    """
    suffix = new_cover_art_path.suffix.lower()
    if suffix[1:] not in c.valid_art_exts:
        raise InvalidCoverArtFileError(
            f"File {new_cover_art_path.name}'s extension is not supported for cover images: "
            "To change this, please read the configuration documentation"
        )

    release_id, release_dirname = resolve_release_ids(c, release_id_or_virtual_dirname)
    source_path = get_release_source_path_from_id(c, release_id)
    if source_path is None:
        logger.debug(f"Failed to lookup source path for release {release_id} ({release_dirname})")
        return None
    for f in source_path.iterdir():
        if f.name.lower() in c.valid_cover_arts:
            logger.debug(f"Deleting existing cover art {f.name} in {release_dirname}")
            send2trash(f)
    shutil.copyfile(new_cover_art_path, source_path / f"cover{new_cover_art_path.suffix}")
    logger.info(f"Set the cover of release {source_path} to {new_cover_art_path.name}")
    update_cache_for_releases(c, [source_path])


def remove_release_cover_art(c: Config, release_id_or_virtual_dirname: str) -> None:
    """This function deletes all potential cover arts in the release source directory."""
    release_id, release_dirname = resolve_release_ids(c, release_id_or_virtual_dirname)
    source_path = get_release_source_path_from_id(c, release_id)
    if source_path is None:
        logger.debug(f"Failed to lookup source path for release {release_id} ({release_dirname})")
        return None
    found = False
    for f in source_path.iterdir():
        if f.name.lower() in c.valid_cover_arts:
            logger.debug(f"Deleting existing cover art {f.name} in {release_dirname}")
            send2trash(f)
            found = True
    if found:
        logger.info(f"Deleted cover arts of release {source_path}")
    else:
        logger.info(f"No-Op: No cover arts found for release {source_path}")
    update_cache_for_releases(c, [source_path])


@dataclass
class MetadataArtist:
    name: str
    role: str

    @staticmethod
    def to_mapping(artists: list[MetadataArtist]) -> ArtistMapping:
        m = ArtistMapping()
        for a in artists:
            try:
                getattr(m, a.role.lower()).append(a.name)
            except AttributeError as e:
                raise UnknownArtistRoleError(
                    f"Failed to write tags: Unknown role for artist {a.name}: {a.role}"
                ) from e
        return m


@dataclass
class MetadataTrack:
    disc_number: str
    track_number: str
    title: str
    artists: list[MetadataArtist]


@dataclass
class MetadataRelease:
    title: str
    releasetype: str
    year: int | None
    genres: list[str]
    labels: list[str]
    artists: list[MetadataArtist]
    tracks: dict[str, MetadataTrack]

    @classmethod
    def from_cache(cls, release: CachedRelease, tracks: list[CachedTrack]) -> MetadataRelease:
        return MetadataRelease(
            title=release.title,
            releasetype=release.releasetype,
            year=release.year,
            genres=release.genres,
            labels=release.labels,
            artists=[
                MetadataArtist(name=a.name, role=a.role) for a in release.artists if not a.alias
            ],
            tracks={
                t.id: MetadataTrack(
                    disc_number=t.disc_number,
                    track_number=t.track_number,
                    title=t.title,
                    artists=[
                        MetadataArtist(name=a.name, role=a.role) for a in t.artists if not a.alias
                    ],
                )
                for t in tracks
            },
        )

    def serialize(self) -> str:
        # LOL TOML DOESN'T HAVE A NULL TYPE. Use -9999 as sentinel. If your music is legitimately
        # released in -9999, you should probably lay off the shrooms.
        data = asdict(self)
        data["year"] = self.year or -9999
        return tomli_w.dumps(data)

    @classmethod
    def from_toml(cls, toml: str) -> MetadataRelease:
        d = tomllib.loads(toml)
        return cls(
            title=d["title"],
            releasetype=d["releasetype"],
            year=d["year"] if d["year"] != -9999 else None,
            genres=d["genres"],
            labels=d["labels"],
            artists=[MetadataArtist(name=a["name"], role=a["role"]) for a in d["artists"]],
            tracks={
                tid: MetadataTrack(
                    track_number=t["track_number"],
                    disc_number=t["disc_number"],
                    title=t["title"],
                    artists=[MetadataArtist(name=a["name"], role=a["role"]) for a in t["artists"]],
                )
                for tid, t in d["tracks"].items()
            },
        )


def edit_release(c: Config, release_id_or_virtual_dirname: str) -> None:
    release_id, _ = resolve_release_ids(c, release_id_or_virtual_dirname)

    # Trigger a quick cache update to ensure we are reading the liveliest data.
    source_path = get_release_source_path_from_id(c, release_id)
    assert source_path is not None
    update_cache_for_releases(c, [source_path])

    with lock(c, release_lock_name(release_id)):
        cachedata = get_release(c, release_id_or_virtual_dirname)
        if not cachedata:
            raise ReleaseDoesNotExistError(
                f"Release {release_id_or_virtual_dirname} does not exist"
            )
        release, tracks = cachedata
        original_metadata = MetadataRelease.from_cache(release, tracks)
        toml = click.edit(original_metadata.serialize(), extension=".toml")
        if not toml:
            logger.info("Aborting manual release edit: metadata file not submitted.")
            return
        release_meta = original_metadata.from_toml(toml)
        if original_metadata == release_meta:
            logger.info("Aborting manual release edit: no metadata change detected.")
            return

        for t in tracks:
            track_meta = release_meta.tracks[t.id]
            tags = AudioTags.from_file(t.source_path)

            dirty = False

            # Track tags.
            if tags.track_number != track_meta.track_number:
                tags.track_number = track_meta.track_number
                dirty = True
                logger.debug(f"Modified tag detected for {t.source_path}: track_number")
            if tags.disc_number != track_meta.disc_number:
                tags.disc_number = track_meta.disc_number
                dirty = True
                logger.debug(f"Modified tag detected for {t.source_path}: disc_number")
            if tags.title != track_meta.title:
                tags.title = track_meta.title
                dirty = True
                logger.debug(f"Modified tag detected for {t.source_path}: title")
            tart = MetadataArtist.to_mapping(track_meta.artists)
            if tags.artists != tart:
                tags.artists = tart
                dirty = True
                logger.debug(f"Modified tag detected for {t.source_path}: artists")

            # Album tags.
            if tags.album != release_meta.title:
                tags.album = release_meta.title
                dirty = True
                logger.debug(f"Modified tag detected for {t.source_path}: album")
            if tags.release_type != release_meta.releasetype:
                tags.release_type = release_meta.releasetype.lower()
                dirty = True
                logger.debug(f"Modified tag detected for {t.source_path}: release_type")
            if tags.year != release_meta.year:
                tags.year = release_meta.year
                dirty = True
                logger.debug(f"Modified tag detected for {t.source_path}: year")
            if tags.genre != release_meta.genres:
                tags.genre = release_meta.genres
                dirty = True
                logger.debug(f"Modified tag detected for {t.source_path}: genre")
            if tags.label != release_meta.labels:
                tags.label = release_meta.labels
                dirty = True
                logger.debug(f"Modified tag detected for {t.source_path}: label")
            aart = MetadataArtist.to_mapping(release_meta.artists)
            if tags.album_artists != aart:
                tags.album_artists = aart
                dirty = True
                logger.debug(f"Modified tag detected for {t.source_path}: album_artists")

            if dirty:
                logger.info(f"Flushing changed tags to {t.source_path}")
                tags.flush()

    update_cache_for_releases(c, [release.source_path], force=True)


def resolve_release_ids(c: Config, release_id_or_virtual_dirname: str) -> tuple[str, str]:
    if valid_uuid(release_id_or_virtual_dirname):
        uuid = release_id_or_virtual_dirname
        virtual_dirname = get_release_virtual_dirname_from_id(c, uuid)
    else:
        virtual_dirname = release_id_or_virtual_dirname
        uuid = get_release_id_from_virtual_dirname(c, virtual_dirname)  # type: ignore
    if uuid is None or virtual_dirname is None:
        raise ReleaseDoesNotExistError(
            f"Release {uuid or ''}{virtual_dirname or ''} does not exist"
        )
    return uuid, virtual_dirname
