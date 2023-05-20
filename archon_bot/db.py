import collections
import contextlib
import enum
import os
import random
import logging
import orjson
import psycopg
import psycopg.types.json
import psycopg_pool
from typing import Any, AsyncIterator


logger = logging.getLogger()


def orjson_dumps(obj: Any) -> str:
    """Adapter required because for now psycopg expects str

    See https://github.com/psycopg/psycopg/issues/569
    """
    return orjson.dumps(obj).decode()


async def configure(conn: psycopg.AsyncConnection) -> None:
    await conn.set_read_only(True)


async def reset_connection(conn: psycopg.AsyncConnection) -> None:
    await conn.set_read_only(True)


def reconnect_failed(_pool: psycopg_pool.AsyncConnectionPool) -> None:
    logger.error("Failed to reconnect to the PostgreSQL database")


DB_USER = os.getenv("DB_USER")
DB_PWD = os.getenv("DB_PWD")
psycopg.types.json.set_json_dumps(orjson_dumps)
psycopg.types.json.set_json_loads(orjson.loads)
POOL = psycopg_pool.AsyncConnectionPool(
    f"postgresql://{DB_USER}:{DB_PWD}@localhost/archon",
    open=False,
    max_size=10,
    configure=configure,
    reset=reset_connection,
    reconnect_failed=reconnect_failed,
)

#: Cache for read operations
TOURNAMENTS = collections.defaultdict(dict)


class UpdateLevel(enum.IntEnum):
    READ_ONLY = 0  # no lock, just read, use cache if possible
    WRITE = 1  # lock, other writes operation wait under a timeout
    EXCLUSIVE_WRITE = 2  # major change: do not wait, fail concurrent writes


class ExclusiveLock(RuntimeError):
    pass


@contextlib.asynccontextmanager
async def connection(
    guild_id: int, category_id: int, update=UpdateLevel.READ_ONLY
) -> AsyncIterator[psycopg.AsyncConnection[Any]]:
    """An async context manager that yields an AsyncConnection to the database.

    It handles locks and read_only setting based on the given update level.
    """
    async with POOL.connection() as conn:
        if update:
            await conn.set_read_only(False)
            if update > UpdateLevel.WRITE:
                # "long writes" wait to take an exclusive lock
                async with conn.cursor() as cur:
                    await cur.execute(
                        " select pg_advisory_xact_lock(%s)",
                        [hash((guild_id, category_id))],
                    )
            else:
                # normal writes take a shared lock that fails immediately (no wait)
                # if an exclusively lock has been taken already
                async with conn.cursor() as cur:
                    await cur.execute(
                        " select pg_try_advisory_xact_lock_shared(%s)",
                        [hash((guild_id, category_id))],
                    )
                    if not (await cur.fetchone())[0]:
                        raise ExclusiveLock()
        yield conn


async def init(reset: bool = False) -> None:
    await POOL.open()
    async with POOL.connection() as conn:
        await conn.set_read_only(False)
        async with conn.cursor() as cursor:
            if reset:
                logger.warning("Reset DB")
                await cursor.execute("DROP TABLE IF EXISTS tournament")
                await cursor.execute("DROP INDEX IF EXISTS tournament_agc")
            logger.debug("Initialising DB")
            await cursor.execute(
                "CREATE TABLE IF NOT EXISTS tournament("
                "id UUID DEFAULT gen_random_uuid() PRIMARY KEY, "
                "active BOOLEAN, "
                "guild TEXT, "
                "category TEXT, "
                "data json)"
            )
            await cursor.execute(
                "CREATE INDEX IF NOT EXISTS tournament_agc ON tournament("
                "active, "
                "guild, "
                "category)"
            )


async def create_tournament(
    conn: psycopg.AsyncConnection[Any],
    guild_id: int,
    category_id: int,
    tournament_data: dict,
) -> None:
    logger.debug("New tournament %s-%s: %s", guild_id, category_id, tournament_data)
    async with conn.cursor() as cursor:
        await cursor.execute(
            "INSERT INTO tournament (active, guild, category, data) "
            "VALUES (TRUE, %s, %s, %s)",
            [
                str(guild_id),
                str(category_id) if category_id else "",
                psycopg.types.json.Json(tournament_data),
            ],
        )


async def get_active_tournaments(
    conn: psycopg.AsyncConnection[Any], guild_id: int
) -> list[dict]:
    async with conn.cursor() as cursor:
        await cursor.execute(
            "SELECT data from tournament WHERE active=TRUE AND guild=%s FOR SHARE",
            [str(guild_id)],
        )
        return list(r[0] for r in cursor)


async def update_tournament(conn, guild_id, category_id, tournament_data):
    """Update tournament data. Caches the data."""
    logger.debug("Update tournament %s-%s: %s", guild_id, category_id, tournament_data)
    if len(TOURNAMENTS) > 5:  # 5 tournaments in cache should be enough
        keep = {k: v for k, v in random.sample(TOURNAMENTS.items(), 4)}
        TOURNAMENTS.clear()
        TOURNAMENTS.update(keep)
    # beware to update the cache before asking for a write
    TOURNAMENTS[(guild_id, category_id)] = tournament_data
    async with conn.cursor() as cursor:
        await cursor.execute(
            "UPDATE tournament SET data=%s "
            "WHERE active=TRUE AND guild=%s AND category=%s",
            [
                psycopg.types.json.Json(tournament_data),
                str(guild_id),
                str(category_id) if category_id else "",
            ],
        )


@contextlib.asynccontextmanager
async def tournament(guild_id, category_id, update: UpdateLevel):
    """Context manager to access a tournament object. Uses cached data if available."""
    # do not consume a DB connection for READ_ONLY operations if data is in the cache
    if update < UpdateLevel.WRITE and (guild_id, category_id) in TOURNAMENTS:
        yield None, TOURNAMENTS[(guild_id, category_id)]
    else:
        async with connection(guild_id, category_id, update=update) as conn:
            tournament = None
            async with conn.cursor() as cursor:
                await cursor.execute(
                    "SELECT data from tournament "
                    "WHERE active=TRUE AND guild=%s AND category=%s"
                    + (" FOR UPDATE" if update else ""),
                    [str(guild_id), str(category_id) if category_id else ""],
                )
                res = await cursor.fetchone()
                if res:
                    tournament = res[0]
                    # beware of concurrency with locked write operations here
                    # it is OK to set the cache if it is empty, but do not overwrite
                    # a locked write cache update with the return of a previous read
                    if (guild_id, category_id) not in TOURNAMENTS:
                        TOURNAMENTS[(guild_id, category_id)] = tournament
            yield conn, tournament


async def close_tournament(conn, guild_id, category_id):
    """Close a tournament. Remove it from cache."""
    logger.debug("Closing tournament %s-%s", guild_id, category_id)
    TOURNAMENTS.pop((guild_id, category_id), None)
    async with conn.cursor() as cursor:
        await cursor.execute(
            "UPDATE tournament SET active=FALSE "
            "WHERE active=TRUE AND guild=%s AND category=%s",
            [str(guild_id), str(category_id) if category_id else ""],
        )
