"""Discord Bot."""
import asyncio
import collections
import logging
import os

import discord


from .commands import COMMANDS, CommandFailed
from . import db

#: Lock for write operations
LOCKS = collections.defaultdict(asyncio.Lock)

# ####################################################################### Logging config
logger = logging.getLogger()
logging.basicConfig(level=logging.INFO, format="[%(levelname)7s] %(message)s")

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
    await db.init()


@client.event
async def on_message(message: discord.Message):
    """Main message loop."""
    if message.author == client.user:
        return
    if not message.content.lower().startswith("archon"):
        return
    logging.info('%s said: "%s"', message.author.display_name, message.content)
    content = message.content[6:].split()
    command = COMMANDS.get(content[0].lower() if content else "", COMMANDS["help"])
    if not getattr(message.channel, "guild", None):
        await message.channel.send("Archon cannot be used in a private channel.")
        return
    try:
        async with db.tournament(
            message.channel.guild.id,
            message.channel.category.id if message.channel.category else None,
            command.UPDATE,
        ) as (
            connection,
            tournament,
        ):
            instance = command(connection, message, tournament)
            await instance(*content[1:])
    except CommandFailed as exc:
        logger.exception("Command failed: %s")
        if exc.args:
            await message.channel.send(exc.args[0], reference=message)
    except Exception:
        logger.exception("Command failed: %s", content)
        await message.channel.send("Command error. Use `archon help` to display help.")


def main():
    """Entrypoint for the Discord Bot."""
    client.run(os.getenv("DISCORD_TOKEN"))
