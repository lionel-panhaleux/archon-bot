"""Discord Bot."""
import asyncio
import collections
import logging
import os

import hikari
import krcg.vtes

from .commands import (
    APPLICATION,
    COMMANDS,
    COMPONENTS,
    COMMANDS_IDS,
    CommandFailed,
    CommandAccess,
)
from .commands import set_admin_permissions
from .commands import set_judge_permissions
from . import db
from .tournament import Tournament

#: Lock for write operations
LOCKS = collections.defaultdict(asyncio.Lock)

# ####################################################################### Logging config
logger = logging.getLogger()
logging.basicConfig(
    level=logging.DEBUG if os.getenv("DEBUG") else logging.INFO,
    format="[%(levelname)7s] %(message)s",
)

# ####################################################################### Discord client
bot = hikari.GatewayBot(os.getenv("DISCORD_TOKEN") or "")

# ####################################################################### Init KRCG
krcg.vtes.VTES.load()


# ########################################################################### Bot events
@bot.listen()
async def on_ready(event: hikari.StartedEvent) -> None:
    """Login success informative log."""
    logger.info("Ready as %s", bot.get_me().username)
    await db.init()


@bot.listen()
async def on_connected(event: hikari.GuildAvailableEvent) -> None:
    logger.info("Logged in %s as %s", event.guild.name, bot.get_me().username)
    if not APPLICATION:
        APPLICATION.append(await bot.rest.fetch_application())
    application = APPLICATION[0]
    guild = event.guild
    commands = []
    for name, klass in COMMANDS.items():
        command = bot.rest.slash_command_builder(
            name, klass.DESCRIPTION
        ).set_default_permission(klass.ACCESS == CommandAccess.PUBLIC)
        for option in klass.OPTIONS:
            command = command.add_option(option)
        commands.append(command)
    try:
        commands = await bot.rest.set_application_commands(
            application=application,
            commands=commands,
            guild=guild,
        )
    except hikari.ForbiddenError:
        logger.error("Bot does not have commands scope in guild %s", guild)
        return
    for command in commands:
        COMMANDS[command.id] = COMMANDS[command.name]
        COMMANDS_IDS[guild.id, command.name] = command.id
    await set_admin_permissions(bot, guild.id)
    async with db.connection() as connection:
        await set_judge_permissions(connection, bot, guild.id)


async def _interaction_response(instance, interaction, content):
    if instance:
        await instance.create_or_edit_response(
            content, flags=hikari.MessageFlag.EPHEMERAL, embeds=[]
        )
    else:
        await interaction.create_initial_response(
            hikari.interactions.base_interactions.ResponseType.MESSAGE_CREATE,
            content,
            flags=hikari.MessageFlag.EPHEMERAL,
            embeds=[],
        )


@bot.listen()
async def on_interaction(event: hikari.InteractionCreateEvent) -> None:
    logger.info("Interaction %s", event.interaction)
    if not event.interaction.guild_id:
        await _interaction_response(
            event.interaction,
            "Archon cannot be used in a private channel",
        )
        return
    if event.interaction.type == hikari.InteractionType.APPLICATION_COMMAND:
        try:
            instance = None
            command = COMMANDS[event.interaction.command_id]
            channel = event.interaction.get_channel()
            if not channel:
                channel = event.interaction.fetch_channel()
            async with db.tournament(
                event.interaction.guild_id,
                channel.parent_id,
                command.UPDATE,
            ) as (
                connection,
                tournament_data,
            ):
                instance = command(
                    bot,
                    connection,
                    Tournament(**tournament_data) if tournament_data else None,
                    event.interaction,
                    channel.id,
                    channel.parent_id,
                )
                await instance(
                    **{
                        option.name: option.value
                        for option in event.interaction.options or []
                    }
                )
        except CommandFailed as exc:
            logger.info("Command failed: %s - %s", event.interaction, exc.args)
            if exc.args:
                await _interaction_response(instance, event.interaction, exc.args[0])
        except Exception:
            logger.exception("Command failed: %s", event.interaction)
            await _interaction_response(instance, event.interaction, "Command error.")

    elif event.interaction.type == hikari.InteractionType.MESSAGE_COMPONENT:
        try:
            instance = None
            component_function = COMPONENTS[event.interaction.custom_id]
            channel = event.interaction.get_channel()
            if not channel:
                channel = event.interaction.fetch_channel()
            async with db.tournament(
                event.interaction.guild_id,
                channel.parent_id,
                component_function.UPDATE,
            ) as (
                connection,
                tournament_data,
            ):
                instance = component_function(
                    bot,
                    connection,
                    Tournament(**tournament_data) if tournament_data else None,
                    event.interaction,
                    channel.id,
                    channel.parent_id,
                )
                await instance()
        except CommandFailed as exc:
            logger.info("Command failed: %s - %s", event.interaction, exc.args)
            if exc.args:
                await _interaction_response(instance, event.interaction, exc.args[0])
        except Exception:
            logger.exception("Command failed: %s", event.interaction)
            await _interaction_response(instance, event.interaction, "Command error.")


def main():
    """Entrypoint for the Discord Bot."""
    bot.run()
