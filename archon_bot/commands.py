import asyncio
import enum
import functools
import itertools
import logging
from typing import List, Optional, Union

import hikari
from hikari.interactions.base_interactions import ResponseType
from hikari.interactions.command_interactions import CommandInteraction
from hikari.interactions.component_interactions import ComponentInteraction
import krcg.deck

import stringcase


from . import db
from . import tournament
from . import permissions as perm

logger = logging.getLogger()
CommandFailed = tournament.CommandFailed

APPLICATION = []
COMMANDS = {}
COMPONENTS = {}
COMMANDS_IDS = {}


class MetaCommand(type):
    """Metaclass to register commands."""

    def __new__(cls, name, bases, dict_):
        command_name = stringcase.spinalcase(name)
        if command_name in COMMANDS:
            raise ValueError(f"Command {name} is already registered")
        klass = super().__new__(cls, name, bases, dict_)
        if command_name == "base-command":
            return klass
        COMMANDS[command_name] = klass
        return klass


class CommandAccess(str, enum.Enum):
    PUBLIC = "PUBLIC"
    ADMIN = "ADMIN"
    PLAYER = "PLAYER"
    JUDGE = "JUDGE"


def _split_text(s, limit):
    """Utility function to split a text at a convenient spot."""
    if len(s) < limit:
        return s, ""
    index = s.rfind("\n", 0, limit)
    rindex = index + 1
    if index < 0:
        index = s.rfind(" ", 0, limit)
        rindex = index + 1
        if index < 0:
            index = limit
            rindex = index
    return s[:index], s[rindex:]


def _paginate_embed(embed: hikari.Embed) -> List[hikari.Embed]:
    """Utility function to paginate a Discord Embed"""
    embeds = []
    fields = []
    base_title = embed.title
    description = ""
    page = 1
    logger.debug("embed: %s", embed)
    while embed:
        if embed.description:
            embed.description, description = _split_text(embed.description, 2048)
        while embed.fields and (len(embed.fields) > 15 or description):
            fields.append(embed.fields[-1])
            embed.remove_field(-1)
        embeds.append(embed)
        if description or fields:
            page += 1
            embed = hikari.Embed(
                title=base_title + f" ({page})",
                description=description,
            )
            for field in fields:
                embed.add_field(name=field.name, value=field.value, inline=field.inline)
            description = ""
            fields = []
        else:
            embed = None
    if len(embeds) > 10:
        raise RuntimeError("Too many embeds")
    return embeds


class BaseInteraction:
    """Base class for all interactions (commands and components)"""

    #: The interaction does not update tournament data. Override in children as needed.
    UPDATE = False

    def __init__(
        self,
        bot: hikari.GatewayBot,
        connection,
        tournament_: tournament.Tournament,
        interaction: Union[CommandInteraction, ComponentInteraction],
        channel_id: hikari.Snowflake,
        category_id=None,
    ):
        self.bot: hikari.GatewayBot = bot
        self.connection = connection
        self.interaction: Union[CommandInteraction, ComponentInteraction] = interaction
        self.channel_id: hikari.Snowflake = channel_id
        self.author: hikari.InteractionMember = self.interaction.member
        self.guild_id: hikari.Snowflake = self.interaction.guild_id
        self.category_id: hikari.Snowflake = category_id
        self.tournament: tournament.Tournament = tournament_
        self.has_response: bool = False

    @classmethod
    def copy_from_interaction(cls, rhs, *args, **kwargs):
        ret = cls(
            *args,
            bot=rhs.bot,
            connection=rhs.connection,
            tournament_=rhs.tournament,
            interaction=rhs.interaction,
            channel_id=rhs.channel_id,
            category_id=rhs.category_id,
            **kwargs,
        )
        ret.has_response = rhs.has_response
        return ret

    def update(self):
        """Update tournament data."""
        if not self.UPDATE:
            raise RuntimeError("Command is not marked as UPDATE")
        data = self.tournament.to_json()
        db.update_tournament(
            self.connection,
            self.guild_id,
            self.category_id,
            data,
        )

    def _is_judge(self):
        """Check whether the author is a judge."""
        judge_role = self.tournament.roles[self.tournament.JUDGE]
        return judge_role in self.author.role_ids

    def _is_judge_channel(self):
        """Check wether the command was issued in the Judges private channel."""
        return (
            self.channel_id
            == self.tournament.channels[tournament.Tournament.JUDGE_TEXT]
        )

    def _player_display(self, player_id: tournament.PlayerID):
        """How to display a player."""
        player = self.tournament.players[player_id]
        return (
            ("**[D]** " if player.vekn in self.tournament.dropped else "")
            + (f"{player.name} #{player.vekn} " if player.name else f"#{player.vekn} ")
            + (f"<@!{player.discord}>" if player.discord else "")
        )

    @property
    def reason(self):
        """Reason given for Discord logs on channel/role creations."""
        return f"{self.tournament.name} Tournament"

    async def __call__(self):
        raise NotImplementedError()


class BaseCommand(BaseInteraction, metaclass=MetaCommand):
    """Base class for all commands"""

    #: Command description. Override in children.
    DESCRIPTION = ""
    #: Command access depending on member state in tournament. Override in children.
    ACCESS = CommandAccess.PLAYER
    #: Define command options. Override in children as needed.
    OPTIONS = []

    async def deferred(self, flags: Optional[hikari.MessageFlag] = None):
        await self.interaction.create_initial_response(
            ResponseType.DEFERRED_MESSAGE_CREATE, flags=flags
        )
        self.has_response = True

    async def create_or_edit_response(self, *args, **kwargs) -> None:
        flags = kwargs.pop("flags", None)
        if self.has_response:
            func = self.interaction.edit_initial_response
        else:
            func = functools.partial(
                self.interaction.create_initial_response,
                ResponseType.MESSAGE_CREATE,
                flags=flags,
            )
        await func(*args, **kwargs)
        self.has_response = True


class BaseComponent(BaseInteraction):
    async def deferred(self, flags: Optional[hikari.MessageFlag] = None):
        await self.interaction.create_initial_response(
            ResponseType.DEFERRED_MESSAGE_UPDATE, flags=flags
        )
        self.has_response = True

    async def create_or_edit_response(self, *args, **kwargs) -> None:
        flags = kwargs.pop("flags", None)
        if self.has_response:
            func = self.interaction.edit_initial_response
        else:
            func = functools.partial(
                self.interaction.create_initial_response,
                ResponseType.MESSAGE_UPDATE,
                flags=flags,
            )
        await func(*args, **kwargs)
        self.has_response = True


async def set_admin_permissions(
    bot: hikari.GatewayBot,
    guild_id: hikari.Snowflakeish,
) -> None:
    """Set commands permissions for admins"""
    admin_commands = [
        COMMANDS_IDS[(guild_id, stringcase.spinalcase(command.__name__))]
        for command in set(COMMANDS.values())
        if command.ACCESS == CommandAccess.ADMIN
    ]
    roles = await bot.rest.fetch_roles(guild_id)
    admin_roles = [
        role.id for role in roles if hikari.Permissions.ADMINISTRATOR & role.permissions
    ]
    admin_roles = admin_roles[:10]
    await bot.rest.set_application_guild_commands_permissions(
        application=APPLICATION[0].id,
        guild=guild_id,
        permissions={
            command_id: [
                hikari.CommandPermission(
                    id=role_id, type=hikari.CommandPermissionType.ROLE, has_access=True
                )
                for role_id in admin_roles
            ]
            for command_id in admin_commands
        },
    )


async def set_judge_permissions(
    connection,
    bot: hikari.GatewayBot,
    guild_id: hikari.Snowflakeish,
) -> None:
    """Set commands permissions for judges"""
    judge_roles = []
    player_roles = []
    for data in await db.get_active_tournaments(connection, guild_id):
        tourney = tournament.Tournament(**data)
        judge_roles.append(tourney.roles[tourney.JUDGE])
        player_roles.append(tourney.roles[tourney.PLAYER])
    judge_roles = judge_roles[:10]
    player_roles = player_roles[:10]
    judge_commands = [
        COMMANDS_IDS[(guild_id, stringcase.spinalcase(command.__name__))]
        for command in set(COMMANDS.values())
        if command.ACCESS == CommandAccess.JUDGE
    ]
    player_commands = [
        COMMANDS_IDS[(guild_id, stringcase.spinalcase(command.__name__))]
        for command in set(COMMANDS.values())
        if command.ACCESS == CommandAccess.PLAYER
    ]
    permissions = {
        command_id: [
            hikari.CommandPermission(
                id=role_id, type=hikari.CommandPermissionType.ROLE, has_access=True
            )
            for role_id in judge_roles
        ]
        for command_id in judge_commands
    }
    permissions.update(
        {
            command_id: [
                hikari.CommandPermission(
                    id=role_id, type=hikari.CommandPermissionType.ROLE, has_access=True
                )
                for role_id in player_roles
            ]
            for command_id in player_commands
        }
    )
    await bot.rest.set_application_guild_commands_permissions(
        application=APPLICATION[0].id,
        guild=guild_id,
        permissions=permissions,
    )


class OpenTournament(BaseCommand):
    UPDATE = True
    ACCESS = CommandAccess.PUBLIC
    DESCRIPTION = "Open a new event or tournament"
    OPTIONS = [
        hikari.CommandOption(
            type=hikari.OptionType.STRING,
            name="name",
            description="The tournament name",
            is_required=True,
        ),
        hikari.CommandOption(
            type=hikari.OptionType.INTEGER,
            name="rounds",
            description=(
                "The maximum number of rounds a player can play (not counting finals)"
            ),
            is_required=False,
            min_value=1,
        ),
    ]

    async def __call__(self, name: str, rounds: Optional[int] = 0):
        if self.tournament:
            raise CommandFailed("A tournament is already open here")
        await self.deferred()
        self.tournament = tournament.Tournament(name=name)
        if rounds:
            self.tournament.max_rounds = rounds
        logger.debug("Creating roles...")
        judge_role, spectator_role, player_role = await asyncio.gather(
            self.bot.rest.create_role(
                self.guild_id,
                name=f"{self.tournament.prefix}Judge",
                mentionable=True,
                reason=self.reason,
            ),
            self.bot.rest.create_role(
                self.guild_id,
                name=f"{self.tournament.prefix}Spectator",
                mentionable=True,
                reason=self.reason,
            ),
            self.bot.rest.create_role(
                self.guild_id,
                name=f"{self.tournament.prefix}Player",
                mentionable=True,
                reason=self.reason,
            ),
        )
        self.tournament.roles[self.tournament.JUDGE] = judge_role.id
        self.tournament.roles[self.tournament.SPECTATOR] = spectator_role.id
        self.tournament.roles[self.tournament.PLAYER] = player_role.id
        logger.debug("Register tournament in DB...")
        db.create_tournament(
            self.connection,
            self.tournament.prefix,
            self.guild_id,
            self.category_id,
            self.tournament.to_json(),
        )
        logger.debug("Add roles and create channels...")
        results = await asyncio.gather(
            self.author.add_role(judge_role, reason=self.reason),
            self.bot.rest.add_role_to_member(
                self.guild_id, self.bot.get_me(), judge_role, reason=self.reason
            ),
            self.bot.rest.create_guild_text_channel(
                self.guild_id,
                "Judges",
                category=self.category_id or hikari.UNDEFINED,
                permission_overwrites=[
                    hikari.PermissionOverwrite(
                        id=self.guild_id,
                        type=hikari.PermissionOverwriteType.ROLE,
                        deny=perm.TEXT,
                    ),
                    hikari.PermissionOverwrite(
                        id=judge_role.id,
                        type=hikari.PermissionOverwriteType.ROLE,
                        allow=perm.TEXT,
                    ),
                ],
            ),
            self.bot.rest.create_guild_voice_channel(
                self.guild_id,
                "Judges",
                category=self.category_id or hikari.UNDEFINED,
                permission_overwrites=[
                    hikari.PermissionOverwrite(
                        id=self.guild_id,
                        type=hikari.PermissionOverwriteType.ROLE,
                        deny=perm.VOICE,
                    ),
                    hikari.PermissionOverwrite(
                        id=judge_role.id,
                        type=hikari.PermissionOverwriteType.ROLE,
                        allow=perm.VOICE,
                    ),
                ],
            ),
        )
        self.tournament.channels[tournament.Tournament.JUDGE_TEXT] = results[2].id
        self.tournament.channels[tournament.Tournament.JUDGE_VOICE] = results[3].id
        logger.debug("Update tournament data")
        self.update()
        await set_judge_permissions(self.connection, self.bot, self.guild_id)
        next_step = ConfigureTournament.copy_from_interaction(self)
        await next_step()


class ConfigureTournament(BaseCommand):
    UPDATE = True
    ACCESS = CommandAccess.JUDGE
    DESCRIPTION = "Configure the tournament"
    OPTIONS = []

    async def __call__(self):
        if hasattr(self.interaction, "custom_id"):
            if self.interaction.custom_id == "vekn-required":
                self.tournament.flags ^= tournament.TournamentFlag.VEKN_REQUIRED
            elif self.interaction.custom_id == "decklist-required":
                self.tournament.flags ^= tournament.TournamentFlag.DECKLIST_REQUIRED
            elif self.interaction.custom_id == "checkin-each-round":
                self.tournament.flags ^= tournament.TournamentFlag.CHECKIN_EACH_ROUND
            elif self.interaction.custom_id == "staggered":
                if self.tournament.flags & tournament.TournamentFlag.STAGGERED:
                    self.tournament.rounds = []
                    self.tournament.flags ^= tournament.TournamentFlag.STAGGERED
                else:
                    self.tournament.make_staggered()
        self.update()
        vekn_required = self.tournament.flags & tournament.TournamentFlag.VEKN_REQUIRED
        decklist_required = (
            self.tournament.flags & tournament.TournamentFlag.DECKLIST_REQUIRED
        )
        checkin_each_round = (
            self.tournament.flags & tournament.TournamentFlag.CHECKIN_EACH_ROUND
        )
        staggered = self.tournament.flags & tournament.TournamentFlag.STAGGERED
        if getattr(self.interaction, "custom_id", None) == "validate":
            components = []
            COMPONENTS.pop("vekn-required", None)
            COMPONENTS.pop("decklist-required", None)
            COMPONENTS.pop("checkin-each-round", None)
            COMPONENTS.pop("staggered", None)
            COMPONENTS.pop("validate", None)
        else:
            components = [
                self.bot.rest.build_action_row()
                .add_button(
                    hikari.ButtonStyle.SECONDARY
                    if vekn_required
                    else hikari.ButtonStyle.PRIMARY,
                    "vekn-required",
                )
                .set_label("No VEKN" if vekn_required else "Require VEKN")
                .add_to_container()
                .add_button(
                    hikari.ButtonStyle.SECONDARY
                    if decklist_required
                    else hikari.ButtonStyle.PRIMARY,
                    "decklist-required",
                )
                .set_label("No Decklist" if decklist_required else "Require Decklist")
                .add_to_container()
                .add_button(
                    hikari.ButtonStyle.SECONDARY
                    if checkin_each_round
                    else hikari.ButtonStyle.PRIMARY,
                    "checkin-each-round",
                )
                .set_label(
                    "Checkin once" if checkin_each_round else "Checkin each round"
                )
                .add_to_container()
                .add_button(
                    hikari.ButtonStyle.SECONDARY
                    if staggered
                    else hikari.ButtonStyle.PRIMARY,
                    "staggered",
                )
                .set_label("Normal" if staggered else "Staggered")
                .add_to_container(),
                self.bot.rest.build_action_row()
                .add_button(hikari.ButtonStyle.SUCCESS, "validate")
                .set_label("OK")
                .add_to_container(),
            ]
            COMPONENTS["vekn-required"] = ConfigureTournament
            COMPONENTS["decklist-required"] = ConfigureTournament
            COMPONENTS["checkin-each-round"] = ConfigureTournament
            COMPONENTS["staggered"] = ConfigureTournament
            COMPONENTS["validate"] = ConfigureTournament

        embed = hikari.Embed(
            title=f"Configuration - {self.tournament.name}",
            description=(
                f"- VEKN ID# is {'' if vekn_required else 'not '}required\n"
                f"- Decklist is {'' if decklist_required else 'not '}required\n"
                f"- Check-in {'each round' if checkin_each_round else 'once'}\n"
                + ("- Tournament is staggered\n" if staggered else "")
            ),
        )
        if not components:
            embed.description += (
                "\nRegistrations are now open.\n"
                "Use the `/appoint` command to appoint judges, bots and spectators.\n"
            )
        # different API response when a component is clicked,
        if getattr(self.interaction, "custom_id", None):
            await self.interaction.create_initial_response(
                hikari.ResponseType.MESSAGE_UPDATE,
                embed=embed,
                components=components,
            )
        # when called directly or just after the `open` command
        else:
            await self.create_or_edit_response(
                embed=embed,
                flags=hikari.MessageFlag.EPHEMERAL,
                components=components,
            )


class CloseTournament(BaseCommand):
    UPDATE = True
    ACCESS = CommandAccess.JUDGE
    DESCRIPTION = "Close the tournament"
    OPTIONS = []

    async def __call__(self):
        if not self.tournament:
            raise CommandFailed("No tournament going on here")
        confirmation = (
            self.bot.rest.build_action_row()
            .add_button(hikari.ButtonStyle.DANGER, "confirm-close")
            .set_label("Close tournament")
            .add_to_container()
        )
        COMPONENTS["confirm-close"] = CloseTournament.Confirmed
        await self.create_or_edit_response(
            embed=hikari.Embed(
                title="Are you sure?",
                description="This will definitely close all tournament channels.",
            ),
            components=[confirmation],
            flags=hikari.MessageFlag.EPHEMERAL,
        )

    class Confirmed(BaseComponent):
        UPDATE = True

        async def __call__(self):
            await self.deferred()
            results = await asyncio.gather(
                *(
                    self.bot.rest.delete_channel(channel)
                    for channel in self.tournament.channels.values()
                ),
                return_exceptions=True,
            )
            results.extend(
                await asyncio.gather(
                    *(
                        self.bot.rest.delete_role(self.guild_id, role_id)
                        for role_id in self.tournament.roles.values()
                    ),
                    return_exceptions=True,
                )
            )
            db.close_tournament(self.connection, self.guild_id, self.category_id)
            COMPONENTS.pop("confirm-close", None)
            if any(isinstance(r, (hikari.ClientHTTPResponseError)) for r in results):
                logger.error("Errors closing tournament: %s", results)
                await self.create_or_edit_response(
                    embed=hikari.Embed(
                        title="Cleanup required",
                        description="Some tournament channels or roles have not been "
                        "deleted, make sure you clean up the server appropriately.",
                    ),
                    components=[],
                )
            else:
                await self.create_or_edit_response(
                    embed=hikari.Embed(
                        title="Tournament closed",
                        description="Thanks for using the Archon Bot.",
                    ),
                    components=[],
                )


class Register(BaseCommand):
    UPDATE = True
    ACCESS = CommandAccess.PUBLIC
    DESCRIPTION = "Register for this tournament"
    OPTIONS = [
        hikari.CommandOption(
            type=hikari.OptionType.STRING,
            name="name",
            description="Your name",
            is_required=False,
        ),
        hikari.CommandOption(
            type=hikari.OptionType.STRING,
            name="vekn",
            description="Your VEKN ID#",
            is_required=False,
        ),
        hikari.CommandOption(
            type=hikari.OptionType.STRING,
            name="decklist",
            description="Your decklist (Amaranth or VDB URL)",
            is_required=False,
        ),
        hikari.CommandOption(
            type=hikari.OptionType.USER,
            name="user",
            description="Judge only: user to register",
            is_required=False,
        ),
    ]

    async def __call__(
        self,
        name: Optional[str] = None,
        vekn: Optional[str] = None,
        decklist: Optional[str] = None,
        user: Union[hikari.PartialUser, hikari.User, None] = None,
    ):
        if not self.tournament:
            raise CommandFailed("No tournament in progress")
        await self.deferred(flags=hikari.MessageFlag.EPHEMERAL)
        judge = self._is_judge()
        if user and not judge:
            raise CommandFailed("Only a Judge can register another user")
        deck = None
        if decklist:
            deck = krcg.deck.Deck.from_url(decklist)
        discord_id = user.id if user else self.author.id
        player = await self.tournament.add_player(
            vekn, name, discord=discord_id, deck=deck, judge=judge
        )
        await self.bot.rest.add_role_to_member(
            self.guild_id,
            discord_id,
            self.tournament.roles[self.tournament.PLAYER],
            reason=self.reason,
        )
        self.update()
        if user:
            prefix = "User is"
        else:
            prefix = "You are"
        description = f"{prefix} successfully registered for the tournament."
        if player.playing:
            description = f"{prefix} ready to play."
        elif (
            self.tournament.flags & tournament.TournamentFlag.DECKLIST_REQUIRED
            and not player.deck
        ):
            description += (
                "\nA decklist is required to participate, please use this command "
                "again to provide one before the tournament begins."
            )
        else:
            if user:
                description += (
                    "\nPlease note the user will need to confirm their presence "
                    "by using the `checkin` command before the next round begins."
                )
            else:
                description += (
                    "\nPlease note you will need to confirm your presence by "
                    "using the `checkin` command before the next round begins."
                )
        await self.create_or_edit_response(
            embed=hikari.Embed(
                title="Registered",
                description=description,
            ),
            flags=hikari.MessageFlag.EPHEMERAL,
            components=[],
        )


class CheckIn(Register):
    UPDATE = True
    ACCESS = CommandAccess.PLAYER
    DESCRIPTION = "Check-in to play the next round"
    OPTIONS = []


class OpenCheckIn(BaseCommand):
    UPDATE = True
    ACCESS = CommandAccess.JUDGE
    DESCRIPTION = "Open check-in to players for next round"
    OPTIONS = []

    async def __call__(self):
        self.tournament.open_checkin()
        self.update()
        await self.create_or_edit_response("Check-in is open")


class Drop(BaseCommand):
    UPDATE = True
    ACCESS = CommandAccess.PLAYER
    DESCRIPTION = "Drop from the tournament"
    OPTIONS = [
        hikari.CommandOption(
            type=hikari.OptionType.USER,
            name="user",
            description="Judge only: user to drop",
            is_required=False,
        ),
    ]

    async def __call__(self, user: Union[hikari.PartialUser, hikari.User, None] = None):
        judge = self._is_judge()
        if user and not judge:
            raise CommandFailed("Only a Judge can drop another player")
        self.tournament.drop(user.id if user else self.author.id)
        self.update()
        await self.create_or_edit_response(
            "Dropped",
            flags=hikari.MessageFlag.EPHEMERAL,
        )


class Disqualify(BaseCommand):
    UPDATE = True
    ACCESS = CommandAccess.JUDGE
    DESCRIPTION = "Disqualify a player from the tournament"
    OPTIONS = [
        hikari.CommandOption(
            type=hikari.OptionType.USER,
            name="user",
            description="user to disqualify",
            is_required=True,
        ),
        hikari.CommandOption(
            type=hikari.OptionType.STRING,
            name="note",
            description=(
                "Judge note stating the reason for disqualification "
                "(ignore if a warning was already issued)"
            ),
            is_required=False,
        ),
    ]

    async def __call__(
        self, user: Union[hikari.PartialUser, hikari.User], note: Optional[str] = None
    ):
        self.tournament.drop(user.id, reason=tournament.DropReason.DISQUALIFIED)
        if note:
            self.tournament.note(
                user.id, self.author.id, tournament.NoteLevel.WARNING, note
            )
        self.update()
        await self.create_or_edit_response(
            f"<@!{user.id}> Disqualified",
            user_mentions=[user],
        )


class Appoint(BaseCommand):
    UPDATE = False
    ACCESS = CommandAccess.JUDGE
    DESCRIPTION = "Appoint judges, bots and spectators"
    OPTIONS = [
        hikari.CommandOption(
            type=hikari.OptionType.STRING,
            name="role",
            description="The role to give",
            is_required=True,
            choices=[
                hikari.CommandChoice(name="Judge", value="JUDGE"),
                hikari.CommandChoice(name="Spectator", value="SPECTATOR"),
                hikari.CommandChoice(name="Bot", value="BOT"),
            ],
        ),
        hikari.CommandOption(
            type=hikari.OptionType.USER,
            name="user",
            description="The user to give the tole to",
            is_required=True,
        ),
    ]

    async def __call__(
        self,
        role: str,
        user: hikari.Snowflake = None,
    ):
        await self.deferred(flags=hikari.MessageFlag.EPHEMERAL)
        if role in ["JUDGE", "BOT"]:
            await self.bot.rest.add_role_to_member(
                self.guild_id,
                user,
                self.tournament.roles[self.tournament.JUDGE],
                reason=self.reason,
            )
        else:
            await self.bot.rest.add_role_to_member(
                self.guild_id,
                user,
                self.tournament.roles[self.tournament.SPECTATOR],
                reason=self.reason,
            )
        await self.create_or_edit_response(
            "Appointed",
            flags=hikari.MessageFlag.EPHEMERAL,
        )


class Round(BaseCommand):
    UPDATE = True
    ACCESS = CommandAccess.JUDGE
    DESCRIPTION = "Handle rounds"
    OPTIONS = [
        hikari.CommandOption(
            type=hikari.OptionType.SUB_COMMAND,
            name="start",
            description="Start the next round",
        ),
        hikari.CommandOption(
            type=hikari.OptionType.SUB_COMMAND,
            name="finish",
            description="Finish the current round",
        ),
        hikari.CommandOption(
            type=hikari.OptionType.SUB_COMMAND,
            name="reset",
            description="Reset the current round",
        ),
        hikari.CommandOption(
            type=hikari.OptionType.SUB_COMMAND,
            name="add",
            description="Add a player to the current round",
            options=[
                hikari.CommandOption(
                    type=hikari.OptionType.USER,
                    name="user",
                    description="The user to add to the round",
                    is_required=True,
                ),
                hikari.CommandOption(
                    type=hikari.OptionType.INTEGER,
                    name="table",
                    description="Table number to add the user to",
                    is_required=True,
                    min_value=1,
                ),
            ],
        ),
        hikari.CommandOption(
            type=hikari.OptionType.SUB_COMMAND,
            name="remove",
            description="Remove a player from the current round",
            options=[
                hikari.CommandOption(
                    type=hikari.OptionType.USER,
                    name="user",
                    description="The user to add to the round",
                    is_required=True,
                ),
            ],
        ),
    ]

    async def __call__(self, *args, **kwargs):
        logger.debug("%s | %s", args, kwargs)
        for subcommand, interaction in kwargs.items():
            options = []
            if interaction and interaction.options:
                options = interaction.options
            await getattr(self, subcommand)(
                **{option.name: option.value for option in options}
            )

    async def _progress(self, step, **kwargs):
        chunk = tournament.ITERATIONS // 20
        if step % chunk:
            return
        progress = step // chunk
        await self.create_or_edit_response(
            embed=hikari.Embed(
                title="Seating players...",
                description="▇" * progress + "▁" * (20 - progress),
            )
        )

    async def _display_seating(self, table_num):
        table = self.rounds[-1].seating[table_num - 1]
        channel_id = self.tournament.channels[f"TEXT-{self.prefix}-Table-{table_num}"]
        await self.bot.rest.create_message(
            channel_id,
            embed=hikari.Embed(
                title=f"Table {table_num} seating",
                description="\n".join(
                    f"{j}. {self._player_display(p)}" for j, p in enumerate(table, 1)
                ),
            ),
        )

    async def start(self):
        await self.create_or_edit_response(
            embed=hikari.Embed(
                title="Seating players...",
                description="_" * 20,
            )
        )
        round = await self.tournament.start_round(self._progress)
        await self.create_or_edit_response(
            embed=hikari.Embed(
                title="Assigning tables...",
                description="Table channels are being opened and roles assigned",
            )
        )
        table_roles = await asyncio.gather(
            self.bot.rest.create_role(
                guild=self.guild_id,
                name=f"{self.prefix}-Table-{i+1}",
                mentionable=True,
                resaon=self.reason,
            )
            for i in range(round.seating.tables_count())
        )
        for role in table_roles:
            self.tournament.roles[role.name] = role.id
        player_roles = []
        for role, table in zip(table_roles, round.seating.iter_tables()):
            for number in table:
                if number not in self.tournament.players:
                    continue
                discord_id = self.tournament.players[number].discord
                if not discord_id:
                    continue
                player_roles.append([discord_id, role.id])
        await asyncio.gather(
            self.bot.rest.add_role_to_member(guild=self.guild_id, user=user, role=role)
            for user, role in player_roles
        )
        judge_role = self.tournament.roles[self.tournament.JUDGE]
        spectator_role = self.tournament.roles[self.tournament.SPECTATOR]
        channels = []
        channels.extend(
            self.bot.rest.create_guild_text_channel(
                guild=self.guild_id,
                name=f"{self.prefix}-Table-{i+1}",
                permission_overwrites=[
                    hikari.PermissionOverwrite(
                        id=self.guild_id,
                        type=hikari.PermissionOverwriteType.ROLE,
                        deny=perm.TEXT,
                    ),
                    hikari.PermissionOverwrite(
                        id=role.id,
                        type=hikari.PermissionOverwriteType.ROLE,
                        allow=perm.TEXT,
                    ),
                    hikari.PermissionOverwrite(
                        id=judge_role.id,
                        type=hikari.PermissionOverwriteType.ROLE,
                        allow=perm.TEXT,
                    ),
                    hikari.PermissionOverwrite(
                        id=spectator_role.id,
                        type=hikari.PermissionOverwriteType.ROLE,
                        allow=perm.SPECTATE_TEXT,
                    ),
                ],
            )
            for i, role in enumerate(table_roles, 1)
        )
        channels.extend(
            self.bot.rest.create_guild_voice_channel(
                guild=self.guild_id,
                name=f"{self.prefix}-Table-{i+1}",
                permission_overwrites=[
                    hikari.PermissionOverwrite(
                        id=self.guild_id,
                        type=hikari.PermissionOverwriteType.ROLE,
                        deny=perm.VOICE,
                    ),
                    hikari.PermissionOverwrite(
                        id=role.id,
                        type=hikari.PermissionOverwriteType.ROLE,
                        allow=perm.VOICE,
                    ),
                    hikari.PermissionOverwrite(
                        id=judge_role.id,
                        type=hikari.PermissionOverwriteType.ROLE,
                        allow=perm.JUDGE_VOICE,
                    ),
                    hikari.PermissionOverwrite(
                        id=spectator_role.id,
                        type=hikari.PermissionOverwriteType.ROLE,
                        allow=perm.SPECTATE_VOICE,
                    ),
                ],
            )
            for i, role in enumerate(table_roles, 1)
        )
        channels = await asyncio.gather(*channels)
        for channel in channels:
            if isinstance(channel, hikari.GuildTextChannel):
                self.tournament.channels[f"TEXT-{channel.name}"] = channel.id
            elif isinstance(channel, hikari.GuildVoiceChannel):
                self.tournament.channels[f"VOICE-{channel.name}"] = channel.id
        await asyncio.gather(
            self._display_seating(i + 1) for i in range(round.seating.tables_count())
        )
        self.update()
        await self.create_or_edit_response(
            embeds=_paginate_embed(
                hikari.Embed(
                    title=f"Round {self.tournament.current_round} Seating",
                    fields=[
                        hikari.EmbedField(
                            name=f"Table {i}",
                            value="\n".join(
                                f"{j}. {self._player_display(vekn)}"
                                for j, vekn in enumerate(table, 1)
                            ),
                            _inline=True,
                        )
                        for i, table in enumerate(round.seating.iter_tables(), 1)
                    ],
                )
            ),
            user_mentions=True,
        )

    async def finish(self):
        self.tournament.finish_round()
        self.update()
        await self.create_or_edit_response("Round finished")

    async def reset(self):
        self.tournament.reset_round()
        self.update()
        await self.create_or_edit_response("Round reset")

    async def add(self, user: Union[hikari.PartialUser, hikari.User], table: int):
        await self.deferred()
        self.tournament.round_add(user, table)
        await self._display_seating(table)
        self.update()
        await self.create_or_edit_response(f"Player added to table {table}")

    async def remove(self, user: Union[hikari.PartialUser, hikari.User]):
        await self.deferred()
        table = self.tournament.round_remove(user)
        await self._display_seating(table)
        self.update()
        await self.create_or_edit_response(f"Player removed from table {table}")


class Finals(BaseCommand):
    UPDATE = True
    ACCESS = CommandAccess.JUDGE
    DESCRIPTION = "Start the finals"
    OPTIONS = []

    async def __call__(self):
        round = self.tournament.start_finals()
        table = self.rounds[-1].seating
        await self.create_or_edit_response(
            embed=hikari.Embed(
                title="Creating channels...",
                description="Finals channels are being opened and roles assigned",
            )
        )
        self.update()
        table_role = await self.bot.rest.create_role(
            guild=self.guild_id,
            name=f"{self.prefix}-Finals",
            mentionable=True,
            resaon=self.reason,
        )
        self.tournament.roles[table_role.name] = table_role.id
        finalists = []
        for number in round.seating[0]:
            if number not in self.tournament.players:
                continue
            discord_id = self.tournament.players[number].discord
            if not discord_id:
                continue
            finalists.append(discord_id)
        await asyncio.gather(
            self.bot.rest.add_role_to_member(
                guild=self.guild_id, user=discord_id, role=table_role
            )
            for discord_id in finalists
        )
        judge_role = self.tournament.roles[self.tournament.JUDGE]
        spectator_role = self.tournament.roles[self.tournament.SPECTATOR]
        channels = [
            self.bot.rest.create_guild_text_channel(
                guild=self.guild_id,
                name=f"{self.prefix}-Finals",
                permission_overwrites=[
                    hikari.PermissionOverwrite(
                        id=self.guild_id,
                        type=hikari.PermissionOverwriteType.ROLE,
                        deny=perm.TEXT,
                    ),
                    hikari.PermissionOverwrite(
                        id=table_role.id,
                        type=hikari.PermissionOverwriteType.ROLE,
                        allow=perm.TEXT,
                    ),
                    hikari.PermissionOverwrite(
                        id=judge_role.id,
                        type=hikari.PermissionOverwriteType.ROLE,
                        allow=perm.TEXT,
                    ),
                    hikari.PermissionOverwrite(
                        id=spectator_role.id,
                        type=hikari.PermissionOverwriteType.ROLE,
                        allow=perm.SPECTATE_TEXT,
                    ),
                ],
            ),
            self.bot.rest.create_guild_voice_channel(
                guild=self.guild_id,
                name=f"{self.prefix}-Finals",
                permission_overwrites=[
                    hikari.PermissionOverwrite(
                        id=self.guild_id,
                        type=hikari.PermissionOverwriteType.ROLE,
                        deny=perm.VOICE,
                    ),
                    hikari.PermissionOverwrite(
                        id=table_role.id,
                        type=hikari.PermissionOverwriteType.ROLE,
                        allow=perm.VOICE,
                    ),
                    hikari.PermissionOverwrite(
                        id=judge_role.id,
                        type=hikari.PermissionOverwriteType.ROLE,
                        allow=perm.JUDGE_VOICE,
                    ),
                    hikari.PermissionOverwrite(
                        id=spectator_role.id,
                        type=hikari.PermissionOverwriteType.ROLE,
                        allow=perm.SPECTATE_VOICE,
                    ),
                ],
            ),
        ]
        channels = await asyncio.gather(*channels)
        for channel in channels:
            if isinstance(channel, hikari.GuildTextChannel):
                self.tournament.channels[f"TEXT-{channel.name}"] = channels[0].id
            elif isinstance(channel, hikari.GuildVoiceChannel):
                self.tournament.channels[f"VOICE-{channel.name}"] = channels[1].id
        seeding_embed = hikari.Embed(
            title="Finals seeding",
            description="\n".join(
                f"{j}. {self._player_display(p)}" for j, p in enumerate(table, 1)
            ),
        )
        await self.bot.rest.create_message(
            channels[0].id,
            embed=seeding_embed,
        )
        self.update()
        await self.create_or_edit_response(
            embed=seeding_embed,
            user_mentions=True,
        )


class Report(BaseCommand):
    UPDATE = True
    ACCESS = CommandAccess.PLAYER
    DESCRIPTION = "Report the number of VPs you got in the round"
    OPTIONS = [
        hikari.CommandOption(
            type=hikari.OptionType.FLOAT,
            name="vp",
            description="Number of VPs won",
            is_required=True,
            min_value=0,
            max_value=5,
        ),
    ]

    async def __call__(self, vp: float):
        self.tournament.report(self.author.id, vp)
        self.update()
        await self.create_or_edit_response(
            content="Result registered", flags=hikari.MessageFlag.EPHEMERAL
        )


class FixReport(BaseCommand):
    UPDATE = True
    ACCESS = CommandAccess.JUDGE
    DESCRIPTION = "Fix a VP score"
    OPTIONS = [
        hikari.CommandOption(
            type=hikari.OptionType.USER,
            name="user",
            description="User whose result should be changed",
            is_required=True,
        ),
        hikari.CommandOption(
            type=hikari.OptionType.FLOAT,
            name="vp",
            description="Number of VPs won",
            is_required=True,
            min_value=0,
            max_value=5,
        ),
        hikari.CommandOption(
            type=hikari.OptionType.INTEGER,
            name="round",
            description=(
                "Round for which to change the result (defaults to current round)"
            ),
            is_required=False,
            min_value=1,
        ),
    ]

    async def __call__(
        self,
        user: Union[hikari.PartialUser, hikari.User],
        vp: float,
        round: Optional[int] = None,
    ):
        self.tournament.report(user.id, vp, round)
        self.update()
        await self.create_or_edit_response(
            content="Result registered", flags=hikari.MessageFlag.EPHEMERAL
        )


class ValidateScore(BaseCommand):
    UPDATE = True
    ACCESS = CommandAccess.JUDGE
    DESCRIPTION = "Validate an odd VP situation"
    OPTIONS = [
        hikari.CommandOption(
            type=hikari.OptionType.INTEGER,
            name="table",
            description=("Table for which to validate the score"),
            is_required=True,
            min_value=1,
        ),
        hikari.CommandOption(
            type=hikari.OptionType.STRING,
            name="note",
            description=("The reason for the odd VP situation"),
            is_required=True,
        ),
        hikari.CommandOption(
            type=hikari.OptionType.INTEGER,
            name="round",
            description=(
                "Round for which to change the result (defaults to current round)"
            ),
            is_required=False,
            min_value=1,
        ),
    ]

    async def __call__(self, table: int, note: str, round: Optional[int] = None):
        self.tournament.validate_score(table, self.author.id, note, round)
        self.update()
        await self.create_or_edit_response(
            content=f"Score validated for table {table}",
            flags=hikari.MessageFlag.EPHEMERAL,
        )


def note_level_int(level) -> int:
    return list(tournament.NoteLevel.__members__.values()).index(level)


def note_level_str(level) -> str:
    return {
        tournament.NoteLevel.OVERRIDE: "Override",
        tournament.NoteLevel.NOTE: "Note",
        tournament.NoteLevel.CAUTION: "Caution",
        tournament.NoteLevel.WARNING: "Warning",
    }[level]


class Note(BaseCommand):
    """Allow a Judge to take a note on or deliver a caution or warning to a player.

    If previous notes have been taken on this player,
    ask the judge to review them and potentially adapt their note level
    (upgrade to caution, warning or disqualification).
    """

    UPDATE = True
    ACCESS = CommandAccess.JUDGE
    DESCRIPTION = "Take a note on a player, or deliver a caution or warning"
    OPTIONS = [
        hikari.CommandOption(
            type=hikari.OptionType.USER,
            name="user",
            description="User whose result should be changed",
            is_required=True,
        ),
        hikari.CommandOption(
            type=hikari.OptionType.STRING,
            name="level",
            description="Level of the remark",
            is_required=True,
            choices=[
                hikari.CommandChoice(name="Note", value=tournament.NoteLevel.NOTE),
                hikari.CommandChoice(
                    name="Caution", value=tournament.NoteLevel.CAUTION
                ),
                hikari.CommandChoice(
                    name="Warning", value=tournament.NoteLevel.WARNING
                ),
            ],
        ),
        hikari.CommandOption(
            type=hikari.OptionType.STRING,
            name="note",
            description="The comment",
            is_required=True,
        ),
    ]

    async def __call__(
        self,
        user: Union[hikari.PartialUser, hikari.User],
        level: tournament.NoteLevel,
        note: str,
    ):
        vekn = self._check_player_id(user.id)
        previous = None
        if vekn in self.tournament.notes:
            notes = sorted(
                self.tournament.notes[vekn], key=lambda n: -note_level_int(n.level)
            )
            for previous_level, notes in itertools.groupby(
                notes, key=lambda n: -note_level_int(n.level)
            ):
                if note_level_int(previous_level) < note_level_int(level):
                    break
                previous = previous_level, notes
                break
        if not previous:
            apply = Note.ApplyNote.copy_from_interaction(
                self,
                user,
                note,
                level,
                False,
            )
            await apply()
            return

        if previous[0] == tournament.NoteLevel.WARNING:
            upgrade_component = (
                "note-upgrade",
                "Disqualification",
                functools.partial(Note.ApplyNote, user, note, Note.Warning, True),
            )
        elif previous[0] == tournament.NoteLevel.CAUTION:
            upgrade_component = (
                "note-upgrade",
                "Warning",
                functools.partial(Note.ApplyNote, user, note, Note.Warning, False),
            )
        elif previous[0] == tournament.NoteLevel.NOTE:
            upgrade_component = (
                "note-upgrade",
                "Caution",
                functools.partial(Note.ApplyNote, user, note, Note.Caution, False),
            )

        confirmation = (
            self.bot.rest.build_action_row()
            .add_button(hikari.ButtonStyle.DANGER, f"note-upgrade-{self.author.id}")
            .set_label(f"Upgrade to {upgrade_component[1]}")
            .add_to_container()
            .add_button(hikari.ButtonStyle.PRIMARY, f"note-continue-{self.author.id}")
            .set_label("Continue")
            .add_to_container()
            .add_button(hikari.ButtonStyle.SECONDARY, f"note-cancel-{self.author.id}")
            .set_label("Cancel")
        )
        COMPONENTS[f"note-upgrade-{self.author.id}"] = upgrade_component[2]
        COMPONENTS[f"note-continue-{self.author.id}"] = functools.partial(
            Note.ApplyNote, user, note, level, False
        )
        COMPONENTS["note-cancel"] = Note.Cancel
        await self.create_or_edit_response(
            embed=hikari.Embed(
                title="Review note level",
                description=(
                    "There are already some notes for this player, "
                    "you might want to upgrade your note level."
                ),
                fields=hikari.EmbedField(
                    name=previous[0],
                    value="\n".join(f"- <@!{p.judge}> {p.text}" for p in previous[1]),
                ),
            ),
            components=[confirmation],
            flags=hikari.MessageFlag.EPHEMERAL,
        )

    class ApplyNote(BaseComponent):
        UPDATE = True

        async def __init__(
            self,
            user: Union[hikari.PartialUser, hikari.User],
            note: str,
            level: tournament.NoteLevel,
            disqualify: bool,
            *args,
            **kwargs,
        ):
            self.user = user
            self.note = note
            self.level = level
            self.disqualify = disqualify
            super().__init__(*args, **kwargs)

        async def __call__(self):
            self.tournament.note(self.user.id, self.author.id, self.level, self.note)
            if self.disqualify:
                self.tournament.drop(self.user.id, tournament.DropReason.DISQUALIFIED)
            self.update()
            if self.level == tournament.NoteLevel.NOTE:
                await self.create_or_edit_response(
                    "Note taken",
                    flags=hikari.MessageFlag.EPHEMERAL,
                )
            else:
                await self.create_or_edit_response(
                    embed=hikari.Embed(title=self.level, description=self.note)
                )

    class Cancel(BaseComponent):
        UPDATE = False

        async def __call__(self):
            return self.create_or_edit_response(
                "Cancelled", flags=hikari.MessageFlag.EPHEMERAL
            )


class Announce(BaseCommand):
    UPDATE = False
    ACCESS = CommandAccess.JUDGE
    DESCRIPTION = "Make the standard announcement (depends on the tournament state)"
    OPTIONS = []

    async def __call__(self):
        current_round = self.tournament.current_round
        if self.tournament.state == tournament.TournamentState.PLAYING:
            current_round += 1
        if self.tournament.rounds and self.tournament.rounds[-1].finals:
            current_round = "Finals"
        else:
            current_round = f"Round {current_round}"
        if self.tournament.state == tournament.TournamentState.REGISTRATION:
            embed = hikari.Embed(
                title=f"{self.tournament.name} — Registrations open",
                description=(
                    f"{self.tournament.players.count} players registered\n"
                    "**Use the `/register` command to register.**"
                ),
            )
            if self.tournament.flags & tournament.TournamentFlag.VEKN_REQUIRED:
                embed.add_field(
                    name="VEKN ID# required",
                    value=(
                        "A VEKN ID is required to register to this tournament. "
                        "You can find yours on the "
                        "[VEKN website](https://www.vekn.net/player-registry). "
                        "If you do not have one, ask the Judges or your Prince."
                    ),
                )
            if self.tournament.flags & tournament.TournamentFlag.DECKLIST_REQUIRED:
                embed.add_field(
                    name="Decklist required",
                    value=(
                        "A decklist is required for this tournament. "
                        "You can register without one, but you need to provide "
                        "it before the first round. "
                        "Use the `/register` command again to add your decklist."
                    ),
                )
            if self.tournament.flags & tournament.TournamentFlag.CHECKIN_EACH_ROUND:
                checkin_time = "each round"
            else:
                checkin_time = "the first round"
            embed.add_field(
                name="Check-in",
                value=(
                    "Once registered, you will need to check in before "
                    f"{checkin_time} using the `/checkin` command"
                ),
            )
            await self.create_or_edit_response(embed=embed)

        elif self.tournament.state == tournament.TournamentState.CHECKIN:
            players_role = self.tournament.roles[self.tournament.PLAYER]
            embed = hikari.Embed(
                title=(f"{self.tournament.name} — CHECK-IN — {current_round}"),
                description=(
                    f"<@&{players_role}> Please confirm your participation "
                    "using the `/checkin` command. Check-in is required to play.\n"
                    "\nYou can use the `/status` command to verify your status."
                ),
                role_mentions=[players_role],
            )
            if (
                self.tournament.current_round == 0
                or self.tournament.flags & tournament.TournamentFlag.LEAGUE
            ):
                embed.add_field(
                    name="Registration",
                    value=(
                        "If you are not registered yet, you can still do so "
                        "by using the `/register` command. You will be checked "
                        "in automatically."
                    ),
                )
            await self.create_or_edit_response(embed=embed)

        elif self.tournament.state == tournament.TournamentState.WAITING:
            embed = hikari.Embed(
                title=(f"{self.tournament.name} — {current_round} finished"),
                description=(
                    "Waiting for next round to begin"
                    "using the `/checkin` command.\n"
                    "You can use the `/status` command to verify your status."
                ),
            )
            if self.tournament.flags & tournament.TournamentFlag.CHECKIN_EACH_ROUND:
                embed.add_field(
                    name="Check-in required",
                    value=(
                        "Players will need to check in again to participate to the "
                        "next round."
                    ),
                )
                embed.add_field(
                    name="Judges",
                    value=(
                        "A Judge needs to use the `/open-check-in` command to open "
                        "the check-in for next round."
                    ),
                )
            await self.create_or_edit_response(embed=embed)

        elif self.tournament.state == tournament.TournamentState.PLAYING:
            embed = hikari.Embed(
                title=(f"{self.tournament.name} — {current_round} in progress"),
                description=("Join your assigned table channels and enjoy your game."),
            )
            embed.add_field(
                name="Report your results",
                value=(
                    "Use the `/report` command to report "
                    "your Victory Points.\n"
                    "No need to report scores of zero."
                ),
            )
            embed.add_field(
                name="Judges",
                value=(
                    "Judges can use the `/result` command to see "
                    "the results.\n"
                    "They can use the `/fix` command to correct them "
                    "and the `/override` command to confirm "
                    "an odd VP situation.\n"
                    "When all is good, they can use the `/round finish` "
                    "command.\n"
                ),
            )
            await self.create_or_edit_response(embed=embed)


class Status(BaseCommand):
    UPDATE = False
    ACCESS = CommandAccess.PUBLIC
    DESCRIPTION = "Check your current status"
    OPTIONS = []

    async def __call__(self):
        if not self.tournament:
            raise CommandFailed("No tournament in progress")
        embed = hikari.Embed(
            title=f"{self.tournament.name} – {self.tournament.players.count} players"
        )
        if self.author.id not in self.tournament.players:
            if self.tournament.rounds and not (
                self.tournament.flags & tournament.TournamentFlag.LEAGUE
            ):
                embed.description = "Tournament in progress. You're not participating."
            elif self.tournament.state == tournament.TournamentState.WAITING:
                embed.description = "Waiting for registrations to open."
            else:
                embed.description = (
                    f"{self.tournament.players.count} players registered.\n"
                    "Register using the `/register` command."
                )
                if self.tournament.flags & tournament.TournamentFlag.VEKN_REQUIRED:
                    embed.description += (
                        "\nThis tournament requires a VEKN ID#. "
                        "If you do not have one, ask a Judge to help "
                        "with your registration."
                    )
        else:

            info = self.tournament.player_info(self.author.id)
            embed.description = ""
            if info.drop and info.drop == tournament.DropReason.DROP:
                embed.description = "**DROPPED**\n"
            elif info.drop and info.drop == tournament.DropReason.DISQUALIFIED:
                embed.description = "**DISQUALIFIED**\n"
            penalties = [
                note
                for note in info.notes
                if note.level
                in [tournament.NoteLevel.CAUTION, tournament.NoteLevel.WARNING]
            ]
            if penalties:
                embed.add_field(
                    name="Penalties",
                    value="\n".join(
                        f"- **{note_level_str(note.level)}:** {note.text}"
                        for note in penalties
                    ),
                )
            if info.status == tournament.PlayerStatus.PLAYING:
                embed.description = "You are ready to play."
            elif info.status == tournament.PlayerStatus.CHECKIN:
                embed.description = (
                    "Use the `checkin` command to check in for the next round."
                )
            elif info.status == tournament.PlayerStatus.MISSING_DECK:
                embed.description = (
                    "Please use the `register` command to provide your decklist."
                )
            elif info.status == tournament.PlayerStatus.WAITING:
                embed.description = "You are registered. Waiting for check-in to open."
            elif info.status == tournament.PlayerStatus.CHECKED_OUT:
                embed.description = "You are not checked in. Check-in is closed, sorry."
            else:
                raise RuntimeError("Unexpected tournament state")
            if self.tournament.rounds:
                embed.description = (
                    f"**You played {info.rounds} rounds {info.score}**\n"
                    + embed.description
                )
        await self.create_or_edit_response(
            embed=embed, flags=hikari.MessageFlag.EPHEMERAL
        )


class Standings(BaseCommand):
    UPDATE = False
    ACCESS = CommandAccess.JUDGE
    DESCRIPTION = "Display current standings"
    OPTIONS = [
        hikari.CommandOption(
            type=hikari.OptionType.BOOLEAN,
            name="public",
            description="Display the standings publicly (default is private)",
            is_required=False,
        ),
    ]

    async def __call__(self, public: bool = False):
        winner, ranking = self.tournament.standings()
        embed = hikari.Embed(
            title="Standings",
            description="\n".join(
                ("*WINNER* " if winner == vekn else f"{rank}. ")
                + f"{self._player_display(vekn)} {score}"
                for rank, vekn, score in ranking
            ),
        )
        await self.create_or_edit_response(
            embeds=_paginate_embed(embed),
            flags=(
                hikari.UNDEFINED
                if public or self._is_judge_channel()
                else hikari.MessageFlag.EPHEMERAL
            ),
        )


class PlayerInfo(BaseCommand):
    UPDATE = False
    ACCESS = CommandAccess.JUDGE
    DESCRIPTION = "Displayer a player's info (private)"
    OPTIONS = [
        hikari.CommandOption(
            type=hikari.OptionType.STRING,
            name="vekn",
            description="Player VEKN ID#",
            is_required=False,
        ),
        hikari.CommandOption(
            type=hikari.OptionType.USER,
            name="user",
            description="Player",
            is_required=False,
        ),
    ]

    async def __call__(
        self,
        vekn: Optional[str] = None,
        user: Optional[Union[hikari.PartialUser, hikari.User]] = None,
    ):
        player, rounds, score, drop, notes = self.tournament.player_info(
            vekn or user.id
        )
        fields = []
        notes = sorted(notes, key=lambda n: note_level_int(n.level))
        for level, level_notes in itertools.groupby(
            notes, key=lambda n: note_level_int(n.level)
        ):
            fields.append(
                hikari.EmbedField(
                    name=note_level_str(level),
                    value="\n".join(f"- <@!{n.judge}> {n.text}" for n in level_notes),
                )
            )
        description = self._player_display(player.vekn)
        description += f"\n{rounds} rounds played {score}"
        if drop and drop == tournament.DropReason.DROP:
            description += "\n**DROPPED**"
        elif drop and drop == tournament.DropReason.DISQUALIFIED:
            description += "\n**DISQUALIFIED**"
        embed = hikari.EmbedField(
            title="Player Info",
            description=description,
            fields=fields,
        )
        await self.create_or_edit_response(
            embeds=_paginate_embed(embed),
            flags=(
                hikari.UNDEFINED
                if self._is_judge_channel()
                else hikari.MessageFlag.EPHEMERAL
            ),
        )


class Results(BaseCommand):
    UPDATE = False
    ACCESS = CommandAccess.JUDGE
    DESCRIPTION = "Display current round results"
    OPTIONS = [
        hikari.CommandOption(
            type=hikari.OptionType.INTEGER,
            name="round",
            description=(
                "Round for which to see the result (defaults to current round)"
            ),
            is_required=False,
            min_value=1,
        ),
        hikari.CommandOption(
            type=hikari.OptionType.BOOLEAN,
            name="public",
            description="Display the results publicly (default is private)",
            is_required=False,
        ),
    ]

    async def __call__(
        self, round: Optional[int] = None, public: Optional[bool] = False
    ):
        round_number = round or self.tournament.current_round
        try:
            round = self.tournament.rounds[round_number - 1]
        except IndexError:
            raise CommandFailed(f"Round {round_number} has not been played")
        if public or self._is_judge_channel():
            flag = hikari.UNDEFINED
        else:
            flag = hikari.MessageFlag.EPHEMERAL
        await self.deferred(flag)
        embed = hikari.Embed(
            title="Finals" if round.finals else f"Round {round_number}"
        )
        for i, table in enumerate(round.seating.iter_tables(), 1):
            scores = []
            for j, player_number in enumerate(table, 1):
                vekn = self.tournament._check_player_id(player_number)
                score = round.results.get(vekn, None) or tournament.Score()
                scores.append(f"{j}. {self._player_display(vekn)} {score}")
            embed.add_field(name=f"Table {i}", value="\n".join(scores), inline=True)
        embeds = _paginate_embed(embed)
        await self.create_or_edit_response(embeds=embeds, flags=flag)


class PlayersList(BaseCommand):
    UPDATE = False
    ACCESS = CommandAccess.JUDGE
    DESCRIPTION = "Display the list of players"
    OPTIONS = [
        hikari.CommandOption(
            type=hikari.OptionType.BOOLEAN,
            name="public",
            description="Display the list publicly (default is private)",
            is_required=False,
        ),
    ]

    async def __call__(self, public: bool = False):
        if public or self._is_judge_channel():
            flag = hikari.UNDEFINED
        else:
            flag = hikari.MessageFlag.EPHEMERAL
        players = sorted(self.tournament.players.iter_players(), key=lambda p: p.number)
        embed = hikari.Embed(title=f"Players ({self.tournament.players.count})")
        checkin_info = self.tournament.state == tournament.TournamentState.CHECKIN
        player_lines = []
        for p in players:
            player_lines.append(f"- {self._player_display(p.vekn)}")
            if checkin_info:
                if p.playing:
                    player_lines[-1] += " ✅"
                else:
                    player_lines[-1] += " ❌"
        embed.description = "\n".join(player_lines)
        embeds = _paginate_embed(embed)
        await self.create_or_edit_response(embeds=embeds, flags=flag)


# class Help(Command):
#     async def __call__(self, *args):
#         embed = hikari.Embed(title="Archon help", description="")
#         try:
#             self._check_judge_private()
#             embed.description += """**Judge commands**
# - `archon appoint [@user] (...[@user])`: appoint users as judges
# - `archon spectator [@user] (...[@user])`: appoint users as spectators
# - `archon register [ID#] [Name]`: register a user (Use `-` for auto ID)
# - `archon checkin [ID#] [@user] ([name])`: check user in, register him (requires name)
# - `archon players`: display the list of players
# - `archon checkin-start`: open check-in
# - `archon checkin-stop`: stop check-in
# - `archon checkin-reset`: reset check-in
# - `archon checkin-all`: check-in all registered players
# - `archon staggered [rounds#]`: run a staggered tournament (6, 7, or 11 players)
# - `archon rounds-limit [#rounds]: limit the number of rounds per player`
# - `archon round-start`: seat the next round
# - `archon round-reset`: rollback the round seating
# - `archon round-finish`: stop reporting and close the current round
# - `archon round-add [@player | ID#]`: add a player (on a 4 players table)
# - `archon round-remove [@player | ID#]`: remove a player (from a 5 players table)
# - `archon results`: check current round results
# - `archon standings`: display current standings
# - `archon finals`: start the finals
# - `archon caution [@player | ID#] [Reason]`: issue a caution to a player
# - `archon warn [@player | ID#] [Reason]`: issue a warning to a player
# - `archon disqualify [@player | ID#] [Reason]`: disqualify a player
# - `archon close`: close current tournament

# **Judge private commands**
# - `archon upload`: upload the list of registered players (attach CSV file)
# - `archon players`: display the list of players and their current score
# - `archon player [@player | ID#]`: display player information, cautions and warnings
# - `archon registrations`: display the list of registrations
# - `archon fix [@player | ID#] [VP#] {Round}`: fix a VP report (default: current round)
# - `archon fix-table [Table] [ID#] (...[ID#])`: reassign table (list players in order)
# - `archon validate [Round] [Table] [Reason]`: validate an odd VP situation
# """
#         except CommandFailed:
#             if self.tournament:
#                 embed.description += """**Player commands**
# - `archon help`: display this help message
# - `archon status`: current tournament status
# - `archon register [ID#] [Name]`: register a VEKN ID# for the tournament
# - `archon checkin [ID#]`: check in for the round (with VEKN ID# if required)
# - `archon report [VP#]`: report your score for the round
# - `archon drop`: drop from the tournament
# """
#             else:
#                 embed.description += (
#                     "`archon open [name]`: start a new tournament or league"
#                 )
#             judge_channel = self.guild.get_channel(
#                 self.tournament.channels.get(self.tournament.JUDGES_TEXT)
#             )
#             if judge_channel and self._from_judge():
#                 embed.set_footer(
#                     text=f'Use "archon help" in the {judge_channel.mention} channel '
#                     "to list judges commands."
#                 )
#         await self.send_embed(embed)

# class Appoint(Command):
#     async def __call__(self, *args):
#         self._check_judge()
#         judge_role = self.judge_role
#         await asyncio.gather(
#             *(
#                 member.add_roles(judge_role, reason=self.reason)
#                 for member in self._get_mentioned_members()
#             )
#         )
#         await self.send("Judge(s) appointed")


# class Spectator(Command):
#     async def __call__(self, *args):
#         self._check_judge()
#         spectator_role = self.guild.get_role(self.tournament.spectator_role)
#         await asyncio.gather(
#             *(
#                 member.add_roles(spectator_role, reason=self.reason)
#                 for member in self._get_mentioned_members()
#             )
#         )
#         await self.send("Spectator(s) appointed")


# class Register(Command):
#     UPDATE = True

#     async def __call__(self, vekn=None, *name_args):
#         judge_role = self.judge_role
#         judge = self._from_judge()
#         # self._check_judge()
#         vekn = vekn.strip("#").strip("-")
#         name = " ".join(name_args)
#         if vekn:
#             await self._check_vekn(vekn)
#         elif not judge:
#             raise CommandFailed(
#                 f"Only a {judge_role.mention} " "can register a user with no VEKN ID"
#             )
#         vekn = self._register_player(vekn, name)
#         self.update()
#         await self.send(f"{name} registered with ID# {vekn}")

#     async def _check_vekn(self, vekn):
#         async with aiohttp.ClientSession() as session:
#             async with session.post(
#                 "https://www.vekn.net/api/vekn/login",
#                 data={"username": VEKN_LOGIN, "password": VEKN_PASSWORD},
#             ) as response:
#                 result = await response.json()
#                 try:
#                     token = result["data"]["auth"]
#                 except:  # noqa: E722
#                     token = None
#             if not token:
#                 raise CommandFailed("Unable to authentify to VEKN")

#             async with session.get(
#                 f"https://www.vekn.net/api/vekn/registry?filter={vekn}",
#                 headers={"Authorization": f"Bearer {token}"},
#             ) as response:
#                 result = await response.json()
#                 result = result["data"]
#                 if isinstance(result, str):
#                     raise CommandFailed(f"VEKN returned an error: {result}")
#                 result = result["players"]
#                 if len(result) > 1:
#                     raise CommandFailed("Incomplete VEKN ID#")
#                 if len(result) < 1:
#                     raise CommandFailed("VEKN ID# not found")
#                 result = result[0]
#                 if result["veknid"] != str(vekn):
#                     raise CommandFailed("VEKN ID# not found")
#             # TODO: Not checking names for now. Future versions might
#             # vekn_name = result["firstname"] + " " + result["lastname"]


# class Status(Command):
#     async def __call__(self):
#         self._check_tournament()
#         message = f"**{self.tournament.name}**"
#         if self.tournament.registered:
#             message += f"\n{len(self.tournament.registered)} players registered"
#         if self.tournament.players:
#             message += f"\n{len(self.round_players)} players checked in"
#         if self.tournament.current_round:
#             if self.tournament.finals_seeding:
#                 if len(self.tournament.results) == self.tournament.current_round:
#                     try:
#                         self._compute_scores()
#                         message += (
#                             f"\n{self._player_display(self.winner)} is the winner!"
#                         )
#                     except CommandFailed:
#                         message += "\nFinals in progress"
#                 else:
#                     message += "\nFinals in progress"
#             else:
#                 message += f"\nRound {self.tournament.current_round} in progress"
#         await self.channel.send(message)


# class Upload(Command):
#     UPDATE = True

#     async def __call__(self, *args):
#         self._check_judge_private()
#         data = await self.message.attachments[0].read()
#         data = io.StringIO(data.decode("utf-8"))
#         data.seek(0)
#         try:
#             data = [
#                 (i, line[0].strip("#"), line[1])
#                 for i, line in enumerate(csv.reader(data), 1)
#             ]
#         except IndexError:
#             data.seek(0)
#             data = [
#                 (i, line[0].strip("#"), line[1])
#                 for i, line in enumerate(csv.reader(data, delimiter=";"), 1)
#             ]
#         data = [(line, vekn, name) for line, vekn, name in data if vekn]
#         issues = collections.defaultdict(list)
#         for line, vekn, _ in data:
#             issues[vekn].append(line)
#         issues = {k: v for k, v in issues.items() if len(v) > 1}
#         if issues:
#             await self.send(
#                 "Some VEKN numbers are duplicated:\n"
#               + "\n".join(f"{vekn}: lines {lines}" for vekn, lines in issues.items())
#             )
#             return
#         results = {vekn: name for _line, vekn, name in data}
#         self.tournament.registered = results
#         self.update()
#         await self.send(f"{len(self.tournament.registered)} players registered")


# class RoundsLimit(Command):
#     UPDATE = True

#     async def __call__(self, rounds_limit: int):
#         self._check_judge()
#         self.tournament.rounds_limit = rounds_limit
#         self.update()
#         await self.send("Rounds limited to {rounds_limit} rounds per player.")


# class Checkin(Command):
#     UPDATE = True

#     async def __call__(self, vekn=None, mention=None, *name_args):
#         self._check_tournament()
#         vekn = (vekn or "").strip("#")
#         judge_role = self.judge_role
#         judge = self._from_judge()
#         if mention:
#             if not judge:
#                 await self.send(
#                     f"Unexpected name: only a {judge_role.mention} "
#                     "can check in another user"
#                 )
#             if len(self.message.mentions) > 1:
#                 raise CommandFailed("You must mention a single player")
#             member = self.message.mentions[0] if self.message.mentions else None
#         else:
#             member = self.author
#         if not judge and not self.tournament.checkin:
#             raise CommandFailed(
#                 "Check-in is closed. Use `archon checkin-start` to open it"
#             )
#         id_to_vekn = {v: k for k, v in self.tournament.players.items()}
#         if member and member.id in id_to_vekn:
#             previous_vekn = id_to_vekn[member.id]
#             del self.tournament.players[previous_vekn]
#             vekn = vekn or previous_vekn
#         if self.tournament.registered:
#             if not vekn:
#                 raise CommandFailed(
#                     "This tournament requires registration, "
#                     "please provide your VEKN ID."
#                 )
#             if vekn not in self.tournament.registered:
#                 if not judge:
#                     raise CommandFailed(
#                         "User not registered for that tournament.\n"
#                         f"A {judge_role.mention} can fix this."
#                     )
#                 if not name_args:
#                     raise CommandFailed(
#                         "User is not registered for that tournament.\n"
#                         "Add the user's name to the command to register him."
#                     )
#                 vekn = self._register_player(vekn, " ".join(name_args))
#         if not vekn:
#             vekn = len(self.tournament.players) + 1
#         if (
#             member
#             and self.tournament.players.get(vekn, member.id) != member.id
#             and vekn not in self.tournament.dropped
#         ):
#             other_member = self.guild.get_member(self.tournament.players[vekn])
#             if other_member:
#                 if judge:
#                     await self.send(
#                         f"ID# was used by {other_member.mention},\n"
#                         "they will need to check in again."
#                     )
#                 else:
#                     raise CommandFailed(
#                         f"ID# already used by {other_member.mention},\n"
#                         "they can `archon drop` so you can use this ID instead."
#                     )
#         if judge:
#             self.tournament.disqualified.discard(vekn)
#         if vekn in self.tournament.disqualified:
#             raise CommandFailed("You've been disqualified, you cannot check in again")
#         self.tournament.dropped.discard(vekn)
#         rounds_played = sum(vekn in r for r in self.tournament.results)
#         if (
#             not judge
#             and self.tournament.rounds_limit
#             and rounds_played >= self.tournament.rounds_limit
#         ):
#             raise CommandFailed(
#                 f"You played {rounds_played} rounds already, "
#                 "you cannot check in for this round."
#             )
#         self.tournament.players[vekn] = member.id if member else None
#         # late checkin
#         if self.tournament.staggered:
#             raise CommandFailed(
#                 "This is a staggered tournament, it cannot accept more players."
#             )
#         if not self.tournament.checkin:
#             self._assign_player_numbers()
#         self.update()
#         name = self.tournament.registered.get(vekn, "")
#         await self.send(
#             f"{member.mention if member else 'player'} checked in as "
#             f"{name}{' ' if name else ''}#{vekn}"
#         )


# class CheckinReset(Command):
#     UPDATE = True

#     async def __call__(self, vekn=None, mention=None):
#         self._check_judge()
#         for vekn in self.tournament.players.keys():
#             await self._drop_player(vekn)
#         self.tournament.checkin = False
#         self.update()
#         await self.send("Check-in reset")


# class CheckinStart(Command):
#     UPDATE = True

#     async def __call__(self):
#         self._check_judge()
#         self.tournament.checkin = True
#         self.update()
#         await self.send("Check-in is open")


# class CheckinStop(Command):
#     UPDATE = True

#     async def __call__(self):
#         self._check_judge()
#         self.tournament.checkin = False
#         self.update()
#         await self.send("Check-in is closed")


# class CheckinAll(Command):
#     UPDATE = True

#     async def __call__(self):
#         self._check_judge()
#         if not self.tournament.registered:
#             raise CommandFailed(
#                 "If you do not use checkin, "
#                 "you need to provide a registrations list by using `archon upload` "
#                 "or `archon register`."
#             )
#         self.tournament.players.update(
#             {vekn: None for vekn in self.tournament.registered.keys()}
#         )
#         self.tournament.checkin = False
#         self.update()
#         await self.send("All registered players will play")


# class Drop(Command):
#     UPDATE = True

#     async def __call__(self, *args):
#         self._check_tournament()
#         author = self.message.author
#         vekn = self._get_vekn(author.id)
#         await self._drop_player(vekn)
#         self.update()
#         await self.send(f"{author.mention} dropped out")


# class Caution(Command):
#     UPDATE = True

#     async def __call__(self, *args):
#         self._check_judge()
#         _, vekn = self._get_mentioned_player(*args[:1])
#         self.tournament.cautions.setdefault(vekn, [])
#         if len(self.tournament.cautions[vekn]) > 0:
#             await self.send(
#                 "Player has been cautioned before:\n"
#                 + "\n".join(
#                     f"- R{round}: {caution}"
#                     for round, caution in self.tournament.cautions[vekn]
#                 )
#             )
#         self.tournament.cautions[vekn].append(
#             [self.tournament.current_round, " ".join(args[1:])]
#         )
#         self.update()
#         await self.send("Player cautioned")


# class Warn(Command):
#     UPDATE = True

#     async def __call__(self, *args):
#         self._check_judge()
#         _, vekn = self._get_mentioned_player(*args[:1])
#         self.tournament.warnings.setdefault(vekn, [])
#         if len(self.tournament.warnings[vekn]) > 0:
#             await self.send(
#                 "Player has been warned before:\n"
#                 + "\n".join(
#                     f"- R{round}: {warning}"
#                     for round, warning in self.tournament.warnings[vekn]
#                 )
#             )
#         self.tournament.warnings[vekn].append(
#             [self.tournament.current_round, " ".join(args[1:])]
#         )
#         self.update()
#         await self.send("Player warned")


# class Disqualify(Command):
#     UPDATE = True

#     async def __call__(self, *args):
#         self._check_judge()
#         _, vekn = self._get_mentioned_player(*args[:1])
#         self.tournament.warnings.setdefault(vekn, [])
#         self.tournament.warnings[vekn].append(
#             [self.tournament.current_round, " ".join(args[1:])]
#         )
#         await self._drop_player(vekn)
#         self.tournament.disqualified.add(vekn)
#         self.update()
#         await self.send("Player disqualifed")


# class Player(Command):
#     async def __call__(self, *args):
#         self._check_judge_private()
#         _user_id, vekn = self._get_mentioned_player(*args[:1])
#         self._compute_scores(raise_on_incorrect=False)
#         score = self.scores[vekn]
#         embed = hikari.Embed(title="Player Information", description="")
#         embed.description = f"** {self._player_display(vekn)} **\n"
#         if vekn in self.tournament.disqualified:
#             embed.description += "Disqualified\n"
#         elif vekn in self.tournament.dropped:
#             embed.description += "Absent\n"
#         embed.description += f"{score}\n"
#         if vekn in self.tournament.cautions:
#             cautions = self.tournament.cautions[vekn]
#             embed.add_field(
#                 name="Cautions",
#                 value="\n".join(f"- R{r}: {c}" for r, c in cautions),
#                 inline=False,
#             )
#         if vekn in self.tournament.warnings:
#             warnings = self.tournament.warnings[vekn]
#             embed.add_field(
#                 name="Warnings",
#                 value="\n".join(f"- R{r}: {c}" for r, c in warnings),
#                 inline=False,
#             )
#         await self.send_embed(embed)


# class Players(Command):
#     async def __call__(self):
#         self._check_judge()
#         await self.send_embed(
#             hikari.Embed(
#                 title="Players list",
#                 description="\n".join(
#                     f"- {self._player_display(vekn)}"
#                     for vekn in sorted(self.tournament.players.keys())
#                     if vekn not in self.tournament.dropped
#                 ),
#             )
#         )


# class Registrations(Command):
#     async def __call__(self):
#         self._check_judge_private()
#         embed = hikari.Embed(title="Registrations", description="")
#         for vekn in sorted(self.tournament.registered.keys()):
#             s = f"- {self._player_display(vekn)}"
#             embed.description += s + "\n"
#         await self.send_embed(embed)


# class _ProgressUpdate:
#     def __init__(self, processes, message, embed):
#         self.processes = processes
#         self.message = message
#         self.embed = embed
#         self.progress = [0] * self.processes

#     def __call__(self, i):
#         async def progression(step, **kwargs):
#             self.progress[i] = (step / (ITERATIONS * self.processes)) * 100
#             progress = sum(self.progress)
#             if not progress % 5 and progress < 100:
#                 progress = "▇" * int(progress // 5) + "▁" * (20 - int(progress // 5))
#                 self.embed.description = progress
#                 await self.message.edit(embed=self.embed)

#         return progression


# class RoundStart(Command):
#     UPDATE = True

#     async def __call__(self):
#         self._check_judge()
#         await self._close_current_round()
#         self.tournament.current_round += 1
#         self.tournament.reporting = True
#         self._assign_player_numbers()
#         if not self.tournament.staggered:
#             self._init_seating()
#         if self.tournament.current_round > 1:
#             await self._optimise_seating()
#         round = krcg.seating.Round(
#             self.tournament.seating[self.tournament.current_round - 1]
#         )
#         table_roles = await asyncio.gather(
#             *(
#                 self.guild.create_role(name=f"{self.tournament.prefix}Table-{i + 1}")
#                 for i in range(len(round))
#             )
#         )
#         judge_role = self.judge_role
#         spectator_role = self.spectator_role
#         text_channels = await asyncio.gather(
#             *(
#                 self.guild.create_text_channel(
#                     name=f"Table {i + 1}",
#                     category=self.category,
#                     overwrites={
#                         self.guild.default_role: perm.NO_TEXT,
#                         spectator_role: perm.SPECTATE_TEXT,
#                         table_roles[i]: perm.TEXT,
#                         judge_role: perm.TEXT,
#                     },
#                 )
#                 for i in range(len(round))
#             )
#         )
#         self.tournament.channels.update(
#             {
#                 f"table-{i}-text": channel.id
#                 for i, channel in enumerate(text_channels, 1)
#             }
#         )
#         voice_channels = await asyncio.gather(
#             *(
#                 self.guild.create_voice_channel(
#                     name=f"Table {i + 1}",
#                     category=self.category,
#                     overwrites={
#                         self.guild.default_role: perm.NO_VOICE,
#                         spectator_role: perm.SPECTATE_VOICE,
#                         table_roles[i]: perm.VOICE,
#                         judge_role: perm.JUDGE_VOICE,
#                     },
#                 )
#                 for i in range(len(round))
#             )
#         )
#         self.tournament.channels.update(
#             {
#                 f"table-{i}-voice": channel.id
#                 for i, channel in enumerate(voice_channels, 1)
#             }
#         )
#         members = {
#             n: self.guild.get_member(
#                 self.tournament.players[self.tournament.player_numbers[n]]
#             )
#             for table in round
#             for n in table
#         }
#         await asyncio.gather(
#             *(
#                 members[n].add_roles(table_roles[i], reason=self.reason)
#                 for i, table in enumerate(round)
#                 for n in table
#                 if members[n]
#             )
#         )
#         n_to_vekn = self.tournament.player_numbers
#         embed = hikari.Embed(title=f"Round {self.tournament.current_round} seating")
#         for i, table in enumerate(round, 1):
#             embed.add_field(
#                 name=f"Table {i}",
#                 value="\n".join(
#                     f"- {j}. {self._player_display(n_to_vekn[n])}"[:200]
#                     for j, n in enumerate(table, 1)
#                 ),
#                 inline=False,
#             )
#         self.tournament.checkin = False
#         self.update()
#         messages = await self.send_embed(embed)
#         await asyncio.gather(*(m.pin() for m in messages))
#         await asyncio.gather(
#             *(
#                 text_channels[i].send(
#                     embed=hikari.Embed(
#                         title="Seating",
#                         description="\n".join(
#                             f"- {j}. {self._player_display(n_to_vekn[n])}"[:200]
#                             for j, n in enumerate(table, 1)
#                         ),
#                     )
#                 )
#                 for i, table in enumerate(round)
#             )
#         )

#     def _init_seating(self):
#         round_players = self.round_players
#         if len(round_players) in [6, 7, 11]:
#             raise CommandFailed(
#                 "The number of players requires a staggered tournament. "
#                 "Add or remove players, or use the `archon staggered` command."
#             )
#         if not self.tournament.seating:
#             self.tournament.seating = [list(range(1, len(round_players) + 1))]
#         vekn_to_number = self._vekn_to_number()
#         while self.tournament.current_round > len(self.tournament.seating):
#             self.tournament.seating.append(
#                 [vekn_to_number[vekn] for vekn in round_players]
#             )
#             random.shuffle(self.tournament.seating[-1])
#         while self.tournament.current_round > len(self.tournament.results):
#             self.tournament.results.append({})

#     async def _optimise_seating(self):
#         embed = hikari.Embed(
#             title=f"Round {self.tournament.current_round} - computing seating",
#             description="▁" * 20,
#         )
#         messages = await self.send_embed(embed)
#         progression = _ProgressUpdate(4, messages[0], embed)
#         results = await asyncio.gather(
#             *(
#                 asgiref.sync.sync_to_async(krcg.seating.optimise)(
#                     permutations=self.tournament.seating,
#                     iterations=ITERATIONS,
#                     callback=asgiref.sync.async_to_sync(progression(i)),
#                     fixed=self.tournament.current_round - 1,
#                     ignore=set(),
#                 )
#                 for i in range(4)
#             )
#         )
#         rounds, score = min(results, key=lambda x: x[1].total)
#         logging.info(
#             "Seating – rounds: %s, score:%s=%s", rounds, score.rules, score.total
#         )
#         self.tournament.seating = [
#             list(itertools.chain.from_iterable(r)) for r in rounds
#         ]
#         await messages[0].delete()


# class Staggered(Command):
#     UPDATE = True

#     async def __call__(self, rounds):
#         self._check_judge()
#         if len(self.round_players) not in [6, 7, 11]:
#             raise CommandFailed("Staggered tournaments are for 6, 7 or 11 players")
#         if self.tournament.seating:
#             raise CommandFailed(
#                 "Impossible: a tournament must be staggered from the start."
#             )
#         rounds = int(rounds)
#         self.tournament.checkin = False
#         self.tournament.staggered = True
#         if rounds > 10:
#             raise CommandFailed("Staggered tournaments must have less than 10 rounds")
#         self.tournament.seating = krcg.seating.permutations(
#             len(self.round_players), rounds
#         )
#         for i in range(1, len(self.tournament.seating)):
#             random.shuffle(self.tournament.seating[i])
#         self.update()
#         await self.send(
#             "Staggered tournament ready: "
#             f"{len(self.tournament.seating)} rounds will be played, "
#             f"each player will play {rounds} rounds out of those."
#         )


# class RoundFinish(Command):
#     UPDATE = True

#     async def __call__(self):
#         self._check_judge
#         await self._close_current_round()
#         self.tournament.reporting = False
#         await self.send(f"Round {self.tournament.current_round} finished")


# class RoundReset(Command):
#     UPDATE = True

#     async def __call__(self):
#         self._check_judge
#         self._check_current_round_modifiable()
#         await self._remove_tables()
#         self.tournament.current_round -= 1
#         if self.tournament.current_round <= 0:
#             self.tournament.seating = []
#             self.tournament.staggered = False
#         self.tournament.finals_seeding = []
#         self.update()
#         await self.send("Seating cancelled")


# class RoundAdd(Command):
#     UPDATE = True

#     async def __call__(self, *args):
#         self._check_judge
#         if self.tournament.staggered:
#             raise CommandFailed("Staggered tournament rounds cannot be modified")
#         user_id, vekn = self._get_mentioned_player(*args[:1])
#         member = self.guild.get_member(user_id)
#         if not member:
#             raise CommandFailed("Player not in server")
#         vekn_to_number = self._vekn_to_number()
#         number = vekn_to_number.get(vekn)
#         # this should not happen
#         if not number:
#             raise CommandFailed(
#                 "Player number not assigned - contact archon maintainer"
#             )

#         round_index = self.tournament.current_round - 1
#         player_index = 0
#         tables = self.tournament._get_round_tables()
#         for table_index, table in enumerate(tables, 1):
#             player_index += len(table)
#             if len(table) > 4:
#                 continue
#             self.tournament.seating[round_index].insert(player_index, number)
#             break
#         else:
#             await self.send("No table available to sit this player in")
#             return
#         self.update()
#         tables = self.tournament._get_round_tables()
#         await member.add_roles(
#             self._get_table_role(table_index),
#             reason=self.reason,
#         )
#         await self.send(f"Player seated 5th on table {table_index}")
#         table_channel = self.guild.get_channel(
#             self.tournament.channels[f"table-{table_index}-text"]
#         )
#         await table_channel.send(
#             embed=hikari.Embed(
#                 title="New seating",
#                 description="\n".join(
#                     f"- {j}. {self._player_display(vekn)}"[:200]
#                     for j, vekn in enumerate(tables[table_index - 1], 1)
#                 ),
#             )
#         )


# class RoundRemove(Command):
#     UPDATE = True

#     async def __call__(self, *args):
#         raise CommandFailed("Not yet implemented.")
#         # self._check_judge
#         # if self.tournament.staggered:
#         #     raise CommandFailed("Staggered tournament rounds cannot be modified")
#         # user_id, vekn = self._get_mentioned_player(*args[:1])
#         # member = self.guild.get_member(user_id)
#         # if not member:
#         #     raise CommandFailed("Player not in server")
#         # if vekn not in self.round_players:
#         #     raise CommandFailed("Player is not playing this round")
#         # self.tournament.dropped.add(vekn)
#         # vekn_to_number = self._vekn_to_number()
#         # number = vekn_to_number.get(vekn)
#         # # this should not happen
#         # if not number:
#         #     raise CommandFailed(
#         #         "Player number bot assigned - contact archon maintainer"
#         #     )
#         # index = self.tournament.current_round - 1
#         # for i, table in enumerate(self.tournament.seating[index], 1):
#         #     if number in table:
#         #         if len(table) < 5:
#         #             raise CommandFailed(
#         #                 f"Table {i} has only 4 players, "
#         #                 "a 5th player is required before you can remove one."
#         #             )
#         #         table.remove(number)
#         #         self.update()
#         #         await member.remove_roles(self._get_table_role(i),
#         #             reason=self.reason)
#         #         table_channel = table_channel = self.guild.get_channel(
#         #             self.tournament.channels[f"table-{i}-text"]
#         #         )
#         #         n_to_vekn = self.tournament.player_numbers
#         #         await table_channel.send(
#         #             embed=discord.Embed(
#         #                 title="New seating",
#         #                 description="\n".join(
#         #                     f"- {j}. {self._player_display(n_to_vekn[n])}"[:200]
#         #                     for j, n in enumerate(table, 1)
#         #                 ),
#         #             )
#         #         )
#         #         break
#         # else:
#         #     await self.send("Player not seated in this round")


# class Finals(Command):
#     UPDATE = True

#     async def __call__(self, *args):
#         self._check_judge()
#         await self._close_current_round()
#         self.tournament.current_round += 1
#         self._compute_scores()
#         table_role = await self.guild.create_role(
#             name=f"{self.tournament.prefix}Finals"
#         )
#         top_5 = [
#             (rank, vekn, score)
#             for rank, vekn, score in self._get_ranking(toss=True)
#             if vekn not in self.tournament.disqualified
#         ][:5]
#         self.tournament.finals_seeding = [vekn for _, vekn, _ in top_5]
#         await asyncio.gather(
#             *(
#                 member.add_roles(
#                     table_role,
#                     reason=self.reason,
#                 )
#                 for member in filter(
#                     bool,
#                     (
#                         self.guild.get_member(self.tournament.players.get(vekn, 0))
#                         for vekn in self.tournament.finals_seeding
#                     ),
#                 )
#             )
#         )
#         judge_role = self.judge_role
#         spectator_role = self.spectator_role
#         text_channel, voice_channel = await asyncio.gather(
#             self.guild.create_text_channel(
#                 name="Finals",
#                 category=self.category,
#                 overwrites={
#                     self.guild.default_role: perm.SPECTATE_TEXT,
#                     judge_role: perm.TEXT,
#                     table_role: perm.TEXT,
#                 },
#             ),
#             self.guild.create_voice_channel(
#                 name="Finals",
#                 category=self.category,
#                 overwrites={
#                     self.guild.default_role: perm.NO_VOICE,
#                     spectator_role: perm.SPECTATE_VOICE,
#                     judge_role: perm.JUDGE_VOICE,
#                     table_role: perm.VOICE,
#                 },
#             ),
#         )
#         self.tournament.channels["finals-text"] = text_channel.id
#         self.tournament.channels["finals-vocal"] = voice_channel.id
#         self.update()
#         messages = await self.send_embed(
#             embed=hikari.Embed(
#                 title="Finals",
#                 description="\n".join(
#                     f"- {i}. {self._player_display(vekn)} " f"{score}"
#                     for i, (_, vekn, score) in enumerate(top_5, 1)
#                 ),
#             )
#         )
#         await messages[0].pin()
#         return


# class Standings(Command):
#     async def __call__(self, *args):
#         self._check_judge()
#         embed = hikari.Embed(title="Standings")
#         self._compute_scores(raise_on_incorrect=False)
#         ranking = self._get_ranking()
#         results = []
#         for rank, vekn, score in ranking:
#             if vekn in self.tournament.disqualified:
#                 rank = ""
#             elif self.winner and rank == 1:
#                 rank = "**WINNER** "
#             else:
#                 rank = f"{rank}. "
#             results.append(f"- {rank}{self._player_display(vekn)} " f"{score}")
#         embed.description = "\n".join(results)
#         await self.send_embed(embed)


# class Results(Command):
#     async def __call__(self, *args):
#         self._check_judge()
#         if not self.tournament.current_round:
#             raise CommandFailed("No seating has been done yet.")
#         if self.tournament.finals_seeding:
#             embed = hikari.Embed(title="Finals", description="")
#             for i, vekn in enumerate(self.tournament.finals_seeding, 1):
#                result = self.tournament.results[-1].get(vekn, 0)
#                embed.description += f"{i}. {self._player_display(vekn)}: {result}VP\n"
#             await self.send_embed(embed)
#         else:
#             embed = hikari.Embed(title=f"Round {self.tournament.current_round}")
#             result, tables, incorrect = self.tournament._compute_round_result()
#             if not result:
#                 embed.description = "No table has reported their result yet."
#                 await self.send_embed(embed)
#                 return
#             incorrect = set(incorrect)
#             for i, table in enumerate(tables, 1):
#                 status = "OK"
#                 if sum(result[vekn].vp for vekn in table) == 0:
#                     status = "NOT REPORTED"
#                 elif i in incorrect:
#                     status = "INVALID"
#                 embed.add_field(
#                     name=f"Table {i} {status}",
#                     value="\n".join(
#                         f"{i}. {self._player_display(vekn)} {result[vekn]}"
#                         for i, vekn in enumerate(table, 1)
#                     ),
#                     inline=True,
#                 )
#             await self.send_embed(embed)


# class Report(Command):
#     UPDATE = True

#     async def __call__(self, vps):
#         self._check_round()
#         if not self.tournament.reporting:
#             raise CommandFailed("No round in progress")
#         vps = float(vps.replace(",", "."))
#         vekn = self._get_vekn(self.message.author.id)
#         index = self.tournament.current_round - 1
#         if self.tournament.finals_seeding:
#             if vekn not in self.tournament.finals_seeding:
#                 raise CommandFailed("You did not participate in the finals")
#         elif vekn not in {
#             self.tournament.player_numbers[n] for n in self.tournament.seating[index]
#         }:
#             raise CommandFailed("You did not participate in this round")
#         if vps > 5:
#             raise CommandFailed("That seems like too many VPs")
#         if vps <= 0:
#             self.tournament.results[index].pop(vekn, None)
#         else:
#             self.tournament.results[index][vekn] = vps
#         self.update()
#         await self.send("Result registered")


# class Fix(Command):
#     UPDATE = True

#     async def __call__(self, vekn, vps, round=None):
#         self._check_judge()
#         _, vekn = self._get_mentioned_player(vekn)
#         vps = float(vps.replace(",", "."))
#         round = self.tournament.current_round if round is None else int(round)
#         self._check_round(round)
#         results = self.tournament.results[round - 1]
#         if vps <= 0:
#             results.pop(vekn, None)
#         else:
#             results[vekn] = vps
#         self.update()
#         await self.send("Fixed")


# class FixTable(Command):
#     UPDATE = True

#     async def __call__(self, table, *vekns):
#         raise CommandFailed("Not yet implemented.")
#         # self._check_judge()
#         # round = self.tournament.current_round
#         # seating = self.tournament.seating[round - 1]
#         # index = table - 1
#         # if index > len(seating):
#         #     raise CommandFailed("Invalid table number")
#         # if index == len(seating):
#         #     seating.append([])
#         # if len(vekns) < 4 or len(vekns) > 5:
#         #     raise CommandFailed("Invalid players count: needs to 4 or 5")
#         # already_seated = set(
#         #     self.tournament.player_numbers[i] for t in seating for i in t
#         # ) & set(vekns)
#         # if already_seated:
#         #     raise CommandFailed(
#         #         f"{already_seated} {'are' if len(already_seated) > 1 else 'is'} "
#         #         "already seated elsewhere"
#         #     )
#         # vekn_to_number = self._vekn_to_number()
#         # seating[index] = [vekn_to_number[vekn] for vekn in vekns]
#         # self.update()
#         # # TODO: Add/Remove roles, repost seating.
#         # await self.send(f"Table {table} fixed")


# class Validate(Command):
#     UPDATE = True

#     async def __call__(self, round, table, *args):
#         self._check_judge()
#         round = int(round)
#         table = int(table)
#         reason = " ".join(args)
#         self._check_round(round)
#         self.tournament.overrides[f"{round}-{table}"] = reason
#         self.update()
#         await self.send("Validated")


# class Close(Command):
#     UPDATE = True

#     async def __call__(self, force=None):
#         self._check_judge()
#         force = force == "force"
#         if self.channel.id in self.tournament.channels.values():
#             raise CommandFailed(
#                 "The `close` command must be issued outside of tournament channels"
#             )
#         self._compute_scores(raise_on_incorrect=not force)
#         if not (force or self.tournament.finals_seeding):
#             raise CommandFailed(
#                 "Tournament is not finished. "
#                 "Use `archon close force` to close it nonetheless."
#             )
#         reports = [self._build_results_csv()]
#         if self.tournament.registered and self.tournament.results:
#             reports.append(self._build_methuselahs_csv())
#             reports.extend(f for f in self._build_rounds_csvs())
#             if (
#                 self.tournament.finals_seeding
#                 and len(self.tournament.results) >= self.tournament.current_round
#             ):
#                 reports.append(self._build_finals_csv())
#         await self.channel.send("Reports", files=reports)
#         await asyncio.gather(
#             *(
#                 self.guild.get_channel(channel).delete()
#                 for channel in self.tournament.channels.values()
#                 if self.guild.get_channel(channel)
#             )
#         )
#         await asyncio.gather(
#             *(
#                 role.delete()
#                 for role in self.guild.roles
#                 if role.name.startswith(self.tournament.prefix)
#             )
#         )
#         db.close_tournament(
#             self.connection, self.guild.id,
#             self.category.id if self.category else None
#         )
#         logger.info("closed tournament %s in %s", self.tournament.name,
#               self.guild.name)
#         await self.send("Tournament closed")

#     def _build_results_csv(self):
#         data = []
#         for rank, vekn, score in self._get_ranking():
#             if vekn in self.tournament.disqualified:
#                 rank = "DQ"
#             number = self._get_player_number(vekn)
#             finals_position = ""
#             if vekn in self.tournament.finals_seeding:
#                 finals_position = self.tournament.finals_seeding.index(vekn) + 1
#             data.append(
#                 [
#                     number,
#                     vekn,
#                     self.tournament.registered.get(vekn, ""),
#                     (
#                         sum(1 for s in self.tournament.seating if number in s)
#                         + (1 if vekn in self.tournament.finals_seeding else 0)
#                     ),
#                     score.gw,
#                     score.vp,
#                     finals_position,
#                     rank,
#                 ]
#             )
#         return self._build_csv(
#             "Report.csv",
#             data,
#             columns=[
#                 "Player Num",
#                 "V:EKN Num",
#                 "Name",
#                 "Games Played",
#                 "Games Won",
#                 "Total VPs",
#                 "Finals Position",
#                 "Rank",
#             ],
#         )

#     def _build_methuselahs_csv(self):
#         data = []
#         for number, vekn in sorted(self.tournament.player_numbers.items()):
#             if vekn not in self.tournament.players:
#                 continue
#             name = self.tournament.registered.get(vekn, "UNKNOWN").split(" ", 1)
#             if len(name) < 2:
#                 name.append("")
#             data.append(
#                 [
#                     number,
#                     name[0],
#                     name[1],
#                     "",  # country
#                     vekn,
#                     (
#                         sum(1 for s in self.tournament.seating if number in s)
#                         + (1 if vekn in self.tournament.finals_seeding else 0)
#                     ),
#                     "DQ" if vekn in self.tournament.disqualified else "",
#                 ]
#             )
#         return self._build_csv("Methuselahs.csv", data)

#     def _build_rounds_csvs(self):
#         for i, permutation in enumerate(self.tournament.seating, 1):
#             if len(self.tournament.results) < i:
#                 break
#             data = []
#             for j, table in enumerate(krcg.seating.Round(permutation), 1):
#                 for number in table:
#                     vekn = self.tournament.player_numbers[number]
#                     first_name, last_name = self._get_first_last_name(vekn)
#                     data.append(
#                         [
#                             number,
#                             first_name,
#                             last_name,
#                             j,
#                             self.tournament.results[i - 1].get(vekn, 0),
#                         ]
#                     )
#                 if len(table) < 5:
#                     data.append(["", "", "", "", ""])
#             yield self._build_csv(f"Round {i}.csv", data)

#     def _build_finals_csv(self):
#         data = []
#         vekn_to_number = self._vekn_to_number()
#         for i, vekn in enumerate(self.tournament.finals_seeding, 1):
#             number = vekn_to_number[vekn]
#             first_name, last_name = self._get_first_last_name(vekn)
#             data.append(
#                 [
#                     number,
#                     first_name,
#                     last_name,
#                     1,  # table
#                     i,  # seat
#                     self.tournament.results[-1].get(vekn, 0),
#                 ]
#             )
#         return self._build_csv("Finals.csv", data)

#     def _build_csv(self, filename, it, columns=None):
#         data = io.StringIO()
#         writer = csv.writer(data)
#         if columns:
#             writer.writerow(columns)
#         writer.writerows(it)
#         data = io.BytesIO(data.getvalue().encode("utf-8"))
#         return hikari.File(data, filename=filename)

#     def _get_first_last_name(self, vekn):
#         name = self.tournament.registered.get(vekn, "UNKNOWN").split(" ", 1)
#         if len(name) < 2:
#             name.append("")
#         return name[0], name[1]
