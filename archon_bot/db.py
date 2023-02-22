import asyncio
import collections
import contextlib
import enum
import os
import random
import logging
import pkg_resources
import psycopg2
import psycopg2.extensions
import psycopg2.extras

logger = logging.getLogger()
version = pkg_resources.Environment()["archon-bot"][0].version
DB_USER = os.getenv("DB_USER")
DB_PWD = os.getenv("DB_PWD")
psycopg2.extensions.register_adapter(dict, psycopg2.extras.Json)
psycopg2.extensions.register_adapter(list, psycopg2.extras.Json)
CONNECTION = asyncio.Queue(maxsize=10)  # 1 connection per ongoing tournament

#: Lock for write operations
LOCKS = collections.defaultdict(asyncio.Lock)
#: set when a long transation is going on (then do not even try to acquire a lock)
LONG = collections.defaultdict(bool)
#: Cache for read operations
TOURNAMENTS = collections.defaultdict(dict)
#: Cache for read operations
GUILDS = collections.defaultdict(dict)


class UpdateLevel(enum.IntEnum):
    READ_ONLY = 0  # no lock, just read, use cache if possible
    WRITE = 1  # lock, other writes operation wait under a timeout
    EXCLUSIVE_WRITE = 2  # major change: do not wait, fail concurrent writes


@contextlib.asynccontextmanager
async def connection(guild_id, category_id, update=UpdateLevel.READ_ONLY):
    conn = None
    try:
        if update:
            # raise is there's a long update going on (eg. seating)
            if LONG[guild_id, category_id]:
                raise asyncio.TimeoutError()
            lock = asyncio.ensure_future(LOCKS[guild_id, category_id].acquire())
            if update > UpdateLevel.WRITE:
                LONG[guild_id, category_id] = True
            # Lock timeout is 5s - use the same (5000 ms) in your DB settings
            # lock_timeout and statement_timeout
            await asyncio.wait_for(lock, 5)
        conn = await CONNECTION.get()
        yield conn
    except:  # noqa: E722
        conn.rollback()
        raise
    else:
        conn.commit()
    finally:
        if conn:
            CONNECTION.put_nowait(conn)
            if update:
                del LONG[guild_id, category_id]
                LOCKS[guild_id, category_id].release()


async def init():
    while True:
        try:
            conn = CONNECTION.get_nowait()
            conn.close()
        except asyncio.QueueEmpty:
            break
    for _ in range(CONNECTION.maxsize):
        CONNECTION.put_nowait(
            psycopg2.connect(
                dbname="archon",
                user=DB_USER,
                password=DB_PWD,
            )
        )
    async with connection(None, None) as conn:
        cursor = conn.cursor()
        logger.debug("Initialising DB")
        cursor.execute(
            "CREATE TABLE IF NOT EXISTS tournament("
            "active BOOLEAN, "
            "guild TEXT, "
            "category TEXT, "
            "data json)"
        )


async def reset():
    async with connection(None, None) as conn:
        cursor = conn.cursor()
        logger.warning("Reset DB")
        cursor.execute("TRUNCATE TABLE tournament")


def create_tournament(conn, guild_id, category_id, tournament_data):
    cursor = conn.cursor()
    logger.debug("New tournament %s-%s: %s", guild_id, category_id, tournament_data)
    cursor.execute(
        "INSERT INTO tournament (active, guild, category, data) "
        "VALUES (TRUE, %s, %s, %s)",
        [
            str(guild_id),
            str(category_id) if category_id else "",
            tournament_data,
        ],
    )


async def get_active_tournaments(conn, guild_id):
    cursor = conn.cursor()
    cursor.execute(
        "SELECT data from tournament WHERE active=TRUE AND guild=%s FOR SHARE",
        [str(guild_id)],
    )
    return list(r[0] for r in cursor)


def update_tournament(conn, guild_id, category_id, tournament_data):
    """Update tournament data. Caches the data."""
    cursor = conn.cursor()
    logger.debug("Update tournament %s-%s: %s", guild_id, category_id, tournament_data)
    if len(TOURNAMENTS) > 5:  # 5-6 tournaments in cache should be enough
        keep = {k: v for k, v in random.sample(TOURNAMENTS.items(), 4)}
        TOURNAMENTS.clear()
        TOURNAMENTS.update(keep)
    # beware to update the cache before asking for a write
    TOURNAMENTS[(guild_id, category_id)] = tournament_data
    cursor.execute(
        "UPDATE tournament SET data=%s WHERE active=TRUE AND guild=%s AND category=%s",
        [
            tournament_data,
            str(guild_id),
            str(category_id) if category_id else "",
        ],
    )


@contextlib.asynccontextmanager
async def tournament(guild_id, category_id, update=False):
    """Context manager to access a tournament object. Uses cached data if available."""
    # do not consume a DB connection for READ_ONLY operations if data is in the cache
    if update < UpdateLevel.WRITE and (guild_id, category_id) in TOURNAMENTS:
        yield None, TOURNAMENTS[(guild_id, category_id)]
    else:
        async with connection(guild_id, category_id) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT data from tournament "
                "WHERE active=TRUE AND guild=%s AND category=%s"
                + (" FOR UPDATE" if update else ""),
                [str(guild_id), str(category_id) if category_id else ""],
            )
            tournament = cursor.fetchone()
            if tournament:
                # beware of concurrency with locked write operations here
                # it is OK to set the cache if it is empty, but do not overwrite
                # a locked write cache update with the return of a previous read
                if (guild_id, category_id) not in TOURNAMENTS:
                    TOURNAMENTS[(guild_id, category_id)] = tournament[0]
                yield conn, tournament[0]
            else:
                yield conn, None


def close_tournament(conn, guild_id, category_id):
    """Close a tournament. Remove it from cache."""
    cursor = conn.cursor()
    logger.debug("Closing tournament %s-%s", guild_id, category_id)
    TOURNAMENTS.pop((guild_id, category_id), None)
    cursor.execute(
        "UPDATE tournament SET active=FALSE "
        "WHERE active=TRUE AND guild=%s AND category=%s",
        [str(guild_id), str(category_id) if category_id else ""],
    )


def upsert_guild_config(conn, guild_id, config_data):
    cursor = conn.cursor()
    logger.debug("New guild config %s: %s", guild_id, config_data)
    cursor.execute(
        "INSERT INTO discord_config (guild, data) " "VALUES (%s, %s)",
        [
            str(guild_id),
            config_data,
        ],
    )
    GUILDS.pop(guild_id, None)


def guild_config(conn, guild_id):
    """Get the guild config data, use the cache if available."""
    # do not consume a DB connection if data is in the cache
    if guild_id in GUILDS:
        return None, GUILDS[guild_id]
    else:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT data from discord_config " "WHERE guild=%s",
            [str(guild_id)],
        )
        guild_config = cursor.fetchone()
        if guild_config:
            # beware of concurrency with locked write operations here
            # it is OK to set the cache if it is empty, but do not overwrite
            # a locked write cache update with the return of a previous read
            if guild_id not in GUILDS:
                GUILDS[guild_id] = guild_config[0]
            return conn, guild_config[0]
        else:
            return conn, None
