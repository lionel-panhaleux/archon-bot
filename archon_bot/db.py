import asyncio
import json
import pkg_resources
import sqlite3


version = pkg_resources.Environment()["archon-bot"][0].version
sqlite3.register_adapter(dict, json.dumps)
sqlite3.register_adapter(list, json.dumps)
sqlite3.register_converter("json", json.loads)
CONNECTION = asyncio.Queue(maxsize=5)


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
