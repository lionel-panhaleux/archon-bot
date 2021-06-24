import asyncio
import collections
import contextlib
import json
import pkg_resources
import sqlite3


version = pkg_resources.Environment()["archon-bot"][0].version
sqlite3.register_adapter(dict, json.dumps)
sqlite3.register_adapter(list, json.dumps)
sqlite3.register_converter("json", json.loads)
CONNECTION = asyncio.Queue(maxsize=5)

#: Lock for write operations
LOCKS = collections.defaultdict(asyncio.Lock)


async def init():
    while True:
        try:
            connection = CONNECTION.get_nowait()
            connection.close()
        except asyncio.QueueEmpty:
            break
    for _ in range(CONNECTION.maxsize):
        CONNECTION.put_nowait(
            sqlite3.connect(
                f"archon-{version}.db", detect_types=sqlite3.PARSE_DECLTYPES
            )
        )
    connection = await CONNECTION.get()
    cursor = connection.cursor()
    cursor.execute(
        "CREATE TABLE IF NOT EXISTS tournament(active INTEGER, guild TEXT, data json)"
    )
    connection.commit()
    CONNECTION.put_nowait(connection)


async def create_tournament(guild_id, tournament_data):
    connection = await CONNECTION.get()
    cursor = connection.cursor()
    cursor.execute(
        "INSERT INTO tournament (active, guild, data) VALUES (1, ?, ?)",
        [str(guild_id), tournament_data],
    )
    connection.commit()
    CONNECTION.put_nowait(connection)


def update_tournament(connection, guild_id, tournament_data):
    cursor = connection.cursor()
    cursor.execute(
        "UPDATE tournament SET data=? WHERE active=1 AND guild=?",
        [
            tournament_data,
            str(guild_id),
        ],
    )


@contextlib.asynccontextmanager
async def tournament(guild_id, update=False):
    """Context manager to access a tournament object.

    Handles DB transactions (commit, rollback) and asyncio lock for write operations
    """
    connection = await CONNECTION.get()
    try:
        if update:
            await LOCKS[guild_id].acquire()
        cursor = connection.cursor()
        cursor.execute(
            "SELECT data from tournament WHERE active=1 AND guild=?",
            [str(guild_id)],
        )
        tournament = cursor.fetchone()
        if tournament:
            yield connection, tournament[0]
        else:
            yield connection, None
    except:  # noqa: E722
        connection.rollback()
        raise
    else:
        connection.commit()
    finally:
        CONNECTION.put_nowait(connection)
        if update:
            LOCKS[guild_id].release()


def close_tournament(connection, guild_id):
    cursor = connection.cursor()
    cursor.execute(
        "UPDATE tournament SET active=0 WHERE active=1 AND guild=?",
        [
            str(guild_id),
        ],
    )
