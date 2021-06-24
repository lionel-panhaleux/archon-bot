"""Discord Bot."""
import asyncio
import collections
import logging
import os

import discord


from .commands import Command, CommandFailed
from .db import CONNECTION, init

#: Lock for write operations
LOCKS = collections.defaultdict(asyncio.Lock)

# ####################################################################### Logging config
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# ####################################################################### Discord client
client = discord.Client(
    intents=discord.Intents(
        guilds=True,
        members=True,
        voice_states=True,
        presences=True,
        messages=True,
    )
)


# ########################################################################### Bot events
@client.event
async def on_ready():
    """Login success informative log."""
    logger.info("Logged in as %s", client.user)
    await init()


@client.event
async def on_message(message: discord.Message):
    """Main message loop."""
    if message.author == client.user:
        return

    if not message.content.lower().startswith("archon"):
        return
    logging.info('%s said: "%s"', message.author.display_name, message.content)
    content = message.content[6:].split()
    command, update = {
        "help": (Command.help, True),
        "open": (Command.open, True),
        "status": (Command.status, False),
        "checkin": (Command.checkin, True),
        "report": (Command.report, True),
        "drop": (Command.drop, True),
        "appoint": (Command.appoint, False),
        "spectator": (Command.spectator, False),
        "register": (Command.register, False),
        "allcheck": (Command.allcheck, True),
        "uncheck": (Command.uncheck, True),
        "upload": (Command.upload, True),
        "seat": (Command.seat, True),
        "add": (Command.add, True),
        "unseat": (Command.unseat, True),
        "players": (Command.players, False),
        "player": (Command.player, False),
        "registrations": (Command.registrations, False),
        "results": (Command.results, False),
        "standings": (Command.standings, False),
        "fix": (Command.fix, True),
        "validate": (Command.validate, True),
        "caution": (Command.caution, True),
        "warn": (Command.warn, True),
        "disqualify": (Command.disqualify, True),
        "close": (Command.close, True),
    }.get(content[0].lower() if content else "", (None, False))
    if not command:
        command = Command.default
    if not getattr(message.channel, "guild", None):
        await message.channel.send("Archon cannot be used in a private channel.")
        return
    try:
        await _execute(command, message, update, *content[1:])
    except CommandFailed:
        pass
    except Exception:
        logger.exception("Command failed %s", content)
        await message.channel.send("Command error. Use `archon help` to display help.")


async def _execute(command, message, update, *args):
    """Execute a command - handle DB transactions and asyncio lock."""
    connection = await CONNECTION.get()
    try:
        if update:
            await LOCKS[message.channel.guild.id].acquire()
        cursor = connection.cursor()
        cursor.execute(
            "SELECT data from tournament WHERE active=1 AND guild=?",
            [str(message.channel.guild.id)],
        )
        tournament = cursor.fetchone()
        if tournament:
            instance = Command(connection, message, tournament[0])
        else:
            instance = Command(connection, message)
        await command(instance, *args)
    except:  # noqa: E722
        connection.rollback()
        raise
    else:
        connection.commit()
    finally:
        CONNECTION.put_nowait(connection)
        if update:
            LOCKS[message.channel.guild.id].release()


def main():
    """Entrypoint for the Discord Bot."""
    client.run(os.getenv("DISCORD_TOKEN"))
