from pathlib import Path
from typing import Iterator

from rose.cache.database import connect
from rose.cache.dataclasses import CachedArtist, CachedRelease
from rose.foundation.conf import Config


def list_albums(c: Config) -> Iterator[CachedRelease]:
    with connect(c) as conn:
        cursor = conn.execute(
            r"""
            WITH genres AS (
                SELECT
                    release_id,
                    GROUP_CONCAT(genre, ' \\ ') AS genres
                FROM releases_genres
                GROUP BY release_id
            ), labels AS (
                SELECT
                    release_id,
                    GROUP_CONCAT(label, ' \\ ') AS labels
                FROM releases_labels
                GROUP BY release_id
            ), artists AS (
                SELECT
                    release_id,
                    GROUP_CONCAT(artist, ' \\ ') AS names,
                    GROUP_CONCAT(role, ' \\ ') AS roles
                FROM releases_artists
                GROUP BY release_id
            )
            SELECT
                r.id
              , r.source_path
              , r.virtual_dirname
              , r.title
              , r.release_type
              , r.release_year
              , r.new
              , COALESCE(g.genres, '') AS genres
              , COALESCE(l.labels, '') AS labels
              , COALESCE(a.names, '') AS artist_names
              , COALESCE(a.roles, '') AS artist_roles
            FROM releases r
            LEFT JOIN genres g ON g.release_id = r.id
            LEFT JOIN labels l ON l.release_id = r.id
            LEFT JOIN artists a ON a.release_id = r.id
            """
        )
        for row in cursor:
            artists: list[CachedArtist] = []
            for n, r in zip(row["artist_names"].split(r" \\ "), row["artist_roles"].split(r" \\ ")):
                artists.append(CachedArtist(name=n, role=r))
            yield CachedRelease(
                id=row["id"],
                source_path=Path(row["source_path"]),
                virtual_dirname=row["virtual_dirname"],
                title=row["title"],
                release_type=row["release_type"],
                release_year=row["release_year"],
                new=bool(row["new"]),
                genres=sorted(row["genres"].split(r" \\ ")),
                labels=sorted(row["labels"].split(r" \\ ")),
                artists=artists,
            )
