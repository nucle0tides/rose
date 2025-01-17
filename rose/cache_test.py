import hashlib
import shutil
import time
from dataclasses import asdict
from pathlib import Path

import pytest
import tomllib

from conftest import TEST_COLLAGE_1, TEST_PLAYLIST_1, TEST_RELEASE_1, TEST_RELEASE_2, TEST_RELEASE_3
from rose.audiotags import AudioTags
from rose.cache import (
    CACHE_SCHEMA_PATH,
    STORED_DATA_FILE_REGEX,
    CachedArtist,
    CachedPlaylist,
    CachedRelease,
    CachedTrack,
    _unpack,
    artist_exists,
    collage_exists,
    connect,
    cover_exists,
    genre_exists,
    get_playlist,
    get_release,
    get_release_id_from_virtual_dirname,
    get_release_source_path_from_id,
    get_release_virtual_dirname_from_id,
    get_track_filename,
    label_exists,
    list_artists,
    list_collage_releases,
    list_collages,
    list_genres,
    list_labels,
    list_playlists,
    list_releases,
    lock,
    migrate_database,
    playlist_exists,
    release_exists,
    track_exists,
    update_cache,
    update_cache_evict_nonexistent_releases,
    update_cache_for_releases,
)
from rose.common import VERSION
from rose.config import Config


def test_schema(config: Config) -> None:
    """Test that the schema successfully bootstraps."""
    with CACHE_SCHEMA_PATH.open("rb") as fp:
        schema_hash = hashlib.sha256(fp.read()).hexdigest()
    migrate_database(config)
    with connect(config) as conn:
        cursor = conn.execute("SELECT schema_hash, config_hash, version FROM _schema_hash")
        row = cursor.fetchone()
        assert row["schema_hash"] == schema_hash
        assert row["config_hash"] is not None
        assert row["version"] == VERSION


def test_migration(config: Config) -> None:
    """Test that "migrating" the database correctly migrates it."""
    config.cache_database_path.unlink()
    with connect(config) as conn:
        conn.execute(
            """
            CREATE TABLE _schema_hash (
                schema_hash TEXT
              , config_hash TEXT
              , version TEXT
              , PRIMARY KEY (schema_hash, config_hash, version)
            )
            """
        )
        conn.execute(
            """
            INSERT INTO _schema_hash (schema_hash, config_hash, version)
            VALUES ('haha', 'lala', 'blabla')
            """,
        )

    with CACHE_SCHEMA_PATH.open("rb") as fp:
        latest_schema_hash = hashlib.sha256(fp.read()).hexdigest()
    migrate_database(config)
    with connect(config) as conn:
        cursor = conn.execute("SELECT schema_hash, config_hash, version FROM _schema_hash")
        row = cursor.fetchone()
        assert row["schema_hash"] == latest_schema_hash
        assert row["config_hash"] is not None
        assert row["version"] == VERSION
        cursor = conn.execute("SELECT COUNT(*) FROM _schema_hash")
        assert cursor.fetchone()[0] == 1


def test_locks(config: Config) -> None:
    """Test that taking locks works. The times are a bit loose b/c GH Actions is slow."""
    lock_name = "lol"

    # Test that the locking and timeout work.
    start = time.time()
    with lock(config, lock_name, timeout=0.2):
        lock1_acq = time.time()
        with lock(config, lock_name, timeout=0.2):
            lock2_acq = time.time()
    # Assert that we had to wait ~0.1sec to get the second lock.
    assert lock1_acq - start < 0.08
    assert lock2_acq - lock1_acq > 0.17

    # Test that releasing a lock actually works.
    start = time.time()
    with lock(config, lock_name, timeout=0.2):
        lock1_acq = time.time()
    with lock(config, lock_name, timeout=0.2):
        lock2_acq = time.time()
    # Assert that we had to wait negligible time to get the second lock.
    assert lock1_acq - start < 0.08
    assert lock2_acq - lock1_acq < 0.08


def test_update_cache_all(config: Config) -> None:
    """Test that the update all function works."""
    shutil.copytree(TEST_RELEASE_1, config.music_source_dir / TEST_RELEASE_1.name)
    shutil.copytree(TEST_RELEASE_2, config.music_source_dir / TEST_RELEASE_2.name)

    # Test that we prune deleted releases too.
    with connect(config) as conn:
        conn.execute(
            """
            INSERT INTO releases (id, source_path, virtual_dirname, added_at, datafile_mtime, title, release_type, multidisc, formatted_artists)
            VALUES ('aaaaaa', '/nonexistent', '0000-01-01T00:00:00+00:00', '999', 'nonexistent', 'aa', 'unknown', false, 'aa;aa')
            """  # noqa: E501
        )

    update_cache(config)

    with connect(config) as conn:
        cursor = conn.execute("SELECT COUNT(*) FROM releases")
        assert cursor.fetchone()[0] == 2
        cursor = conn.execute("SELECT COUNT(*) FROM tracks")
        assert cursor.fetchone()[0] == 4


def test_update_cache_multiprocessing(config: Config) -> None:
    """Test that the update all function works."""
    shutil.copytree(TEST_RELEASE_1, config.music_source_dir / TEST_RELEASE_1.name)
    shutil.copytree(TEST_RELEASE_2, config.music_source_dir / TEST_RELEASE_2.name)
    update_cache_for_releases(config, force_multiprocessing=True)
    with connect(config) as conn:
        cursor = conn.execute("SELECT COUNT(*) FROM releases")
        assert cursor.fetchone()[0] == 2
        cursor = conn.execute("SELECT COUNT(*) FROM tracks")
        assert cursor.fetchone()[0] == 4


def test_update_cache_releases(config: Config) -> None:
    release_dir = config.music_source_dir / TEST_RELEASE_1.name
    shutil.copytree(TEST_RELEASE_1, release_dir)
    update_cache_for_releases(config, [release_dir])

    # Check that the release directory was given a UUID.
    release_id: str | None = None
    for f in release_dir.iterdir():
        if m := STORED_DATA_FILE_REGEX.match(f.name):
            release_id = m[1]
    assert release_id is not None

    # Assert that the release metadata was read correctly.
    with connect(config) as conn:
        cursor = conn.execute(
            """
            SELECT id, source_path, title, release_type, release_year, new
            FROM releases WHERE id = ?
            """,
            (release_id,),
        )
        row = cursor.fetchone()
        assert row["source_path"] == str(release_dir)
        assert row["title"] == "I Love Blackpink"
        assert row["release_type"] == "album"
        assert row["release_year"] == 1990
        assert row["new"]

        cursor = conn.execute(
            "SELECT genre FROM releases_genres WHERE release_id = ?",
            (release_id,),
        )
        genres = {r["genre"] for r in cursor.fetchall()}
        assert genres == {"K-Pop", "Pop"}

        cursor = conn.execute(
            "SELECT label FROM releases_labels WHERE release_id = ?",
            (release_id,),
        )
        labels = {r["label"] for r in cursor.fetchall()}
        assert labels == {"A Cool Label"}

        cursor = conn.execute(
            "SELECT artist, role FROM releases_artists WHERE release_id = ?",
            (release_id,),
        )
        artists = {(r["artist"], r["role"]) for r in cursor.fetchall()}
        assert artists == {
            ("BLACKPINK", "main"),
        }

        for f in release_dir.iterdir():
            if f.suffix != ".m4a":
                continue

            # Assert that the track metadata was read correctly.
            cursor = conn.execute(
                """
                SELECT
                    id, source_path, title, release_id, track_number, disc_number, duration_seconds
                FROM tracks WHERE source_path = ?
                """,
                (str(f),),
            )
            row = cursor.fetchone()
            track_id = row["id"]
            assert row["title"].startswith("Track")
            assert row["release_id"] == release_id
            assert row["track_number"] != ""
            assert row["disc_number"] == "1"
            assert row["duration_seconds"] == 2

            cursor = conn.execute(
                "SELECT artist, role FROM tracks_artists WHERE track_id = ?",
                (track_id,),
            )
            artists = {(r["artist"], r["role"]) for r in cursor.fetchall()}
            assert artists == {
                ("BLACKPINK", "main"),
            }


def test_update_cache_releases_duplicate_collision(config: Config) -> None:
    """Test that equivalent releases are appropriately handled."""
    shutil.copytree(TEST_RELEASE_1, config.music_source_dir / "d1")
    shutil.copytree(TEST_RELEASE_1, config.music_source_dir / "d2")
    shutil.copytree(TEST_RELEASE_1, config.music_source_dir / "d3")
    update_cache_for_releases(config)

    with connect(config) as conn:
        cursor = conn.execute("SELECT id, virtual_dirname FROM releases")
        rows = cursor.fetchall()
        assert len({r["id"] for r in rows}) == 3
        assert {r["virtual_dirname"] for r in rows} == {
            "{NEW} BLACKPINK - 1990. I Love Blackpink [K-Pop;Pop]",
            "{NEW} BLACKPINK - 1990. I Love Blackpink [K-Pop;Pop] [2]",
            "{NEW} BLACKPINK - 1990. I Love Blackpink [K-Pop;Pop] [3]",
        }


def test_update_cache_releases_uncached_with_existing_id(config: Config) -> None:
    """Test that IDs in filenames are read and preserved."""
    release_dir = config.music_source_dir / TEST_RELEASE_2.name
    shutil.copytree(TEST_RELEASE_2, release_dir)
    update_cache_for_releases(config, [release_dir])

    # Check that the release directory was given a UUID.
    release_id: str | None = None
    for f in release_dir.iterdir():
        if m := STORED_DATA_FILE_REGEX.match(f.name):
            release_id = m[1]
    assert release_id == "ilovecarly"  # Hardcoded ID for testing.


def test_update_cache_releases_preserves_track_ids_across_rebuilds(config: Config) -> None:
    """Test that track IDs are preserved across cache rebuilds."""
    release_dir = config.music_source_dir / TEST_RELEASE_3.name
    shutil.copytree(TEST_RELEASE_3, release_dir)
    update_cache_for_releases(config, [release_dir])
    with connect(config) as conn:
        cursor = conn.execute("SELECT id FROM tracks")
        first_track_ids = {r["id"] for r in cursor}

    # Nuke the database.
    config.cache_database_path.unlink()
    migrate_database(config)

    # Repeat cache population.
    update_cache_for_releases(config, [release_dir])
    with connect(config) as conn:
        cursor = conn.execute("SELECT id FROM tracks")
        second_track_ids = {r["id"] for r in cursor}

    # Assert IDs are equivalent.
    assert first_track_ids == second_track_ids


def test_update_cache_releases_writes_ids_to_tags(config: Config) -> None:
    """Test that track IDs and release IDs are written to files."""
    release_dir = config.music_source_dir / TEST_RELEASE_3.name
    shutil.copytree(TEST_RELEASE_3, release_dir)

    af = AudioTags.from_file(release_dir / "01.m4a")
    assert af.id is None
    assert af.release_id is None
    af = AudioTags.from_file(release_dir / "02.m4a")
    assert af.id is None
    assert af.release_id is None

    update_cache_for_releases(config, [release_dir])

    af = AudioTags.from_file(release_dir / "01.m4a")
    assert af.id is not None
    assert af.release_id is not None
    af = AudioTags.from_file(release_dir / "02.m4a")
    assert af.id is not None
    assert af.release_id is not None


def test_update_cache_releases_already_fully_cached(config: Config) -> None:
    """Test that a fully cached release No Ops when updated again."""
    release_dir = config.music_source_dir / TEST_RELEASE_1.name
    shutil.copytree(TEST_RELEASE_1, release_dir)
    update_cache_for_releases(config, [release_dir])
    update_cache_for_releases(config, [release_dir])

    # Assert that the release metadata was read correctly.
    with connect(config) as conn:
        cursor = conn.execute(
            "SELECT id, source_path, title, release_type, release_year, new FROM releases",
        )
        row = cursor.fetchone()
        assert row["source_path"] == str(release_dir)
        assert row["title"] == "I Love Blackpink"
        assert row["release_type"] == "album"
        assert row["release_year"] == 1990
        assert row["new"]


def test_update_cache_releases_disk_update_to_previously_cached(config: Config) -> None:
    """Test that a cached release is updated after a track updates."""
    release_dir = config.music_source_dir / TEST_RELEASE_1.name
    shutil.copytree(TEST_RELEASE_1, release_dir)
    update_cache_for_releases(config, [release_dir])
    # I'm too lazy to mutagen update the files, so instead we're going to update the database. And
    # then touch a file to signify that "we modified it."
    with connect(config) as conn:
        conn.execute("UPDATE releases SET title = 'An Uncool Album'")
        (release_dir / "01.m4a").touch()
    update_cache_for_releases(config, [release_dir])

    # Assert that the release metadata was re-read and updated correctly.
    with connect(config) as conn:
        cursor = conn.execute(
            "SELECT id, source_path, title, release_type, release_year, new FROM releases",
        )
        row = cursor.fetchone()
        assert row["source_path"] == str(release_dir)
        assert row["title"] == "I Love Blackpink"
        assert row["release_type"] == "album"
        assert row["release_year"] == 1990
        assert row["new"]


def test_update_cache_releases_disk_update_to_datafile(config: Config) -> None:
    """Test that a cached release is updated after a datafile updates."""
    release_dir = config.music_source_dir / TEST_RELEASE_1.name
    shutil.copytree(TEST_RELEASE_1, release_dir)
    update_cache_for_releases(config, [release_dir])
    with connect(config) as conn:
        conn.execute("UPDATE releases SET datafile_mtime = '0' AND new = false")
    update_cache_for_releases(config, [release_dir])

    # Assert that the release metadata was re-read and updated correctly.
    with connect(config) as conn:
        cursor = conn.execute("SELECT new, added_at FROM releases")
        row = cursor.fetchone()
        assert row["new"]
        assert row["added_at"]


def test_update_cache_releases_disk_upgrade_old_datafile(config: Config) -> None:
    """Test that a legacy invalid datafile is upgraded on index."""
    release_dir = config.music_source_dir / TEST_RELEASE_1.name
    shutil.copytree(TEST_RELEASE_1, release_dir)
    datafile = release_dir / ".rose.lalala.toml"
    datafile.touch()
    update_cache_for_releases(config, [release_dir])

    # Assert that the release metadata was re-read and updated correctly.
    with connect(config) as conn:
        cursor = conn.execute("SELECT id, new, added_at FROM releases")
        row = cursor.fetchone()
        assert row["id"] == "lalala"
        assert row["new"]
        assert row["added_at"]
    with datafile.open("r") as fp:
        data = fp.read()
        assert "new = true" in data
        assert "added_at = " in data


def test_update_cache_releases_source_path_renamed(config: Config) -> None:
    """Test that a cached release is updated after a directory rename."""
    release_dir = config.music_source_dir / TEST_RELEASE_1.name
    shutil.copytree(TEST_RELEASE_1, release_dir)
    update_cache_for_releases(config, [release_dir])
    moved_release_dir = config.music_source_dir / "moved lol"
    release_dir.rename(moved_release_dir)
    update_cache_for_releases(config, [moved_release_dir])

    # Assert that the release metadata was re-read and updated correctly.
    with connect(config) as conn:
        cursor = conn.execute(
            "SELECT id, source_path, title, release_type, release_year, new FROM releases",
        )
        row = cursor.fetchone()
        assert row["source_path"] == str(moved_release_dir)
        assert row["title"] == "I Love Blackpink"
        assert row["release_type"] == "album"
        assert row["release_year"] == 1990
        assert row["new"]


def test_update_cache_releases_delete_nonexistent(config: Config) -> None:
    """Test that deleted releases that are no longer on disk are cleared from cache."""
    with connect(config) as conn:
        conn.execute(
            """
            INSERT INTO releases (id, source_path, virtual_dirname, added_at, datafile_mtime, title, release_type, multidisc, formatted_artists)
            VALUES ('aaaaaa', '/nonexistent', '0000-01-01T00:00:00+00:00', '999', 'nonexistent', 'aa', 'unknown', false, 'aa;aa')
            """  # noqa: E501
        )
    update_cache_evict_nonexistent_releases(config)
    with connect(config) as conn:
        cursor = conn.execute("SELECT COUNT(*) FROM releases")
        assert cursor.fetchone()[0] == 0


def test_update_cache_releases_skips_empty_directory(config: Config) -> None:
    """Test that an directory with no audio files is skipped."""
    rd = config.music_source_dir / "lalala"
    rd.mkdir()
    (rd / "ignoreme.file").touch()
    update_cache_for_releases(config, [rd])
    with connect(config) as conn:
        cursor = conn.execute("SELECT COUNT(*) FROM releases")
        assert cursor.fetchone()[0] == 0


def test_update_cache_releases_uncaches_empty_directory(config: Config) -> None:
    """Test that a previously-cached directory with no audio files now is cleared from cache."""
    release_dir = config.music_source_dir / TEST_RELEASE_1.name
    shutil.copytree(TEST_RELEASE_1, release_dir)
    update_cache_for_releases(config, [release_dir])
    shutil.rmtree(release_dir)
    release_dir.mkdir()
    update_cache_for_releases(config, [release_dir])
    with connect(config) as conn:
        cursor = conn.execute("SELECT COUNT(*) FROM releases")
        assert cursor.fetchone()[0] == 0


def test_update_cache_releases_evicts_relations(config: Config) -> None:
    """
    Test that related entities (artist, genre, label) that have been removed from the tags are
    properly evicted from the cache on update.
    """
    release_dir = config.music_source_dir / TEST_RELEASE_2.name
    shutil.copytree(TEST_RELEASE_2, release_dir)
    # Initial cache population.
    update_cache_for_releases(config, [release_dir])
    # Pretend that we have more artists in the cache.
    with connect(config) as conn:
        conn.execute(
            """
            INSERT INTO releases_genres (release_id, genre, genre_sanitized)
            VALUES ('ilovecarly', 'lalala', 'lalala')
            """,
        )
        conn.execute(
            """
            INSERT INTO releases_labels (release_id, label, label_sanitized)
            VALUES ('ilovecarly', 'lalala', 'lalala')
            """,
        )
        conn.execute(
            """
            INSERT INTO releases_artists (release_id, artist, artist_sanitized, role, alias)
            VALUES ('ilovecarly', 'lalala', 'lalala', 'main', false)
            """,
        )
        conn.execute(
            """
            INSERT INTO tracks_artists (track_id, artist, artist_sanitized, role, alias)
            SELECT id, 'lalala', 'lalala', 'main', false FROM tracks
            """,
        )
    # Second cache refresh.
    update_cache_for_releases(config, [release_dir], force=True)
    # Assert that all of the above were evicted.
    with connect(config) as conn:
        cursor = conn.execute(
            "SELECT EXISTS (SELECT * FROM releases_genres WHERE genre = 'lalala')"
        )
        assert not cursor.fetchone()[0]
        cursor = conn.execute(
            "SELECT EXISTS (SELECT * FROM releases_labels WHERE label = 'lalala')"
        )
        assert not cursor.fetchone()[0]
        cursor = conn.execute(
            "SELECT EXISTS (SELECT * FROM releases_artists WHERE artist = 'lalala')"
        )
        assert not cursor.fetchone()[0]
        cursor = conn.execute(
            "SELECT EXISTS (SELECT * FROM tracks_artists WHERE artist = 'lalala')"
        )
        assert not cursor.fetchone()[0]


def test_update_cache_releases_adds_aliased_artist(config: Config) -> None:
    """Test that an artist alias is properly recorded in the read cache."""
    config = Config(
        **{
            **asdict(config),
            "artist_aliases_parents_map": {"BLACKPINK": ["HAHA"]},
            "artist_aliases_map": {"HAHA": ["BLACKPINK"]},
        }
    )
    release_dir = config.music_source_dir / TEST_RELEASE_1.name
    shutil.copytree(TEST_RELEASE_1, release_dir)
    update_cache_for_releases(config, [release_dir])

    with connect(config) as conn:
        cursor = conn.execute(
            "SELECT artist, role, alias FROM releases_artists",
        )
        artists = {(r["artist"], r["role"], bool(r["alias"])) for r in cursor.fetchall()}
        assert artists == {
            ("BLACKPINK", "main", False),
            ("HAHA", "main", True),
        }

        for f in release_dir.iterdir():
            if f.suffix != ".m4a":
                continue

            cursor = conn.execute(
                """
                SELECT ta.artist, ta.role, ta.alias
                FROM tracks_artists ta
                JOIN tracks t ON t.id = ta.track_id
                WHERE t.source_path = ?
                """,
                (str(f),),
            )
            artists = {(r["artist"], r["role"], bool(r["alias"])) for r in cursor.fetchall()}
            assert artists == {
                ("BLACKPINK", "main", False),
                ("HAHA", "main", True),
            }


def test_update_cache_releases_ignores_directories(config: Config) -> None:
    """Test that the ignore_release_directories configuration value works."""
    config = Config(**{**asdict(config), "ignore_release_directories": ["lalala"]})
    release_dir = config.music_source_dir / "lalala"
    shutil.copytree(TEST_RELEASE_1, release_dir)

    # Test that both arg+no-arg ignore the directory.
    update_cache_for_releases(config)
    with connect(config) as conn:
        cursor = conn.execute("SELECT COUNT(*) FROM releases")
        assert cursor.fetchone()[0] == 0

    update_cache_for_releases(config)
    with connect(config) as conn:
        cursor = conn.execute("SELECT COUNT(*) FROM releases")
        assert cursor.fetchone()[0] == 0


def test_update_cache_releases_ignores_partially_written_directory(config: Config) -> None:
    """Test that a partially-written cached release is ignored."""
    # 1. Write the directory and index it. This should give it IDs and shit.
    release_dir = config.music_source_dir / TEST_RELEASE_1.name
    shutil.copytree(TEST_RELEASE_1, release_dir)
    update_cache(config)

    # 2. Move the directory and "remove" the ID file.
    renamed_release_dir = config.music_source_dir / "lalala"
    release_dir.rename(renamed_release_dir)
    datafile = next(f for f in renamed_release_dir.iterdir() if f.stem.startswith(".rose"))
    tmpfile = datafile.with_name("tmp")
    datafile.rename(tmpfile)

    # 3. Re-update cache. We should see an empty cache now.
    update_cache(config)
    with connect(config) as conn:
        cursor = conn.execute("SELECT COUNT(*) FROM releases")
        assert cursor.fetchone()[0] == 0

    # 4. Put the datafile back. We should now see the release cache again properly.
    datafile.with_name("tmp").rename(datafile)
    update_cache(config)
    with connect(config) as conn:
        cursor = conn.execute("SELECT COUNT(*) FROM releases")
        assert cursor.fetchone()[0] == 1

    # 5. Rename and remove the ID file again. We should see an empty cache again.
    release_dir = renamed_release_dir
    renamed_release_dir = config.music_source_dir / "bahaha"
    release_dir.rename(renamed_release_dir)
    next(f for f in renamed_release_dir.iterdir() if f.stem.startswith(".rose")).unlink()
    update_cache(config)
    with connect(config) as conn:
        cursor = conn.execute("SELECT COUNT(*) FROM releases")
        assert cursor.fetchone()[0] == 0

    # 6. Run with force=True. This should index the directory and make a new .rose.toml file.
    update_cache(config, force=True)
    assert (renamed_release_dir / datafile.name).is_file()
    with connect(config) as conn:
        cursor = conn.execute("SELECT COUNT(*) FROM releases")
        assert cursor.fetchone()[0] == 1


def test_update_cache_releases_updates_full_text_search(config: Config) -> None:
    release_dir = config.music_source_dir / TEST_RELEASE_1.name
    shutil.copytree(TEST_RELEASE_1, release_dir)

    update_cache_for_releases(config, [release_dir])
    with connect(config) as conn:
        cursor = conn.execute(
            """
            SELECT rowid, * FROM rules_engine_fts
            """
        )
        print([dict(x) for x in cursor])
        cursor = conn.execute(
            """
            SELECT rowid, * FROM tracks
            """
        )
        print([dict(x) for x in cursor])
    with connect(config) as conn:
        cursor = conn.execute(
            """
            SELECT t.source_path
            FROM rules_engine_fts s
            JOIN tracks t ON t.rowid = s.rowid
            WHERE s.tracktitle MATCH 'r a c k'
            """
        )
        fnames = {Path(r["source_path"]) for r in cursor}
        assert fnames == {
            release_dir / "01.m4a",
            release_dir / "02.m4a",
        }

    # And then test the DELETE+INSERT behavior. And that the query still works.
    update_cache_for_releases(config, [release_dir], force=True)
    with connect(config) as conn:
        cursor = conn.execute(
            """
            SELECT t.source_path
            FROM rules_engine_fts s
            JOIN tracks t ON t.rowid = s.rowid
            WHERE s.tracktitle MATCH 'r a c k'
            """
        )
        fnames = {Path(r["source_path"]) for r in cursor}
        assert fnames == {
            release_dir / "01.m4a",
            release_dir / "02.m4a",
        }


def test_update_cache_collages(config: Config) -> None:
    shutil.copytree(TEST_RELEASE_2, config.music_source_dir / TEST_RELEASE_2.name)
    shutil.copytree(TEST_COLLAGE_1, config.music_source_dir / "!collages")
    update_cache(config)

    # Assert that the collage metadata was read correctly.
    with connect(config) as conn:
        cursor = conn.execute("SELECT name, source_mtime FROM collages")
        rows = cursor.fetchall()
        assert len(rows) == 1
        row = rows[0]
        assert row["name"] == "Rose Gold"
        assert row["source_mtime"]

        cursor = conn.execute(
            "SELECT collage_name, release_id, position FROM collages_releases WHERE NOT missing"
        )
        rows = cursor.fetchall()
        assert len(rows) == 1
        row = rows[0]
        assert row["collage_name"] == "Rose Gold"
        assert row["release_id"] == "ilovecarly"
        assert row["position"] == 1


def test_update_cache_collages_missing_release_id(config: Config) -> None:
    shutil.copytree(TEST_COLLAGE_1, config.music_source_dir / "!collages")
    update_cache(config)

    # Assert that the releases in the collage were read as missing.
    with connect(config) as conn:
        cursor = conn.execute("SELECT COUNT(*) FROM collages_releases WHERE missing")
        assert cursor.fetchone()[0] == 2
    # Assert that source file was updated to set the releases missing.
    with (config.music_source_dir / "!collages" / "Rose Gold.toml").open("rb") as fp:
        data = tomllib.load(fp)
    assert len(data["releases"]) == 2
    assert len([r for r in data["releases"] if r["missing"]]) == 2

    shutil.copytree(TEST_RELEASE_2, config.music_source_dir / TEST_RELEASE_2.name)
    shutil.copytree(TEST_RELEASE_3, config.music_source_dir / TEST_RELEASE_3.name)
    update_cache(config)

    # Assert that the releases in the collage were unflagged as missing.
    with connect(config) as conn:
        cursor = conn.execute("SELECT COUNT(*) FROM collages_releases WHERE NOT missing")
        assert cursor.fetchone()[0] == 2
    # Assert that source file was updated to remove the missing flag.
    with (config.music_source_dir / "!collages" / "Rose Gold.toml").open("rb") as fp:
        data = tomllib.load(fp)
    assert len([r for r in data["releases"] if "missing" not in r]) == 2


def test_update_cache_collages_on_release_rename(config: Config) -> None:
    """
    Test that a renamed release source directory does not remove the release from any collages. This
    can occur because the rename operation is executed in SQL as release deletion followed by
    release creation.
    """
    shutil.copytree(TEST_COLLAGE_1, config.music_source_dir / "!collages")
    shutil.copytree(TEST_RELEASE_2, config.music_source_dir / TEST_RELEASE_2.name)
    shutil.copytree(TEST_RELEASE_3, config.music_source_dir / TEST_RELEASE_3.name)
    update_cache(config)

    (config.music_source_dir / TEST_RELEASE_2.name).rename(config.music_source_dir / "lalala")
    update_cache(config)

    with connect(config) as conn:
        cursor = conn.execute("SELECT collage_name, release_id, position FROM collages_releases")
        rows = [dict(r) for r in cursor]
        assert rows == [
            {"collage_name": "Rose Gold", "release_id": "ilovecarly", "position": 1},
            {"collage_name": "Rose Gold", "release_id": "ilovenewjeans", "position": 2},
        ]

    # Assert that source file was not updated to remove the release.
    with (config.music_source_dir / "!collages" / "Rose Gold.toml").open("rb") as fp:
        data = tomllib.load(fp)
    assert not [r for r in data["releases"] if "missing" in r]
    assert len(data["releases"]) == 2


def test_update_cache_playlists(config: Config) -> None:
    shutil.copytree(TEST_RELEASE_2, config.music_source_dir / TEST_RELEASE_2.name)
    shutil.copytree(TEST_PLAYLIST_1, config.music_source_dir / "!playlists")
    update_cache(config)

    # Assert that the playlist metadata was read correctly.
    with connect(config) as conn:
        cursor = conn.execute("SELECT name, source_mtime, cover_path FROM playlists")
        rows = cursor.fetchall()
        assert len(rows) == 1
        row = rows[0]
        assert row["name"] == "Lala Lisa"
        assert row["source_mtime"] is not None
        assert row["cover_path"] == str(config.music_source_dir / "!playlists" / "Lala Lisa.jpg")

        cursor = conn.execute(
            "SELECT playlist_name, track_id, position FROM playlists_tracks ORDER BY position"
        )
        assert [dict(r) for r in cursor] == [
            {"playlist_name": "Lala Lisa", "track_id": "iloveloona", "position": 1},
            {"playlist_name": "Lala Lisa", "track_id": "ilovetwice", "position": 2},
        ]


def test_update_cache_playlists_missing_track_id(config: Config) -> None:
    shutil.copytree(TEST_PLAYLIST_1, config.music_source_dir / "!playlists")
    update_cache(config)

    # Assert that the tracks in the playlist were read as missing.
    with connect(config) as conn:
        cursor = conn.execute("SELECT COUNT(*) FROM playlists_tracks WHERE missing")
        assert cursor.fetchone()[0] == 2
    # Assert that source file was updated to set the tracks missing.
    with (config.music_source_dir / "!playlists" / "Lala Lisa.toml").open("rb") as fp:
        data = tomllib.load(fp)
    assert len(data["tracks"]) == 2
    assert len([r for r in data["tracks"] if r["missing"]]) == 2

    shutil.copytree(TEST_RELEASE_2, config.music_source_dir / TEST_RELEASE_2.name)
    update_cache(config)

    # Assert that the tracks in the playlist were unflagged as missing.
    with connect(config) as conn:
        cursor = conn.execute("SELECT COUNT(*) FROM playlists_tracks WHERE NOT missing")
        assert cursor.fetchone()[0] == 2
    # Assert that source file was updated to remove the missing flag.
    with (config.music_source_dir / "!playlists" / "Lala Lisa.toml").open("rb") as fp:
        data = tomllib.load(fp)
    assert len([r for r in data["tracks"] if "missing" not in r]) == 2


def test_update_releases_updates_collages_description_meta(config: Config) -> None:
    shutil.copytree(TEST_RELEASE_1, config.music_source_dir / TEST_RELEASE_1.name)
    shutil.copytree(TEST_RELEASE_2, config.music_source_dir / TEST_RELEASE_2.name)
    shutil.copytree(TEST_RELEASE_3, config.music_source_dir / TEST_RELEASE_3.name)
    shutil.copytree(TEST_COLLAGE_1, config.music_source_dir / "!collages")
    cpath = config.music_source_dir / "!collages" / "Rose Gold.toml"

    # First cache update: releases are inserted, collage is new. This should update the collage
    # TOML.
    update_cache(config)
    with cpath.open("r") as fp:
        assert (
            fp.read()
            == """\
[[releases]]
uuid = "ilovecarly"
description_meta = "Carly Rae Jepsen - 1990. I Love Carly [Dream Pop;Pop]"

[[releases]]
uuid = "ilovenewjeans"
description_meta = "NewJeans - 1990. I Love NewJeans [K-Pop;R&B]"
"""
        )

    # Now prep for the second update. Reset the TOML to have garbage again, and update the database
    # such that the virtual dirnames are also incorrect.
    with cpath.open("w") as fp:
        fp.write(
            """\
[[releases]]
uuid = "ilovecarly"
description_meta = "lalala"
[[releases]]
uuid = "ilovenewjeans"
description_meta = "hahaha"
"""
        )
    with connect(config) as conn:
        conn.execute("UPDATE releases SET virtual_dirname = id || 'lalala'")

    # Second cache update: releases exist, collages exist, release is "updated." This should also
    # trigger a metadata update.
    update_cache(config, force=True)
    with cpath.open("r") as fp:
        assert (
            fp.read()
            == """\
[[releases]]
uuid = "ilovecarly"
description_meta = "Carly Rae Jepsen - 1990. I Love Carly [Dream Pop;Pop]"

[[releases]]
uuid = "ilovenewjeans"
description_meta = "NewJeans - 1990. I Love NewJeans [K-Pop;R&B]"
"""
        )


def test_update_tracks_updates_playlists_description_meta(config: Config) -> None:
    shutil.copytree(TEST_RELEASE_2, config.music_source_dir / TEST_RELEASE_2.name)
    shutil.copytree(TEST_PLAYLIST_1, config.music_source_dir / "!playlists")
    ppath = config.music_source_dir / "!playlists" / "Lala Lisa.toml"

    # First cache update: tracks are inserted, playlist is new. This should update the playlist
    # TOML.
    update_cache(config)
    with ppath.open("r") as fp:
        assert (
            fp.read()
            == """\
tracks = [
    { uuid = "iloveloona", description_meta = "Carly Rae Jepsen - Track 1.m4a" },
    { uuid = "ilovetwice", description_meta = "Carly Rae Jepsen - Track 2.m4a" },
]
"""
        )

    # Now prep for the second update. Reset the TOML to have garbage again, and update the database
    # such that the virtual filenames are also incorrect.
    with ppath.open("w") as fp:
        fp.write(
            """\
[[tracks]]
uuid = "iloveloona"
description_meta = "lalala"
[[tracks]]
uuid = "ilovetwice"
description_meta = "hahaha"
"""
        )
    with connect(config) as conn:
        conn.execute("UPDATE tracks SET virtual_filename = id || 'lalala'")

    # Second cache update: tracks exist, playlists exist, track is "updated." This should also
    # trigger a metadata update.
    update_cache(config, force=True)
    with ppath.open("r") as fp:
        assert (
            fp.read()
            == """\
tracks = [
    { uuid = "iloveloona", description_meta = "Carly Rae Jepsen - Track 1.m4a" },
    { uuid = "ilovetwice", description_meta = "Carly Rae Jepsen - Track 2.m4a" },
]
"""
        )


def test_update_cache_playlists_on_release_rename(config: Config) -> None:
    """
    Test that a renamed release source directory does not remove any of its tracks any playlists.
    This can occur because when a release is renamed, we remove all tracks from the database and
    then reinsert them.
    """
    shutil.copytree(TEST_PLAYLIST_1, config.music_source_dir / "!playlists")
    shutil.copytree(TEST_RELEASE_2, config.music_source_dir / TEST_RELEASE_2.name)
    update_cache(config)

    (config.music_source_dir / TEST_RELEASE_2.name).rename(config.music_source_dir / "lalala")
    update_cache(config)

    with connect(config) as conn:
        cursor = conn.execute("SELECT playlist_name, track_id, position FROM playlists_tracks")
        rows = [dict(r) for r in cursor]
        assert rows == [
            {"playlist_name": "Lala Lisa", "track_id": "iloveloona", "position": 1},
            {"playlist_name": "Lala Lisa", "track_id": "ilovetwice", "position": 2},
        ]

    # Assert that source file was not updated to remove the track.
    with (config.music_source_dir / "!playlists" / "Lala Lisa.toml").open("rb") as fp:
        data = tomllib.load(fp)
    assert not [t for t in data["tracks"] if "missing" in t]
    assert len(data["tracks"]) == 2


@pytest.mark.usefixtures("seeded_cache")
def test_list_releases(config: Config) -> None:
    releases = list(list_releases(config))
    assert releases == [
        CachedRelease(
            datafile_mtime="999",
            id="r1",
            source_path=Path(config.music_source_dir / "r1"),
            cover_image_path=None,
            added_at="0000-01-01T00:00:00+00:00",
            virtual_dirname="r1",
            title="Release 1",
            releasetype="album",
            year=2023,
            multidisc=False,
            new=False,
            genres=["Deep House", "Techno"],
            labels=["Silk Music"],
            artists=[
                CachedArtist(name="Bass Man", role="main"),
                CachedArtist(name="Techno Man", role="main"),
            ],
            formatted_artists="Techno Man;Bass Man",
        ),
        CachedRelease(
            datafile_mtime="999",
            id="r2",
            source_path=Path(config.music_source_dir / "r2"),
            cover_image_path=Path(config.music_source_dir / "r2" / "cover.jpg"),
            added_at="0000-01-01T00:00:00+00:00",
            virtual_dirname="r2",
            title="Release 2",
            releasetype="album",
            year=2021,
            multidisc=False,
            new=False,
            genres=["Classical"],
            labels=["Native State"],
            artists=[
                CachedArtist(name="Conductor Woman", role="guest"),
                CachedArtist(name="Violin Woman", role="main"),
            ],
            formatted_artists="Violin Woman feat. Conductor Woman",
        ),
        CachedRelease(
            datafile_mtime="999",
            id="r3",
            source_path=Path(config.music_source_dir / "r3"),
            cover_image_path=None,
            added_at="0000-01-01T00:00:00+00:00",
            virtual_dirname="{NEW} r3",
            title="Release 3",
            releasetype="album",
            year=2021,
            multidisc=False,
            new=True,
            genres=[],
            labels=[],
            artists=[],
            formatted_artists="",
        ),
    ]

    releases = list(list_releases(config, sanitized_artist_filter="Techno Man"))
    assert releases == [
        CachedRelease(
            datafile_mtime="999",
            id="r1",
            source_path=Path(config.music_source_dir / "r1"),
            cover_image_path=None,
            added_at="0000-01-01T00:00:00+00:00",
            virtual_dirname="r1",
            title="Release 1",
            releasetype="album",
            year=2023,
            multidisc=False,
            new=False,
            genres=["Deep House", "Techno"],
            labels=["Silk Music"],
            artists=[
                CachedArtist(name="Bass Man", role="main"),
                CachedArtist(name="Techno Man", role="main"),
            ],
            formatted_artists="Techno Man;Bass Man",
        ),
    ]

    releases = list(list_releases(config, sanitized_genre_filter="Techno"))
    assert releases == [
        CachedRelease(
            datafile_mtime="999",
            id="r1",
            source_path=Path(config.music_source_dir / "r1"),
            cover_image_path=None,
            added_at="0000-01-01T00:00:00+00:00",
            virtual_dirname="r1",
            title="Release 1",
            releasetype="album",
            year=2023,
            multidisc=False,
            new=False,
            genres=["Deep House", "Techno"],
            labels=["Silk Music"],
            artists=[
                CachedArtist(name="Bass Man", role="main"),
                CachedArtist(name="Techno Man", role="main"),
            ],
            formatted_artists="Techno Man;Bass Man",
        ),
    ]

    releases = list(list_releases(config, sanitized_label_filter="Silk Music"))
    assert releases == [
        CachedRelease(
            datafile_mtime="999",
            id="r1",
            source_path=Path(config.music_source_dir / "r1"),
            cover_image_path=None,
            added_at="0000-01-01T00:00:00+00:00",
            virtual_dirname="r1",
            title="Release 1",
            releasetype="album",
            year=2023,
            multidisc=False,
            new=False,
            genres=["Deep House", "Techno"],
            labels=["Silk Music"],
            artists=[
                CachedArtist(name="Bass Man", role="main"),
                CachedArtist(name="Techno Man", role="main"),
            ],
            formatted_artists="Techno Man;Bass Man",
        ),
    ]


@pytest.mark.usefixtures("seeded_cache")
def test_get_release(config: Config) -> None:
    assert get_release(config, "r1") == (
        CachedRelease(
            datafile_mtime="999",
            id="r1",
            source_path=Path(config.music_source_dir / "r1"),
            cover_image_path=None,
            added_at="0000-01-01T00:00:00+00:00",
            virtual_dirname="r1",
            title="Release 1",
            releasetype="album",
            year=2023,
            multidisc=False,
            new=False,
            genres=["Deep House", "Techno"],
            labels=["Silk Music"],
            artists=[
                CachedArtist(name="Bass Man", role="main"),
                CachedArtist(name="Techno Man", role="main"),
            ],
            formatted_artists="Techno Man;Bass Man",
        ),
        [
            CachedTrack(
                id="t1",
                source_path=config.music_source_dir / "r1" / "01.m4a",
                source_mtime="999",
                virtual_filename="01.m4a",
                title="Track 1",
                release_id="r1",
                track_number="01",
                disc_number="01",
                formatted_release_position="01",
                duration_seconds=120,
                artists=[
                    CachedArtist(name="Bass Man", role="main", alias=False),
                    CachedArtist(name="Techno Man", role="main", alias=False),
                ],
                formatted_artists="Techno Man;Bass Man",
            ),
            CachedTrack(
                id="t2",
                source_path=config.music_source_dir / "r1" / "02.m4a",
                source_mtime="999",
                virtual_filename="02.m4a",
                title="Track 2",
                release_id="r1",
                track_number="02",
                disc_number="01",
                formatted_release_position="02",
                duration_seconds=240,
                artists=[
                    CachedArtist(name="Bass Man", role="main", alias=False),
                    CachedArtist(name="Techno Man", role="main", alias=False),
                ],
                formatted_artists="Techno Man;Bass Man",
            ),
        ],
    )


@pytest.mark.usefixtures("seeded_cache")
def test_get_release_id_from_virtual_dirname(config: Config) -> None:
    assert get_release_id_from_virtual_dirname(config, "r1") == "r1"


@pytest.mark.usefixtures("seeded_cache")
def test_get_release_virtual_dirname_from_id(config: Config) -> None:
    assert get_release_virtual_dirname_from_id(config, "r1") == "r1"


@pytest.mark.usefixtures("seeded_cache")
def test_get_release_source_path_dirname_from_id(config: Config) -> None:
    assert str(get_release_source_path_from_id(config, "r1")).endswith("/source/r1")


@pytest.mark.usefixtures("seeded_cache")
def test_get_track_filename(config: Config) -> None:
    assert get_track_filename(config, "t1") == "01.m4a"


@pytest.mark.usefixtures("seeded_cache")
def test_list_artists(config: Config) -> None:
    artists = list(list_artists(config))
    assert set(artists) == {
        ("Techno Man", "Techno Man"),
        ("Bass Man", "Bass Man"),
        ("Violin Woman", "Violin Woman"),
        ("Conductor Woman", "Conductor Woman"),
    }


@pytest.mark.usefixtures("seeded_cache")
def test_list_genres(config: Config) -> None:
    genres = list(list_genres(config))
    assert set(genres) == {
        ("Techno", "Techno"),
        ("Deep House", "Deep House"),
        ("Classical", "Classical"),
    }


@pytest.mark.usefixtures("seeded_cache")
def test_list_labels(config: Config) -> None:
    labels = list(list_labels(config))
    assert set(labels) == {("Silk Music", "Silk Music"), ("Native State", "Native State")}


@pytest.mark.usefixtures("seeded_cache")
def test_list_collages(config: Config) -> None:
    collages = list(list_collages(config))
    assert set(collages) == {"Rose Gold", "Ruby Red"}


@pytest.mark.usefixtures("seeded_cache")
def test_list_collage_releases(config: Config) -> None:
    releases = list(list_collage_releases(config, "Rose Gold"))
    assert set(releases) == {
        (1, "r1", config.music_source_dir / "r1"),
        (2, "r2", config.music_source_dir / "r2"),
    }
    releases = list(list_collage_releases(config, "Ruby Red"))
    assert releases == []


@pytest.mark.usefixtures("seeded_cache")
def test_list_playlists(config: Config) -> None:
    playlists = list(list_playlists(config))
    assert set(playlists) == {"Lala Lisa", "Turtle Rabbit"}


@pytest.mark.usefixtures("seeded_cache")
def test_get_playlist(config: Config) -> None:
    pdata = get_playlist(config, "Lala Lisa")
    assert pdata is not None
    playlist, tracks = pdata
    assert playlist == CachedPlaylist(
        name="Lala Lisa",
        source_mtime="999",
        cover_path=config.music_source_dir / "!playlists" / "Lala Lisa.jpg",
        track_ids=["t1", "t3"],
    )
    assert tracks == [
        CachedTrack(
            id="t1",
            source_path=config.music_source_dir / "r1" / "01.m4a",
            source_mtime="999",
            virtual_filename="01.m4a",
            title="Track 1",
            release_id="r1",
            track_number="01",
            disc_number="01",
            formatted_release_position="01",
            duration_seconds=120,
            artists=[
                CachedArtist(name="Bass Man", role="main", alias=False),
                CachedArtist(name="Techno Man", role="main", alias=False),
            ],
            formatted_artists="Techno Man;Bass Man",
        ),
        CachedTrack(
            id="t3",
            source_path=config.music_source_dir / "r2" / "01.m4a",
            source_mtime="999",
            virtual_filename="01.m4a",
            title="Track 1",
            release_id="r2",
            track_number="01",
            disc_number="01",
            formatted_release_position="01",
            duration_seconds=120,
            artists=[
                CachedArtist(name="Conductor Woman", role="guest", alias=False),
                CachedArtist(name="Violin Woman", role="main", alias=False),
            ],
            formatted_artists="Violin Woman feat. Conductor Woman",
        ),
    ]


@pytest.mark.usefixtures("seeded_cache")
def test_release_exists(config: Config) -> None:
    assert release_exists(config, "r1")
    assert not release_exists(config, "lalala")


@pytest.mark.usefixtures("seeded_cache")
def test_track_exists(config: Config) -> None:
    assert track_exists(config, "r1", "01.m4a")
    assert not track_exists(config, "lalala", "lalala")
    assert not track_exists(config, "r1", "lalala")


@pytest.mark.usefixtures("seeded_cache")
def test_cover_exists(config: Config) -> None:
    assert cover_exists(config, "r2", "cover.jpg")
    assert not cover_exists(config, "r2", "cover.png")
    assert not cover_exists(config, "r1", "cover.jpg")


@pytest.mark.usefixtures("seeded_cache")
def test_artist_exists(config: Config) -> None:
    assert artist_exists(config, "Bass Man")
    assert not artist_exists(config, "lalala")


@pytest.mark.usefixtures("seeded_cache")
def test_genre_exists(config: Config) -> None:
    assert genre_exists(config, "Deep House")
    assert not genre_exists(config, "lalala")


@pytest.mark.usefixtures("seeded_cache")
def test_label_exists(config: Config) -> None:
    assert label_exists(config, "Silk Music")
    assert not label_exists(config, "Cotton Music")


@pytest.mark.usefixtures("seeded_cache")
def test_collage_exists(config: Config) -> None:
    assert collage_exists(config, "Rose Gold")
    assert not collage_exists(config, "lalala")


@pytest.mark.usefixtures("seeded_cache")
def test_playlist_exists(config: Config) -> None:
    assert playlist_exists(config, "Lala Lisa")
    assert not playlist_exists(config, "lalala")


def test_unpack() -> None:
    i = _unpack(r"Rose \\ Lisa \\ Jisoo \\ Jennie", r"vocal \\ dance \\ visual \\ vocal")
    assert list(i) == [
        ("Rose", "vocal"),
        ("Lisa", "dance"),
        ("Jisoo", "visual"),
        ("Jennie", "vocal"),
    ]
    assert list(_unpack("", "")) == []
