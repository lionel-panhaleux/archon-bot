import asyncio

import csv
import enum
import functools
import itertools
import io
import json
import logging
import random
import re
from dataclasses import dataclass, field, asdict
from typing import Iterable, List, Optional, Union


import hikari
import hikari.channels
import hikari.guilds
import hikari.users
from hikari.interactions.base_interactions import ResponseType
from hikari.interactions.command_interactions import CommandInteraction
from hikari.interactions.component_interactions import ComponentInteraction
import krcg.deck
import krcg.vtes

import chardet
import requests
import stringcase


from . import db
from . import tournament
from . import utils
from . import permissions as perm

logger = logging.getLogger()
CommandFailed = tournament.CommandFailed

APPLICATION = []
COMMANDS = {}
SUB_COMMANDS = {}
COMMANDS_TO_REGISTER = {}
COMPONENTS = {}

VDB_URL = "https://vdb.im"
AMARANTH_URL = "https://amaranth.vtes.co.nz"


# We'll need cleaner VEKN csvs to replace this
VTES_ABBREV_TO_SET = {
    "Jyhad": "Jyhad",
    "VTES": "Vampire: The Eternal Struggle",
    "DS": "Dark Sovereigns",
    "AH": "Ancient Hearts",
    "Sabbat": "Sabbat",
    "SW": "Sabbat War",
    "FN": "Final Nights",
    "BL": "Bloodlines",
    "CE": "Camarilla Edition",
    "Anarchs": "Anarchs",
    "BH": "Black Hand",
    "Gehenna": "Gehenna",
    "Tenth": "Tenth Anniversary",
    "KMW": "Kindred Most Wanted",
    "LoB": "Legacies of Blood",
    "NoR": "Nights of Reckoning",
    "Third": "Third Edition",
    "SoC": "Sword of Caine",
    "LotN": "Lords of the Night",
    "BSC": "Blood Shadowed Court",
    "TR": "Twilight Rebellion",
    "KoT": "Keepers of Tradition",
    "KoTR": "Keepers of Tradition Reprint",
    "EK": "Ebony Kingdom",
    "HttB": "Heirs to the Blood",
    "HttBR": "Heirs to the Blood Reprint",
    "DM": "Danse Macabre",
    "TU": "The Unaligned",
    "AU": "Anarch Unbound",
    "Anthology": "Anthology",
    "Anthology I": "Anthology",  # hack because of VDB/VEKN mismatch here
    "LK": "Lost Kindred",
    "SP": "Sabbat Preconstructed",
    "25th": "Twenty-Fifth Anniversary",
    "FB": "First Blood",
    "V5": "Fifth Edition",
    "V5A": "Fifth Edition (Anarch)",
    "NB": "New Blood",
    "FoL": "Fall of London",
    "SoB": "Shadows of Berlin",
    "EoG": "Echoes of Gehenna",
    "NB2": "New Blood II",
}


def build_command_tree(rest_api):
    """Hikari commands to submit to the Discord server on boot."""
    commands = {}
    for name, klass in COMMANDS_TO_REGISTER.items():
        command = rest_api.slash_command_builder(name, klass.DESCRIPTION)
        for option in klass.OPTIONS:
            command = command.add_option(option)
        commands[klass] = command

    for klass, sub_commands in SUB_COMMANDS.items():
        for name, sub_klass in sub_commands.items():
            if any(
                opt.type == hikari.OptionType.SUB_COMMAND for opt in sub_klass.OPTIONS
            ):
                assert all(
                    opt.type == hikari.OptionType.SUB_COMMAND
                    for opt in sub_klass.OPTIONS
                ), "if one option is a subcommand, they all should be"
                option_type = hikari.OptionType.SUB_COMMAND_GROUP
            else:
                option_type = hikari.OptionType.SUB_COMMAND

            option = hikari.CommandOption(
                type=option_type,
                name=name,
                description=sub_klass.DESCRIPTION,
                options=sub_klass.OPTIONS,
            )
            commands[klass] = commands[klass].add_option(option)

    return list(commands.values())


class MetaCommand(type):
    """Metaclass to register commands."""

    COMMANDS_TO_REGISTER = {}

    def __new__(cls, name, bases, dict_):
        command_name = stringcase.spinalcase(name)
        if command_name in COMMANDS_TO_REGISTER:
            raise ValueError(f"Command {name} is already registered")
        klass = super().__new__(cls, name, bases, dict_)
        if command_name == "base-command":
            return klass
        if klass.GROUP:
            SUB_COMMANDS.setdefault(klass.GROUP, {})
            SUB_COMMANDS[klass.GROUP][command_name] = klass
        else:
            COMMANDS_TO_REGISTER[command_name] = klass
        return klass


class CommandAccess(str, enum.Enum):
    """For now, only the Judge access is controlled."""

    PUBLIC = "PUBLIC"
    ADMIN = "ADMIN"
    PLAYER = "PLAYER"
    JUDGE = "JUDGE"


class Role(str, enum.Enum):
    """Different roles in a tournament"""

    PLAYER = "Player"
    SPECTATOR = "Spectator"
    JUDGE = "Judge"
    ROOT_JUDGE = "Root Judge"


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
            for f in fields:
                embed.add_field(name=f.name, value=f.value, inline=f.is_inline)
            description = ""
            fields = []
        else:
            embed = None
    if len(embeds) > 10:
        raise RuntimeError("Too many embeds")
    return embeds


class InteractionContext:
    """In case of interaction chaining, this context is passed unchanged.

    Track if we have an initial response already, to know if we should create or edit
    """

    def __init__(self):
        self.has_response = False


@dataclass
class DiscordRole:
    id: hikari.Snowflake
    name: str

    @classmethod
    def from_hikari(cls, role: hikari.PartialRole):
        return cls(id=role.id, name=role.name)


@dataclass
class DiscordChannel:
    id: hikari.Snowflake
    name: str
    type: hikari.ChannelType

    @classmethod
    def from_hikari(cls, channel: hikari.PartialChannel):
        return cls(id=channel.id, name=channel.name, type=channel.type)


@dataclass
class DiscordExtra:
    prefix: str = ""
    main_channel_id: hikari.Snowflake = 0
    players: dict[hikari.Snowflake, str] = field(default_factory=dict)
    judges: list[hikari.Snowflake] = field(default_factory=list)
    spectators: list[hikari.Snowflake] = field(default_factory=list)
    roles: dict[Union[Role, int], DiscordRole] = field(default_factory=dict)
    channels: dict[str, dict[Union[Role, int], DiscordChannel]] = field(
        default_factory=dict
    )

    def get_vekn(self, discord_id: hikari.Snowflake) -> Optional[str]:
        return self.players.get(discord_id, None)

    def get_discord_id(self, vekn: str) -> str:
        return {v: k for k, v in self.players.items()}.get(vekn, None)

    def role_name(self, role: Role, table_num: Optional[int] = None) -> str:
        if role == Role.ROOT_JUDGE:
            return "Archon-Judge"
        if role == Role.JUDGE:
            return f"{self.prefix}-Judge"
        elif role == Role.PLAYER:
            if table_num:
                return f"{self.prefix}-Table-{table_num}"
            else:
                return f"{self.prefix}-Player"
        elif role == Role.SPECTATOR:
            return f"{self.prefix}-Spectator"

    def table_name(self, table_num=int) -> str:
        return f"{self.prefix}-Table-{table_num}"

    def table_roles(self) -> List[DiscordRole]:
        ret = sorted(
            [(k, v) for k, v in self.roles.items() if isinstance(k, int)],
            key=lambda a: a[0],
        )
        return [r[1] for r in ret]

    def channel_name(
        self, role: Role, type: hikari.ChannelType, table_num: Optional[int] = None
    ):
        name = self.prefix + "-"
        if role == Role.JUDGE:
            name += "Judge"
        elif role == Role.PLAYER:
            if not table_num:
                raise ValueError("Player channel requires a table number")
            name += f"Table-{table_num}"
        else:
            raise ValueError(f"No channel for {role}")
        if type == hikari.ChannelType.GUILD_TEXT:
            name = name.lower()
        elif type == hikari.ChannelType.GUILD_VOICE:
            pass
        else:
            raise ValueError(f"No channel type {type}")
        return name

    def get_judge_text_channel(self):
        return self.channels["TEXT"][Role.JUDGE]

    def set_judge_text_channel(self, channel: DiscordChannel):
        self.channels.setdefault("TEXT", {})
        self.channels["TEXT"][Role.JUDGE] = channel

    def get_judge_voice_channel(self):
        return self.channels["VOICE"][Role.JUDGE]

    def set_judge_voice_channel(self, channel: DiscordChannel):
        self.channels.setdefault("VOICE", {})
        self.channels["VOICE"][Role.JUDGE] = channel

    def get_table_voice_channel(self, table_num: int):
        return self.channels["VOICE"][table_num]

    def set_table_voice_channel(self, table_num: int, channel: DiscordChannel):
        self.channels.setdefault("VOICE", {})
        self.channels["VOICE"][table_num] = channel

    def all_channels_ids(self):
        return {
            c.id
            for c in itertools.chain(
                self.channels.get("TEXT", {}).values(),
                self.channels.get("VOICE", {}).values(),
            )
        }


class BaseInteraction:
    """Base class for all interactions (commands and components)"""

    #: The interaction update mode (conditions DB lock)
    UPDATE = db.UpdateLevel.READ_ONLY
    #: The interaction requires an open tournament (most of them except open)
    REQUIRES_TOURNAMENT = True
    ACCESS = CommandAccess.PUBLIC

    def __init__(
        self,
        bot: hikari.GatewayBot,
        connection,
        tournament_: tournament.Tournament,
        interaction: Union[CommandInteraction, ComponentInteraction],
        channel_id: hikari.Snowflake,
        category_id: Optional[hikari.Snowflake] = None,
        interaction_context: Optional[InteractionContext] = None,
    ):
        self.bot: hikari.GatewayBot = bot
        self.connection = connection
        self.interaction: Union[CommandInteraction, ComponentInteraction] = interaction
        self.channel_id: hikari.Snowflake = channel_id
        self.author: hikari.InteractionMember = self.interaction.member
        self.guild_id: hikari.Snowflake = self.interaction.guild_id
        self.category_id: hikari.Snowflake = category_id
        self.tournament: tournament.Tournament = tournament_
        discord_data = {}
        if self.tournament:
            discord_data = self.tournament.extra.get("discord", {})
            self.vdb_format = self.tournament.extra.get("vdb_format", {})
        else:
            self.vdb_format = {}
        self.discord = utils.dictas(DiscordExtra, discord_data)
        self.interaction_context = interaction_context or InteractionContext()
        if self.REQUIRES_TOURNAMENT and not self.tournament:
            raise CommandFailed(
                "No tournament running. Please use the "
                f"{OpenTournament.mention()} command."
            )
        if self.ACCESS == CommandAccess.JUDGE and not self._is_judge():
            raise CommandFailed("Only a Judge can call this command")

    @classmethod
    def copy_from_interaction(cls, rhs, *args, **kwargs):
        """Can be used to "chain" interactions.

        For example you might have commands A, B and C as different steps
        for a given process, but want them to chain up in the same context
        """
        return cls(
            *args,
            bot=rhs.bot,
            connection=rhs.connection,
            interaction=rhs.interaction,
            channel_id=rhs.channel_id,
            category_id=rhs.category_id,
            tournament_=rhs.tournament,
            interaction_context=rhs.interaction_context,
            **kwargs,
        )

    async def update(self) -> None:
        """Update tournament data."""
        if self.UPDATE < db.UpdateLevel.WRITE:
            raise RuntimeError("Command is not marked as UPDATE")
        self.tournament.extra["discord"] = asdict(self.discord)
        self.tournament.extra["vdb_format"] = self.vdb_format
        data = asdict(self.tournament)
        await db.update_tournament(
            self.connection,
            self.guild_id,
            self.category_id,
            data,
        )

    def _is_judge(self) -> bool:
        """Check whether the author is a judge."""
        judge_role = self.discord.roles[Role.JUDGE]
        return judge_role.id in self.author.role_ids

    def _is_judge_channel(self) -> bool:
        """Check wether the command was issued in the Judges private channel."""
        return self.channel_id == self.discord.get_judge_text_channel().id

    def _player_display(self, vekn: str) -> str:
        """How to display a player."""
        name = None
        if vekn in self.tournament.players:
            name = self.tournament.players[vekn].name
            discord_id = self.discord.get_discord_id(vekn)
            if name and len(name) > 32:
                name[:29] + "..."
        return (
            ("**[D]** " if vekn in self.tournament.dropped else "")
            + (f"{name} #{vekn}" if name else f"#{vekn}")
            + (f"<@{discord_id}>" if discord_id else "")
        )

    def _deck_display(self, data: dict) -> str:
        deck = krcg.deck.Deck()
        deck.from_json(data)
        return f"[{deck.name}]({deck.to_vdb()})"

    async def _align_roles(
        self,
        initial: bool = False,
        silence_exceptions: bool = False,
    ) -> None:
        # list what is expected
        expected = [(r, self.discord.role_name(r)) for r in Role]
        if self.tournament.state == tournament.TournamentState.PLAYING:
            for table_num in range(1, self.tournament.tables_count() + 1):
                expected.append(
                    (table_num, self.discord.role_name(Role.PLAYER, table_num))
                )
        logger.debug("expected roles: %s", expected)
        # delete spurious keys from registry
        to_delete = []
        expected_keys = {e[0] for e in expected}
        for key, role in self.discord.roles.items():
            if key not in expected_keys:
                to_delete.append(key)
        for key in to_delete:
            logger.debug(
                "deleting unexpected role from registry: %s: %s",
                key,
                self.discord.roles[key],
            )
            del self.discord.roles[key]
        # compare what exists with what is registered
        existing = await self.bot.rest.fetch_roles(self.guild_id)
        # special case for the root judge role: keep it and its ID if it exists
        root_judge = [
            r for r in existing if r.name == self.discord.role_name(Role.ROOT_JUDGE)
        ]
        if root_judge:
            self.discord.roles[Role.ROOT_JUDGE] = DiscordRole.from_hikari(root_judge[0])
        existing = [r for r in existing if r.name.startswith(self.discord.prefix + "-")]
        if existing and initial:
            raise CommandFailed(
                f"Roles with the {self.discord.prefix}- prefix exist: "
                "remove them or use another tournament name."
            )
        logger.debug("existing roles on discord: %s", existing + root_judge)
        registered = {r.id for r in self.discord.roles.values()}
        logger.debug("registered roles: %s", self.discord.roles)
        # delete spurious from discord
        to_delete = [r.id for r in existing if r.id not in registered]
        if to_delete:
            logger.warning("deleting unexpected roles on discord: %s", to_delete)
            # delete spurious from discord
            await asyncio.gather(
                *(self.bot.rest.delete_role(self.guild_id, r) for r in to_delete),
                return_exceptions=silence_exceptions,
            )
        existing = {r.id for r in existing + root_judge if r.id in registered}
        # delete spurious from registry (do not delete the root judge)
        to_delete = []
        for key, role in self.discord.roles.items():
            if role.id not in existing:
                to_delete.append(key)
        for key in to_delete:
            logger.debug(
                "deleting unavailable role from registry: %s: %s",
                key,
                self.discord.roles[key],
            )
            del self.discord.roles[key]
        # now discord and internal registry are aligned
        # create what is missing both on discord and in registry
        keys_to_create = []
        roles_to_create = []
        for key, name in expected:
            if key not in self.discord.roles:
                logger.debug("creating role on discord: %s, %s", key, name)
                keys_to_create.append(key)
                roles_to_create.append(
                    self.bot.rest.create_role(
                        self.guild_id,
                        name=name,
                        mentionable=True,
                        reason=self.reason,
                    )
                )
        roles = await asyncio.gather(*roles_to_create)
        # assign the newly created roles to the guild members
        id_roles = []
        for key, role in zip(keys_to_create, roles):
            logger.debug("creating role in registry: %s, %s", key, role)
            self.discord.roles[key] = DiscordRole.from_hikari(role)
            # when we're recreating JUDGE / TABLE role,
            # we must drop the matching channels
            self.discord.channels.get("TEXT", {}).pop(key, None)
            self.discord.channels.get("VOICE", {}).pop(key, None)
            if key == Role.PLAYER:
                id_roles.extend((did, role) for did in self.discord.players.keys())
            elif key == Role.ROOT_JUDGE:
                id_roles.extend((uid, role) for uid in self.discord.judges)
                id_roles.append((self.bot.get_me().id, role))
                logger.warning(
                    "Recreating Root judge role, "
                    "review commands permissions settings"
                )
            elif key == Role.JUDGE:
                id_roles.extend((uid, role) for uid in self.discord.judges)
                id_roles.append((self.bot.get_me().id, role))
                if not initial:
                    logger.warning(
                        "Recreating Judge role, "
                        "bot might miss access to previous channels"
                    )
            elif key == Role.SPECTATOR:
                id_roles.extend((uid, role) for uid in self.discord.spectators)
            else:
                try:
                    table_num = int(key)
                except ValueError:
                    raise RuntimeError(f"Unexpected role key {key}")
                # table role
                if not self.tournament.rounds:
                    logger.debug("Missing table role, but no round in progress")
                    continue
                round = self.tournament.rounds[self.tournament.current_round - 1]
                table = round.seating[table_num - 1]
                for vekn in table:
                    if vekn not in self.tournament.players:
                        continue
                    discord_id = self.discord.get_discord_id(vekn)
                    if not discord_id:
                        continue
                    id_roles.append((discord_id, role))

        if id_roles:
            logger.debug("assigning roles: %s", id_roles)
            await asyncio.gather(
                *[
                    self.bot.rest.add_role_to_member(
                        self.guild_id,
                        snowflake,
                        role,
                        reason=self.reason,
                    )
                    for snowflake, role in id_roles
                ]
            )
        logger.debug("roles aligned")

    async def _align_channels(
        self,
        initial: bool = False,
        silence_exceptions: bool = False,
    ) -> None:
        # list what is expected
        expected = [
            (
                ("TEXT", Role.JUDGE),
                self.discord.channel_name(Role.JUDGE, hikari.ChannelType.GUILD_TEXT),
                [
                    hikari.PermissionOverwrite(
                        id=self.bot.get_me().id,
                        type=hikari.PermissionOverwriteType.MEMBER,
                        allow=perm.ARCHON,
                    ),
                    hikari.PermissionOverwrite(
                        id=self.guild_id,
                        type=hikari.PermissionOverwriteType.ROLE,
                        deny=perm.TEXT,
                    ),
                    hikari.PermissionOverwrite(
                        id=self.discord.roles[Role.JUDGE].id,
                        type=hikari.PermissionOverwriteType.ROLE,
                        allow=perm.TEXT,
                    ),
                ],
            ),
            (
                ("VOICE", Role.JUDGE),
                self.discord.channel_name(Role.JUDGE, hikari.ChannelType.GUILD_VOICE),
                [
                    hikari.PermissionOverwrite(
                        id=self.bot.get_me().id,
                        type=hikari.PermissionOverwriteType.MEMBER,
                        allow=perm.ARCHON,
                    ),
                    hikari.PermissionOverwrite(
                        id=self.guild_id,
                        type=hikari.PermissionOverwriteType.ROLE,
                        deny=perm.VOICE,
                    ),
                    hikari.PermissionOverwrite(
                        id=self.discord.roles[Role.JUDGE].id,
                        type=hikari.PermissionOverwriteType.ROLE,
                        allow=perm.VOICE,
                    ),
                ],
            ),
        ]
        if self.tournament.state == tournament.TournamentState.PLAYING:
            for table_num in range(1, self.tournament.tables_count() + 1):
                expected.append(
                    (
                        ("VOICE", table_num),
                        self.discord.channel_name(
                            Role.PLAYER, hikari.ChannelType.GUILD_VOICE, table_num
                        ),
                        [
                            hikari.PermissionOverwrite(
                                id=self.bot.get_me().id,
                                type=hikari.PermissionOverwriteType.MEMBER,
                                allow=perm.ARCHON,
                            ),
                            hikari.PermissionOverwrite(
                                id=self.guild_id,
                                type=hikari.PermissionOverwriteType.ROLE,
                                deny=perm.VOICE,
                            ),
                            hikari.PermissionOverwrite(
                                id=self.discord.roles[table_num].id,
                                type=hikari.PermissionOverwriteType.ROLE,
                                allow=perm.VOICE,
                            ),
                            hikari.PermissionOverwrite(
                                id=self.discord.roles[Role.JUDGE].id,
                                type=hikari.PermissionOverwriteType.ROLE,
                                allow=perm.JUDGE_VOICE,
                            ),
                            hikari.PermissionOverwrite(
                                id=self.discord.roles[Role.SPECTATOR].id,
                                type=hikari.PermissionOverwriteType.ROLE,
                                allow=perm.SPECTATE_VOICE,
                            ),
                        ],
                    )
                )
        # delete spurious keys from registry
        to_delete = []
        expected_keys = {e[0] for e in expected}
        logger.debug("expected channels: %s", expected_keys)
        for key0, channels in self.discord.channels.items():
            for key1, channel in channels.items():
                if (key0, key1) not in expected_keys:
                    to_delete.append((key0, key1))
        for key0, key1 in to_delete:
            logger.debug(
                "deleting unexpected channel from registry: %s: %s",
                (key0, key1),
                self.discord.channels[key0][key1],
            )
            del self.discord.channels[key0][key1]
        # compare what exists with what is registered
        registered = self.discord.all_channels_ids()
        logger.debug("registered channels: %s", self.discord.channels)
        existing = await self.bot.rest.fetch_guild_channels(self.guild_id)
        existing = [
            c
            for c in existing
            if c.type in {hikari.ChannelType.GUILD_TEXT, hikari.ChannelType.GUILD_VOICE}
        ]
        if self.category_id:
            existing = [c for c in existing if c.parent_id == self.category_id]
        existing = [
            c
            for c in existing
            if c.name.lower().startswith(self.discord.prefix.lower() + "-")
        ]
        if existing and initial:
            raise CommandFailed(
                f"Channels with the {self.discord.prefix}- prefix exist: "
                "remove them or use another tournament name."
            )
        logger.debug("existing channels on discord: %s", existing)
        to_delete = [c for c in existing if c.id not in registered]
        if to_delete:
            logger.debug("deleting channels on discord: %s", to_delete)
            # delete spurious from discord
            try:
                result = await asyncio.gather(
                    *(self.bot.rest.delete_channel(c.id) for c in to_delete),
                    return_exceptions=silence_exceptions,
                )
            except hikari.ClientHTTPResponseError as err:
                raise CommandFailed(f"Failed to delete channel: {err}")
            errors = [
                r for r in result if isinstance(r, hikari.ClientHTTPResponseError)
            ]
            if errors:
                logger.warning("errors closing channels: %s", errors)
        existing = {c.id for c in existing if c.id in registered}
        # delete spurious from registry
        to_delete = []
        for key0, channels in self.discord.channels.items():
            for key1, channel in channels.items():
                if channel.id not in existing:
                    to_delete.append((key0, key1))
        for key0, key1 in to_delete:
            logger.debug(
                "deleting unavailable channel from registry: %s: %s",
                (key0, key1),
                self.discord.channels[key0][key1],
            )
            del self.discord.channels[key0][key1]

        # the registry now matches discord
        # create what is missing both on discord and in registry
        keys_to_create = []
        to_create = []
        self.discord.channels.setdefault("TEXT", {})
        self.discord.channels.setdefault("VOICE", {})
        for key, name, permissions in expected:
            if key[1] in self.discord.channels[key[0]]:
                continue
            keys_to_create.append(key)
            if key[0] == "TEXT":
                logger.debug("creating channel on discord: %s, %s", key, name)
                to_create.append(
                    self.bot.rest.create_guild_text_channel(
                        self.guild_id,
                        name,
                        category=self.category_id or hikari.UNDEFINED,
                        reason=self.reason,
                        permission_overwrites=permissions,
                    )
                )
            elif key[0] == "VOICE":
                logger.debug("creating channel on discord: %s, %s", key, name)
                to_create.append(
                    self.bot.rest.create_guild_voice_channel(
                        self.guild_id,
                        name,
                        category=self.category_id or hikari.UNDEFINED,
                        reason=self.reason,
                        permission_overwrites=permissions,
                    )
                )
            else:
                raise RuntimeError("Unexpected channel type")
        result = await asyncio.gather(*to_create)
        for key, res in zip(keys_to_create, result):
            logger.debug("add channel to registry: %s, %s", key, res)
            self.discord.channels[key[0]][key[1]] = DiscordChannel.from_hikari(res)
        logger.debug("channels aligned")

    @property
    def reason(self) -> str:
        """Reason given for Discord logs on channel/role creations."""
        return f"{self.tournament.name} Tournament"

    async def __call__(self) -> None:
        """To implement in children classes"""
        raise NotImplementedError()


class BaseCommand(BaseInteraction, metaclass=MetaCommand):
    """Base class for all commands"""

    #: Discord ID, set by the GatewayBot on connection
    DISCORD_ID = None
    #: Command description. Override in children.
    DESCRIPTION = ""
    #: Define command options. Override in children as needed.
    OPTIONS = []
    #: Main command this sub command is attached to, if any
    GROUP = None

    async def deferred(self, flags: Optional[hikari.MessageFlag] = None) -> None:
        """Let Discord know we're working (displays the '...' on Discord).

        It's useful especially for commands that have a bit of compute time,
        where we cannot be certain we will answer fast enough for Discord to
        not drop the command altogether.

        Note the flags (None or EPHEMERAL) passed should match the ones used in
        subsequent calls to create_or_edit_response.
        """
        await self.interaction.create_initial_response(
            ResponseType.DEFERRED_MESSAGE_CREATE, flags=flags
        )
        self.interaction_context.has_response = True

    async def create_or_edit_response(self, *args, **kwargs) -> None:
        """Create or edit the interaction response.

        The flags (None or EPHEMERAL) are used on creation to display the answer
        to the author only (EPHEMERAL) or everyone.
        You can pass empty list for embeds and components if you want to reset them.
        """
        flags = kwargs.pop("flags", None)
        if self.interaction_context.has_response:
            func = self.interaction.edit_initial_response
        else:
            func = functools.partial(
                self.interaction.create_initial_response,
                ResponseType.MESSAGE_CREATE,
                flags=flags,
            )
        await func(*args, **kwargs)
        self.interaction_context.has_response = True

    @classmethod
    def mention(cls, subcommand: str = None):
        name = stringcase.spinalcase(cls.__name__)
        if subcommand:
            name += f" {subcommand}"
        return f"</{name}:{cls.DISCORD_ID}>"


class BaseComponent(BaseInteraction):
    """Base class for all components"""

    async def deferred(self, flags: Optional[hikari.MessageFlag] = None) -> None:
        """Let Discord know we're working (displays the '...' on Discord)."""
        await self.interaction.create_initial_response(
            ResponseType.DEFERRED_MESSAGE_UPDATE, flags=flags
        )
        self.interaction_context.has_response = True

    async def create_or_edit_response(self, *args, **kwargs) -> None:
        """Create or edit the interaction response."""
        flags = kwargs.pop("flags", None)
        if self.interaction_context.has_response:
            func = self.interaction.edit_initial_response
        else:
            func = functools.partial(
                self.interaction.create_initial_response,
                ResponseType.MESSAGE_UPDATE,
                flags=flags,
            )
        await func(*args, **kwargs)
        self.interaction_context.has_response = True


class BaseModal(BaseInteraction):
    """Base class for all components"""

    async def deferred(self, flags: Optional[hikari.MessageFlag] = None) -> None:
        """Let Discord know we're working (displays the '...' on Discord)."""
        await self.interaction.create_initial_response(
            ResponseType.DEFERRED_MESSAGE_UPDATE, flags=flags
        )
        self.interaction_context.has_response = True

    async def create_or_edit_response(self, *args, **kwargs) -> None:
        """Create or edit the interaction response."""
        flags = kwargs.pop("flags", None)
        if self.interaction_context.has_response:
            func = self.interaction.edit_initial_response
        else:
            func = functools.partial(
                self.interaction.create_initial_response,
                ResponseType.MESSAGE_CREATE,
                flags=flags,
            )
        await func(*args, **kwargs)
        self.interaction_context.has_response = True


class OpenTournament(BaseCommand):
    """Open the tournament"""

    UPDATE = db.UpdateLevel.EXCLUSIVE_WRITE
    REQUIRES_TOURNAMENT = False
    DESCRIPTION = "ADMIN: Open a new event or tournament"
    OPTIONS = [
        hikari.CommandOption(
            type=hikari.OptionType.STRING,
            name="name",
            description="The tournament name",
            is_required=True,
        ),
    ]

    async def __call__(self, name: str) -> None:
        """Open the tournament, create channels and roles, then configure (chain)"""
        if self.tournament:
            raise CommandFailed("A tournament is already open here")
        await self.deferred(flags=hikari.MessageFlag.EPHEMERAL)
        logger.debug("Creating tournament...")
        self.tournament = tournament.Tournament(name=name)
        self.discord.prefix = "".join([w[0] for w in name.split()][:3])
        self.discord.main_channel_id = self.channel_id
        self.discord.judges.append(self.author.id)
        await self._align_roles(initial=True)
        await self._align_channels(initial=True)
        # author is now a judge, he can configure (next step)
        self.author.role_ids.append(self.discord.roles[Role.JUDGE].id)
        logger.debug("Register tournament in DB...")
        self.tournament.extra["discord"] = asdict(self.discord)
        await db.create_tournament(
            self.connection,
            self.guild_id,
            self.category_id,
            asdict(self.tournament),
        )
        # now configure the tournament
        next_step = ConfigureTournament.copy_from_interaction(self)
        await next_step()


class ConfigureTournament(BaseCommand):
    """Configure. Chained from OpenTournament, can also be called on its own.

    VEKN_REQUIRED: Requires VEKN ID# for registration, check against VEKN website
    DECKLIST_REQUIRED: Requires decklist (VDB or Amaranth), check legqlity
    CHECKIN_EACH_ROUND: Players must check in beafore each round
    LEAGUE: Players can still register and change deck once the tournament is running
    STAGGERED: 6, 7, 11 players round-robin
    """

    UPDATE = db.UpdateLevel.WRITE
    ACCESS = CommandAccess.JUDGE
    DESCRIPTION = "JUDGE: Configure the tournament"
    OPTIONS = []

    async def __call__(self) -> None:
        # has been acknowledged/deferred already
        if hasattr(self.interaction, "custom_id"):
            if self.interaction.custom_id == "vekn-required":
                self.tournament.flags ^= tournament.TournamentFlag.VEKN_REQUIRED
            elif self.interaction.custom_id == "decklist-required":
                self.tournament.flags ^= tournament.TournamentFlag.DECKLIST_REQUIRED
            elif self.interaction.custom_id == "checkin-each-round":
                self.tournament.flags ^= tournament.TournamentFlag.CHECKIN_EACH_ROUND
            elif self.interaction.custom_id == "multideck":
                self.tournament.flags ^= tournament.TournamentFlag.MULTIDECK
            elif (
                self.interaction.custom_id == "register-between"
                and not self.tournament.flags & tournament.TournamentFlag.STAGGERED
            ):
                self.tournament.flags ^= tournament.TournamentFlag.REGISTER_BETWEEN
            await self.update()
        vekn_required = self.tournament.flags & tournament.TournamentFlag.VEKN_REQUIRED
        decklist_required = (
            self.tournament.flags & tournament.TournamentFlag.DECKLIST_REQUIRED
        )
        checkin_each_round = (
            self.tournament.flags & tournament.TournamentFlag.CHECKIN_EACH_ROUND
        )
        multideck = self.tournament.flags & tournament.TournamentFlag.MULTIDECK
        between = self.tournament.flags & tournament.TournamentFlag.REGISTER_BETWEEN
        if getattr(self.interaction, "custom_id", None) == "validate":
            components = []
            COMPONENTS.pop("vekn-required", None)
            COMPONENTS.pop("decklist-required", None)
            COMPONENTS.pop("checkin-each-round", None)
            COMPONENTS.pop("multideck", None)
            COMPONENTS.pop("register-between", None)
            COMPONENTS.pop("validate", None)
        else:
            components = [
                self.bot.rest.build_message_action_row()
                .add_interactive_button(
                    hikari.ButtonStyle.SECONDARY
                    if vekn_required
                    else hikari.ButtonStyle.PRIMARY,
                    "vekn-required",
                    label=("No VEKN" if vekn_required else "Require VEKN"),
                )
                .add_interactive_button(
                    hikari.ButtonStyle.SECONDARY
                    if decklist_required
                    else hikari.ButtonStyle.PRIMARY,
                    "decklist-required",
                    label=("No Decklist" if decklist_required else "Require Decklist"),
                )
                .add_interactive_button(
                    hikari.ButtonStyle.SECONDARY
                    if checkin_each_round
                    else hikari.ButtonStyle.PRIMARY,
                    "checkin-each-round",
                    label=(
                        "Checkin once" if checkin_each_round else "Checkin each round"
                    ),
                )
                .add_interactive_button(
                    hikari.ButtonStyle.SECONDARY
                    if multideck
                    else hikari.ButtonStyle.PRIMARY,
                    "multideck",
                    label=("Single Deck" if multideck else "Multideck"),
                ),
                self.bot.rest.build_message_action_row().add_interactive_button(
                    hikari.ButtonStyle.SUCCESS, "validate", label="OK"
                ),
            ]
            COMPONENTS["vekn-required"] = ConfigureTournament
            COMPONENTS["decklist-required"] = ConfigureTournament
            COMPONENTS["checkin-each-round"] = ConfigureTournament
            COMPONENTS["multideck"] = ConfigureTournament
            COMPONENTS["validate"] = ConfigureTournament
            # only allow to activate the register-between option if not staggered
            if not self.tournament.flags & tournament.TournamentFlag.STAGGERED:
                components[0].add_interactive_button(
                    hikari.ButtonStyle.SECONDARY
                    if between
                    else hikari.ButtonStyle.PRIMARY,
                    "register-between",
                    label=("No late joiners" if between else "Join anytime"),
                )
                COMPONENTS["register-between"] = ConfigureTournament
        # main embed
        embed = hikari.Embed(
            title=f"Configuration - {self.tournament.name}",
            description=(
                f"- VEKN ID# is {'' if vekn_required else 'not '}required\n"
                f"- Decklist is {'' if decklist_required else 'not '}required\n"
                f"- Check-in {'each round' if checkin_each_round else 'once'}\n"
                f"- {'Different decks can' if multideck else 'A single deck must'} "
                "be used throughout the tournament\n"
                f"- Players {'can still' if between else 'cannot'} join "
                "after first round\n"
            ),
        )
        if self.tournament.is_limited():
            limits = []
            if self.tournament.flags & tournament.TournamentFlag.SINGLE_CLAN:
                limits.append("- Single clan (75%) in crypt")
            if self.tournament.flags & tournament.TournamentFlag.SINGLE_VAMPIRE:
                limits.append("- Single vampire in crypt")
            if self.tournament.include:
                limits.append("- Limited list of allowed cards")
            if self.tournament.exclude:
                limits.append("- Additional list of banned cards")
            embed.description += "\n**Limited tournament**\n" + "\n".join(limits) + "\n"
            embed.description += (
                f"Use {DefineLimited.mention()} to modify the format.\n"
            )
            if components and self.vdb_format:
                COMPONENTS["vdb-format"] = DownloadVDBFormat
                components.insert(
                    1,
                    self.bot.rest.build_message_action_row().add_interactive_button(
                        hikari.ButtonStyle.PRIMARY,
                        "vdb-format",
                        label="Download VDB format",
                    ),
                )
        else:
            embed.description += (
                f"\n**Standard tournament**\n"
                f"You can use {DefineLimited.mention()} to define a limited format.\n"
            )
        if components:
            if between and not self.tournament.max_rounds:
                embed.description += (
                    "\n**League mode**"
                    "\nIf this is a league, you might want to limit the number of"
                    " rounds a player can participate in with "
                    f"{SetMaxRounds.mention()}.\n"
                )
            if not self.tournament.rounds and len(self.tournament.players) in [
                6,
                7,
                11,
            ]:
                embed.description += (
                    "\n**Staggered tournament**"
                    "\nIf you have 6, 7, or 11 players, you can use "
                    f"{Stagger.mention()} to make this a staggered tournament"
                    "(eg. each player playing 2 out of 3 rounds).\n"
                )
        else:
            embed.description += (
                "\n**Registrations are now open**\n"
                f"- Use {Announce.mention()} to display help for players and judges\n"
                f"- Use {Appoint.mention()} to appoint judges, bots and spectators\n"
            )
        # different API response when a component is clicked,
        if getattr(self.interaction, "custom_id", None):
            await self.interaction.create_initial_response(
                hikari.ResponseType.MESSAGE_UPDATE,
                embed=embed,
                components=components,
            )
            self.interaction_context.has_response = True
            if not components:
                next_step = Announce.copy_from_interaction(self)
                await next_step()
        # when called directly or just after the `open` command
        else:
            await self.create_or_edit_response(
                embed=embed,
                flags=hikari.MessageFlag.EPHEMERAL,
                components=components,
            )


class DownloadVDBFormat(BaseComponent):
    UPDATE = db.UpdateLevel.READ_ONLY
    ACCESS = CommandAccess.PUBLIC

    async def __call__(self):
        if not self.vdb_format:
            raise CommandFailed("No format file available")
        data = json.dumps(self.vdb_format, indent=2).encode("utf-8")
        attachment = hikari.Bytes(
            data, f"{self.tournament.name}_vdb_format.txt", mimetype="text"
        )
        await self.interaction.create_initial_response(
            hikari.ResponseType.MESSAGE_CREATE,
            flags=hikari.MessageFlag.EPHEMERAL,
            attachment=attachment,
        )
        self.interaction_context.has_response = True


class SetMaxRounds(BaseCommand):
    """Optional: configure a maximum number of rounds for players.
    Mostly useful for leagues.
    """

    UPDATE = db.UpdateLevel.WRITE
    ACCESS = CommandAccess.JUDGE
    DESCRIPTION = "JUDGE: Set a maximum number of rounds for the tournament"
    OPTIONS = [
        hikari.CommandOption(
            type=hikari.OptionType.INTEGER,
            name="rounds_count",
            description="The number of rounds a player is allowed to play",
            is_required=True,
            min_value=1,
        ),
    ]

    async def __call__(self, rounds_count: int) -> None:
        self.tournament.max_rounds = rounds_count
        await self.update()
        await self.create_or_edit_response(
            embed=hikari.Embed(
                title=f"Max rounds set: {rounds_count}",
                description=(
                    "Players will be forbidden to play more than "
                    f"{rounds_count} round{'s' if rounds_count > 1 else ''} "
                    "during this tournament."
                ),
            ),
            flags=hikari.MessageFlag.EPHEMERAL,
        )


class Stagger(BaseCommand):
    """Configure Tournament as staggered for 6, 7 or 11 players."""

    UPDATE = db.UpdateLevel.WRITE
    ACCESS = CommandAccess.JUDGE
    DESCRIPTION = "JUDGE: Make a tournament staggered (for 7, 9, or 11 players)"
    OPTIONS = [
        hikari.CommandOption(
            type=hikari.OptionType.INTEGER,
            name="rounds_count",
            description="The number of rounds each player will play",
            is_required=True,
            min_value=1,
        ),
    ]

    async def _progress(self, step, **kwargs) -> None:
        """Progress bar for the start subcommand"""
        chunk = tournament.ITERATIONS // 20
        if step % chunk:
            return
        progress = step // chunk
        await self.create_or_edit_response(
            embed=hikari.Embed(
                title="Computing seating...",
                description="▇" * progress + "▁" * (20 - progress),
            )
        )

    async def __call__(self, rounds_count: int) -> None:
        await self.create_or_edit_response(
            embed=hikari.Embed(
                title="Computing seating...",
                description="▁" * 20,
            )
        )
        await self.tournament.make_staggered(rounds_count, self._progress)
        await self.update()
        await self.create_or_edit_response(
            embed=hikari.Embed(
                title=f"Staggered Tournament",
                description=(
                    "This tournament is now staggered: the players list "
                    "cannot be changed anymore.\n"
                    f"Use {UnStagger.mention()} to undo this."
                ),
            ),
            flags=hikari.MessageFlag.EPHEMERAL,
        )


class UnStagger(BaseCommand):
    """Switch back from staggered to standard."""

    UPDATE = db.UpdateLevel.WRITE
    ACCESS = CommandAccess.JUDGE
    DESCRIPTION = "JUDGE: Switch from staggered to standard tournament"
    OPTIONS = []

    async def __call__(self) -> None:
        self.tournament.unmake_staggered()
        await self.update()
        await self.create_or_edit_response(
            embed=hikari.Embed(
                title=f"Standard Tournament",
                description="The tournament is back to a standard structure.",
            ),
            flags=hikari.MessageFlag.EPHEMERAL,
        )


class DefineLimited(BaseCommand):
    """Define a limited tournament"""

    UPDATE = db.UpdateLevel.WRITE
    ACCESS = CommandAccess.JUDGE
    DESCRIPTION = "JUDGE: Define a limited format (no parameters removes all limits)"
    OPTIONS = [
        hikari.CommandOption(
            type=hikari.OptionType.ATTACHMENT,
            name="vdb_format",
            description="A VDB format file",
            is_required=False,
        ),
        hikari.CommandOption(
            type=hikari.OptionType.ATTACHMENT,
            name="ban_list",
            description="A list of banned cards",
            is_required=False,
        ),
        hikari.CommandOption(
            type=hikari.OptionType.BOOLEAN,
            name="single_clan",
            description="Crypts need to be a single clan for 9 out of 12 cards",
            is_required=False,
        ),
        hikari.CommandOption(
            type=hikari.OptionType.BOOLEAN,
            name="single_vampire",
            description="Only one vampire allowed in crypt",
            is_required=False,
        ),
    ]

    async def __call__(
        self,
        vdb_format: Optional[hikari.Snowflake] = None,
        ban_list: Optional[hikari.Snowflake] = None,
        single_clan: Optional[bool] = False,
        single_vampire: Optional[bool] = False,
    ) -> None:
        description = ""
        if vdb_format and ban_list:
            await self.create_or_edit_response(
                "You can provide either a vdb_format or a ban_list, not both."
            )
            return
        if vdb_format or ban_list:
            data = self.interaction.resolved.attachments[vdb_format or ban_list]
            data = await data.read()
            data = data.decode(chardet.detect(data)["encoding"])
        else:
            self.tournament.include = []
            self.tournament.exclude = []
        if vdb_format:
            if data.startswith("{"):
                try:
                    krcg.vtes.VTES.load()
                except requests.HTTPError:
                    raise CommandFailed("Failed to load VTES database.")
                try:
                    data = json.loads(data)
                    self.vdb_format = data
                except json.JSONDecodeError:
                    raise CommandFailed("Invalid VDB format file.")
                sets = set([k for k, v in data["sets"].items() if v])
                allowed = set(
                    [int(k) for k, v in data["allowed"]["crypt"].items() if v]
                    + [int(k) for k, v in data["allowed"]["library"].items() if v]
                )
                banned = set(
                    [int(k) for k, v in data["banned"]["crypt"].items() if v]
                    + [int(k) for k, v in data["banned"]["library"].items() if v]
                )
                if allowed & banned:
                    raise CommandFailed(
                        "Invalid format: some cards are both allowed and banned"
                    )
                # hack for Anthology I
                if "Anthology I" in sets:
                    sets.pop("Anthology I")
                    sets.add("Anthology")
                    if not (101110 in allowed or "SoB" in sets):
                        banned.add(101110)  # The Line
                    if 102128 not in allowed:
                        banned.add(102128)  # Vivenne Géroux
                    if not (200030 in allowed or "EoG" in sets):
                        banned.add(200030)  # Aisha az-Zahra
                    if not (200081 in allowed or "SoB" in sets):
                        banned.add(200081)  # André the Manipulator
                    if not (200105 in allowed or "SoB" in sets):
                        banned.add(200105)  # Anne-Marie Bourgeois
                    if not (200123 in allowed or "EoG" in sets):
                        banned.add(200123)  # Apolonia Czarnecki
                    if not (200562 in allowed or "SoB" in sets):
                        banned.add(200562)  # Hamid Mansour
                    if not (200723 in allowed or "EoG" in sets):
                        banned.add(200723)  # Joseph Fisher
                    if not (200816 in allowed or "SoB" in sets):
                        banned.add(200816)  # Laura Goldman
                    if not (201321 in allowed or "EoG" in sets):
                        banned.add(201321)  # Styles Margs
                    if not (201465 in allowed or "SoB" in sets):
                        banned.add(201465)  # Weirich Waldburg
                sets = set([VTES_ABBREV_TO_SET[a] for a in sets])
                if not sets and not allowed:
                    self.tournament.include = []
                    self.tournament.exclude = banned
                else:
                    cards = [c.id for c in krcg.vtes.VTES if set(c.sets.keys()) & sets]
                    cards += allowed
                    cards = sorted(set(cards) - banned)
                    self.tournament.include = cards
                    self.tournament.exclude = []
                if sets:
                    description += (
                        "- The following sets are included:\n"
                        + "\n".join(f"  - {s}" for s in sets)
                        + "\n"
                    )
                if allowed and len(allowed) < 20:
                    description += (
                        f"- {'These additional' if sets else 'The following'} cards "
                        "are included:\n"
                        + "\n".join(f"  - {krcg.vtes.VTES[c].name}" for c in allowed)
                        + "\n"
                    )
                elif allowed:
                    description += (
                        f"- {len(allowed)}{' additional' if sets else ''} "
                        "cards are included\n"
                    )
                if banned and len(banned) < 20:
                    description += (
                        "- The following cards are excluded:\n"
                        + "\n".join(f"  - {krcg.vtes.VTES[c].name}" for c in banned)
                        + "\n"
                    )
                elif banned:
                    description += f"- {len(banned)} cards are excluded\n"
        else:
            self.vdb_format = {}
        if ban_list:
            deck = krcg.deck.Deck.from_txt(io.StringIO(data), preface=False)
            cards = [c.id for c in deck]
            self.vdb_format = {}
            self.tournament.include = []
            self.tournament.exclude = cards
            if cards and len(cards) < 20:
                description += (
                    "- The following cards are excluded:\n"
                    + "\n".join(f"  - {c.name}" for c in deck)
                    + "\n"
                )
            elif cards:
                description += f"- {len(cards)} cards are excluded\n"
        if single_clan is not None:
            if single_clan:
                self.tournament.flags |= tournament.TournamentFlag.SINGLE_CLAN
                description += "- The crypt must have a single main clan at 75%\n"
            else:
                self.tournament.flags &= ~tournament.TournamentFlag.SINGLE_CLAN
        if single_vampire is not None:
            if single_vampire:
                self.tournament.flags |= tournament.TournamentFlag.SINGLE_VAMPIRE
                description += "- The crypt must have a single vampire\n"
            else:
                self.tournament.flags &= ~tournament.TournamentFlag.SINGLE_VAMPIRE
        # Recheck existing decks
        rejected_decks = []
        for player in self.tournament.players.values():
            if player.deck:
                deck = krcg.deck.Deck()
                deck.from_json(player.deck)
                if self.tournament.check_deck(deck):
                    player.deck = {}
                    rejected_decks.append(player.vekn)
        await self.update()
        comp = []
        if self.vdb_format:
            comp = [
                self.bot.rest.build_message_action_row().add_interactive_button(
                    hikari.ButtonStyle.PRIMARY,
                    "vdb-format",
                    label="Download VDB format",
                )
            ]
            COMPONENTS["vdb-format"] = DownloadVDBFormat
        if rejected_decks:
            description += "\n⚠️ **Some players had their deck invalidated:**\n" + (
                "\n".join(f"- {self._player_display(vekn)}" for vekn in rejected_decks)
            )
        if description:
            title = "Limited format defined"
        else:
            title = "Limits removed: Standard format"
            description = "This tournament uses the Standard VEKN format."
        embeds = _paginate_embed(hikari.Embed(title=title, description=description))
        await self.create_or_edit_response(
            embeds=embeds,
            components=comp,
        )
        next_step = Announce.copy_from_interaction(self)
        await next_step()


class CloseTournament(BaseCommand):
    """Delete all roles and channels, mark as closed in DB (confirmation required)"""

    UPDATE = db.UpdateLevel.READ_ONLY
    ACCESS = CommandAccess.JUDGE
    DESCRIPTION = "JUDGE: Close the tournament"
    OPTIONS = []

    async def __call__(self) -> None:
        """Ask for confirmation, display confirm and cancel buttons"""
        if not self.tournament:
            raise CommandFailed("No tournament going on here")
        confirmation = (
            self.bot.rest.build_message_action_row()
            .add_interactive_button(
                hikari.ButtonStyle.DANGER, "confirm-close", label="Close tournament"
            )
            .add_interactive_button(
                hikari.ButtonStyle.SECONDARY, "cancel-close", label="Cancel"
            )
        )

        COMPONENTS["confirm-close"] = CloseTournament.Confirmed
        COMPONENTS["cancel-close"] = CloseTournament.Cancel
        await self.create_or_edit_response(
            embed=hikari.Embed(
                title="Are you sure?",
                description=(
                    "This will definitely close all tournament channels.\n"
                    "Make sure you downloaded the tournament reports "
                    f"({DownloadReports.mention()})"
                ),
            ),
            components=[confirmation],
            flags=hikari.MessageFlag.EPHEMERAL,
        )

    class Confirmed(BaseComponent):
        """When the confirm button is hit"""

        UPDATE = db.UpdateLevel.EXCLUSIVE_WRITE

        async def __call__(self) -> None:
            await self.deferred()
            all_channels = self.discord.all_channels_ids()
            if self.channel_id in all_channels:
                raise CommandFailed(
                    "This command can only be issued in the top level channel "
                    "(where you opened the tournament)."
                )
            # remove root judge role from the judges
            # beware to keep it if they are also judges in another running tournament
            root_judge = self.discord.roles.get(Role.ROOT_JUDGE)
            if root_judge:
                root_judge_remove = []
                judges = await asyncio.gather(
                    *(
                        self.bot.rest.fetch_member(self.guild_id, judge)
                        for judge in self.discord.judges
                    )
                )
                guild_roles = await self.bot.rest.fetch_roles(self.guild_id)
                guild_roles = {r.id: r for r in guild_roles}
                for judge in judges:
                    if any(
                        rid
                        for rid in judge.role_ids
                        if rid in guild_roles
                        and guild_roles[rid].name.endswith("-Judge")
                    ):
                        continue
                    root_judge_remove.append(judge.id)
                await asyncio.gather(
                    *(
                        self.bot.rest.remove_role_from_member(
                            self.guils_id, judge_id, root_judge.id
                        )
                        for judge_id in root_judge_remove
                    )
                )
                # do not delete the ROOT_JUDGE role from discord
                self.discord.roles.pop(Role.ROOT_JUDGE)
            # delete tournament channels and roles
            results = await asyncio.gather(
                *(
                    self.bot.rest.delete_channel(channel_id)
                    for channel_id in all_channels
                ),
                return_exceptions=True,
            )
            results.extend(
                await asyncio.gather(
                    *(
                        self.bot.rest.delete_role(self.guild_id, role.id)
                        for role in self.discord.roles.values()
                    ),
                    return_exceptions=True,
                )
            )
            self.discord.channels.clear()
            self.discord.roles.clear()
            await self.update()
            await db.close_tournament(self.connection, self.guild_id, self.category_id)
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

    class Cancel(BaseComponent):
        """When the cancel button is hit"""

        UPDATE = db.UpdateLevel.READ_ONLY

        async def __call__(self):
            COMPONENTS.pop("cancel-close", None)
            await self.create_or_edit_response(
                "Cancelled",
                flags=hikari.MessageFlag.EPHEMERAL,
                components=[],
                embeds=[],
            )


class Register(BaseCommand):
    """Registration (auto-check-in if the check-in is open).

    The same class and code is used for CheckIn.
    """

    UPDATE = db.UpdateLevel.WRITE
    DESCRIPTION = "Register for this tournament"
    OPTIONS = [
        hikari.CommandOption(
            type=hikari.OptionType.STRING,
            name="vekn",
            description="Your VEKN ID#",
            is_required=False,
        ),
        hikari.CommandOption(
            type=hikari.OptionType.STRING,
            name="name",
            description="Your name",
            is_required=False,
        ),
    ]

    async def __call__(
        self,
        vekn: Optional[str] = None,
        name: Optional[str] = None,
    ) -> None:
        if not self.tournament:
            raise CommandFailed("No tournament in progress")
        await self.deferred(flags=hikari.MessageFlag.EPHEMERAL)
        name = name and name[:4096]
        discord_id = self.author.id
        prev_vekn = None
        if vekn:
            other_discord = self.discord.get_discord_id(vekn)
            if other_discord and other_discord != discord_id:
                raise CommandFailed(
                    "Another player has already registered with this ID"
                )
            other_vekn = self.discord.get_vekn(discord_id)
            if other_vekn and other_vekn != vekn:
                prev_vekn = other_vekn
        else:
            vekn = self.discord.get_vekn(discord_id)
        player = await self.tournament.add_player(
            vekn=vekn, prev_vekn=prev_vekn, name=name, judge=False
        )
        self.discord.players[discord_id] = player.vekn
        await self.bot.rest.add_role_to_member(
            self.guild_id,
            discord_id,
            self.discord.roles[Role.PLAYER].id,
            reason=self.reason,
        )
        description = (
            "You have successfully registered for the tournament as: \n"
            f"- {self._player_display(player.vekn)}\n\n"
        )
        if player.playing:
            description = "You are ready to play."
        elif (
            self.tournament.flags & tournament.TournamentFlag.DECKLIST_REQUIRED
            and not player.deck
        ):
            description += (
                "\n**Deck list required**\n"
                "A decklist is required to participate, please use "
                f"{UploadDeck.mention()} to provide one before "
                "the tournament begins.\n"
            )
        else:
            description += (
                "\n**Check-in required**\n"
                "Please note you will need to confirm your presence by "
                f"using {CheckIn.mention()} before the next round begins.\n"
            )
        description += f"\nUse {Status.mention()} anytime to check your status."
        await self.update()
        await self.create_or_edit_response(
            embed=hikari.Embed(
                title="Registered",
                description=description,
            ),
            flags=hikari.MessageFlag.EPHEMERAL,
            components=[],
        )


class RegisterPlayer(BaseCommand):
    """Register another player (for judges). Also useful for offline tournaments."""

    UPDATE = db.UpdateLevel.WRITE
    ACCESS = CommandAccess.JUDGE
    DESCRIPTION = "JUDGE: Register a player for this tournament"
    OPTIONS = [
        hikari.CommandOption(
            type=hikari.OptionType.STRING,
            name="vekn",
            description="VEKN ID#",
            is_required=False,
        ),
        hikari.CommandOption(
            type=hikari.OptionType.STRING,
            name="name",
            description="Player name",
            is_required=False,
        ),
        hikari.CommandOption(
            type=hikari.OptionType.USER,
            name="user",
            description="User to register (if any)",
            is_required=False,
        ),
    ]

    async def __call__(
        self,
        vekn: Optional[str] = None,
        name: Optional[str] = None,
        decklist: Optional[str] = None,
        user: Optional[hikari.Snowflake] = None,
    ) -> None:
        await self.deferred()
        name = name and name[:4096]
        prev_vekn = None
        if decklist:
            deck = krcg.deck.Deck.from_url(decklist)
        if user:
            if vekn:
                other_discord = self.discord.get_discord_id(vekn)
                if other_discord and other_discord != user:
                    raise CommandFailed(
                        "Another player has already registered with this VEKN#.\n"
                        "Change their VEKN# first."
                    )
                other_vekn = self.discord.get_vekn(user)
                if other_vekn and other_vekn != vekn:
                    prev_vekn = other_vekn
            else:
                vekn = self.discord.get_vekn(user)
        player = await self.tournament.add_player(
            vekn=vekn, prev_vekn=prev_vekn, name=name, judge=True
        )
        if user:
            self.discord.players[user] = player.vekn
            await self.bot.rest.add_role_to_member(
                self.guild_id,
                user,
                self.discord.roles[Role.PLAYER].id,
                reason=self.reason,
            )
        await self.update()
        player_display = self._player_display(player.vekn)
        description = f"{player_display} is successfully registered for the tournament."
        if player.playing:
            description = f"{player_display} is ready to play."
        elif (
            self.tournament.flags & tournament.TournamentFlag.DECKLIST_REQUIRED
            and not player.deck
        ):
            description += (
                "\n\n**Deck list required**\n"
                "A decklist is required to participate, please use "
                f"{UploadDeckFor.mention} to provide one before the tournament begins."
            )
        elif (
            self.tournament.max_rounds
            and self.player_rounds_played(player) >= self.max_rounds
        ):
            description += (
                "\n\n**Maximum number of rounds**\n"
                f"{self._player_display(player.vekn)} has played the maximum number "
                "of rounds."
            )
        else:
            if self.tournament.state == tournament.TournamentState.PLAYING:
                description += (
                    "\n\n**Current round**\n"
                    "You can add the player to the current round if you find a spot "
                    "for them on a table that has not yet begun to play by using "
                    f"{Round.mention('add')}."
                )
            else:
                description += (
                    "\n\n**Check-in required**\n"
                    "The user will need to confirm their presence with the "
                    f"{CheckIn.mention()} command before next round begins.\n"
                )
        await self.create_or_edit_response(
            embed=hikari.Embed(
                title="Player registered",
                description=description,
            ),
        )


class CheckIn(BaseCommand):
    """Check in. Judges use the Register command"""

    UPDATE = db.UpdateLevel.WRITE
    ACCESS = CommandAccess.PLAYER
    DESCRIPTION = "Check-in to play the next round"
    OPTIONS = []

    async def __call__(self) -> None:
        if not self.tournament:
            raise CommandFailed("No tournament in progress")
        vekn = self.discord.get_vekn(self.author.id)
        if not vekn:
            await self.create_or_edit_response(
                f"You are not registered for this tournament: use {Register.mention()}."
            )
            return
        status = self.tournament.player_check_in(vekn=vekn)
        if status == tournament.PlayerStatus.CHECKED_IN:
            title = "Registered"
            description = "You are ready to play."
        elif status == tournament.PlayerStatus.PLAYING:
            title = "Playing"
            description = "You are already playing."
        elif status == tournament.PlayerStatus.MISSING_DECK:
            title = "⚠️ Deck list required"
            description = (
                "A decklist is required to participate, please use "
                f"{UploadDeck.mention()} to provide one before "
                "the tournament begins.\n"
            )
        elif status == tournament.PlayerStatus.MAX_ROUNDS_PLAYED:
            title = "⚠️ Maximum number of rounds played"
            description = (
                "You played the maximum number of rounds and cannot play another.\n"
            )
        elif status == tournament.PlayerStatus.CHECKED_OUT:
            title = "⚠️ Checked out"
            description = (
                f"Only a <@&{self.discord.roles[Role.JUDGE].id}> can check you in, "
                "reach out to them."
            )
        description += f"\nUse {Status.mention()} anytime to check your status."
        await self.update()
        await self.create_or_edit_response(
            embed=hikari.Embed(
                title=title,
                description=description,
            ),
            flags=hikari.MessageFlag.EPHEMERAL,
            components=[],
            role_mentions=[self.discord.roles[Role.JUDGE].id],
        )


class BatchRegister(BaseCommand):
    """Register a decklist"""

    UPDATE = db.UpdateLevel.WRITE
    ACCESS = CommandAccess.JUDGE
    DESCRIPTION = "Upload a players CSV — (VEKN, name, decklist url) in this order"
    OPTIONS = [
        hikari.CommandOption(
            type=hikari.OptionType.ATTACHMENT,
            name="file",
            description="Deck list text file",
            is_required=True,
        ),
    ]

    async def __call__(
        self,
        file: hikari.Snowflake,
    ) -> None:
        await self.deferred(flags=hikari.MessageFlag.EPHEMERAL)
        actual_file = self.interaction.resolved.attachments[file]
        data = await actual_file.read()
        data = data.decode(encoding=chardet.detect(data)["encoding"])
        sniffer = csv.Sniffer()
        dialect = sniffer.sniff(data, ",;\t|")
        data = data.splitlines()[1 if sniffer.has_header(data) else 0 :]
        try:
            players = [
                [vekn, name, url]
                for vekn, name, url in csv.reader(data, dialect=dialect)
            ]
        except csv.Error as e:
            await self.create_or_edit_response(f"Invalid format: {e.args}")
            return
        except ValueError:
            await self.create_or_edit_response(
                "Invalid columns: exactly 3 columns are required for each line"
                "(vekn, name, decklist_url)."
            )
            return
        await asyncio.gather(
            *(
                self.tournament.add_player(vekn=vekn, name=name, judge=True)
                for vekn, name, _ in players
            )
        )
        for vekn, _, url in players:
            if url:
                deck = krcg.deck.Deck.from_url(url)
                self.tournament.add_player_deck(vekn=vekn, deck=deck)
        await self.update()
        await self.create_or_edit_response(
            embed=hikari.Embed(
                title="File uploaded",
                description=f"Use {PlayersList.mention()} to diplay the players list.",
            ),
            flags=hikari.MessageFlag.EPHEMERAL,
        )


class OpenCheckIn(BaseCommand):
    """Open the check-in so players can join the incoming round."""

    UPDATE = db.UpdateLevel.WRITE
    ACCESS = CommandAccess.JUDGE
    DESCRIPTION = "JUDGE: Open check-in to players for next round"
    OPTIONS = []

    async def __call__(self) -> None:
        self.tournament.open_checkin()
        await self.update()
        await self.create_or_edit_response("Check-in is open")


class Drop(BaseCommand):
    """Drop from the tournament."""

    UPDATE = db.UpdateLevel.WRITE
    ACCESS = CommandAccess.PLAYER
    DESCRIPTION = "Drop from the tournament"
    OPTIONS = []

    async def __call__(self) -> None:
        vekn = self.discord.get_vekn(self.author.id)
        await self.bot.rest.remove_role_from_member(
            self.guild_id,
            self.author,
            self.discord.roles[Role.PLAYER].id,
            reason=self.reason,
        )
        self.tournament.drop(vekn)
        await self.update()
        await self.create_or_edit_response(
            "Dropped",
            flags=hikari.MessageFlag.EPHEMERAL,
        )


class DropPlayer(BaseCommand):
    """Drop a player. Remove him from the list if the tournament has not started."""

    UPDATE = db.UpdateLevel.WRITE
    ACCESS = CommandAccess.JUDGE
    DESCRIPTION = "JUDGE: Remove a player from tournament (not a disqualification)"
    OPTIONS = [
        hikari.CommandOption(
            type=hikari.OptionType.USER,
            name="user",
            description="user to drop",
            is_required=False,
        ),
        hikari.CommandOption(
            type=hikari.OptionType.STRING,
            name="vekn",
            description="user to drop",
            is_required=False,
        ),
    ]

    async def __call__(
        self, user: Optional[hikari.Snowflake] = None, vekn: Optional[str] = None
    ) -> None:
        if user:
            vekn = vekn or self.discord.get_vekn(user)
            await self.bot.rest.remove_role_from_member(
                self.guild_id,
                user,
                self.discord.roles[Role.PLAYER].id,
                reason=self.reason,
            )
        self.tournament.drop(vekn)
        await self.update()
        await self.create_or_edit_response("Dropped")  # cannot display them anymore


class Disqualify(BaseCommand):
    """Disqualify a player. Only a Judge can re-register them."""

    UPDATE = db.UpdateLevel.WRITE
    ACCESS = CommandAccess.JUDGE
    DESCRIPTION = "JUDGE: Disqualify a player from the tournament"
    OPTIONS = [
        hikari.CommandOption(
            type=hikari.OptionType.USER,
            name="user",
            description="user to disqualify",
            is_required=False,
        ),
        hikari.CommandOption(
            type=hikari.OptionType.STRING,
            name="vekn",
            description="user to disqualify",
            is_required=False,
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
        self,
        user: Optional[hikari.Snowflake] = None,
        vekn: Optional[str] = None,
        note: Optional[str] = None,
    ) -> None:
        vekn = vekn or self.discord.get_vekn(user)
        player_display = self._player_display(vekn)
        self.tournament.drop(vekn, reason=tournament.DropReason.DISQUALIFIED)
        if note:
            self.tournament.note(
                vekn, self.author.id, tournament.NoteLevel.WARNING, note
            )
        await self.update()
        await self.create_or_edit_response(
            f"{player_display} Disqualified",
            user_mentions=[user] if user else [],
        )


class UploadDeck(BaseCommand):
    """Register a decklist"""

    UPDATE = db.UpdateLevel.WRITE
    ACCESS = CommandAccess.PLAYER
    DESCRIPTION = "Upload your decklist (use no arguments to provide your list as text)"
    OPTIONS = [
        hikari.CommandOption(
            type=hikari.OptionType.STRING,
            name="url",
            description="VDB or Amaranth URL",
            is_required=False,
        ),
        hikari.CommandOption(
            type=hikari.OptionType.ATTACHMENT,
            name="file",
            description="Deck list text file",
            is_required=False,
        ),
    ]

    async def __call__(
        self,
        vekn: Optional[str] = None,
        judge: Optional[bool] = False,
        url: Optional[str] = None,
        file: Optional[hikari.Snowflake] = None,
    ) -> None:
        vekn = vekn or self.discord.get_vekn(self.author.id)
        if not vekn:
            await self.create_or_edit_response(
                f"You are not registered for this tournament. Use {Register.mention()}",
                flags=hikari.MessageFlag.EPHEMERAL,
            )
            return
        if url:
            deck = krcg.deck.Deck.from_url(url)
            await self.check_and_add_deck(vekn, deck, judge)
            return
        if file:
            actual_file = self.interaction.resolved.attachments[file]
            data = await actual_file.read()
            deck = krcg.deck.Deck.from_txt(
                io.StringIO(data.decode(encoding=chardet.detect(data)["encoding"]))
            )
            deck.name = deck.name or actual_file.filename.split(".")[0]
            await self.check_and_add_deck(vekn, deck, judge)
            return
        component = self.bot.rest.build_modal_action_row().add_text_input(
            "decklist",
            "Deck list",
            style=hikari.TextInputStyle.PARAGRAPH,
            placeholder=("Your deck list in any reasonably standard text format"),
        )
        custom_id = f"modal-deck-{self.author.id}"
        COMPONENTS[custom_id] = partialclass(UploadDeck.DeckList, vekn, judge)
        await self.interaction.create_modal_response(
            "Register deck", custom_id, component
        )

    async def check_and_add_deck(self, vekn, deck: krcg.deck.Deck, judge: bool) -> None:
        issues = self.tournament.check_deck(deck)
        if issues:
            comp = []
            if self.tournament.is_limited() and self.vdb_format:
                comp = [
                    self.bot.rest.build_message_action_row().add_interactive_button(
                        hikari.ButtonStyle.PRIMARY,
                        "vdb-format",
                        label="Download VDB format",
                    )
                ]
                COMPONENTS["vdb-format"] = DownloadVDBFormat
            description = ""
            for issue in issues:
                if isinstance(issue, tournament.DeckIssue.BannedCards):
                    description += (
                        "- Banned cards:\n"
                        + "\n".join(f"  - {c}" for c in issue.cards)
                        + "\n"
                    )
                elif isinstance(issue, tournament.DeckIssue.ExcludedCards):
                    description += (
                        "- Excluded cards:\n"
                        + "\n".join(f"  - {c}" for c in issue.cards)
                        + "\n"
                    )
                else:
                    description += f"- {issue}\n"
            await self.create_or_edit_response(
                embed=hikari.Embed(
                    title="⚠️ **Invalid Deck**",
                    description=description,
                ),
                components=comp,
                flags=hikari.MessageFlag.EPHEMERAL,
            )
            return
        self.tournament.add_player_deck(vekn, deck=deck, judge=judge)
        await self.update()
        await self.create_or_edit_response(
            "Decklist copied. Note that if you make changes, "
            "you need to upload it again.",
            flags=hikari.MessageFlag.EPHEMERAL,
        )

    class DeckList(BaseModal):
        UPDATE = db.UpdateLevel.WRITE

        def __init__(
            self,
            vekn: str,
            judge: bool,
            *args,
            **kwargs,
        ):
            self.vekn = vekn
            self.judge = judge
            super().__init__(*args, **kwargs)

        async def __call__(self, decklist: str):
            logger.info("Received decklist: %s", decklist)
            deck = krcg.deck.Deck.from_txt(io.StringIO(decklist))
            deck.name = deck.name or "No Name"
            if not deck:
                await self.create_or_edit_response(
                    f"**Error: Invalid decklist**",
                    flags=hikari.MessageFlag.EPHEMERAL,
                )
                return
            # hmpf, dirty workaround, we need better abstract class work overall
            await UploadDeck.check_and_add_deck(self, self.vekn, deck, self.judge)


class UploadDeckFor(UploadDeck):
    ACCESS = CommandAccess.JUDGE
    DESCRIPTION = "Upload a decklist (use no arguments to provide the list as text)"
    OPTIONS = [
        hikari.CommandOption(
            type=hikari.OptionType.USER,
            name="user",
            description="player whose deck it is",
            is_required=False,
        ),
        hikari.CommandOption(
            type=hikari.OptionType.STRING,
            name="vekn",
            description="player whose deck it is",
            is_required=False,
        ),
        hikari.CommandOption(
            type=hikari.OptionType.STRING,
            name="url",
            description="VDB or Amaranth URL",
            is_required=False,
        ),
        hikari.CommandOption(
            type=hikari.OptionType.ATTACHMENT,
            name="file",
            description="Deck list text file",
            is_required=False,
        ),
    ]

    async def __call__(
        self,
        user: Optional[hikari.Snowflake] = None,
        vekn: Optional[str] = None,
        url: Optional[str] = None,
        file: Optional[hikari.Snowflake] = None,
    ) -> None:
        vekn = vekn or self.discord.get_vekn(user)
        if vekn not in self.tournament.players:
            await self.create_or_edit_response(
                f"Player not registered for this tournament. "
                f"Use {RegisterPlayer.mention()} first.",
                flags=hikari.MessageFlag.EPHEMERAL,
            )
            return
        await super().__call__(vekn=vekn, judge=True, url=url, file=file)


class Appoint(BaseCommand):
    """Appoint Judges, bots and spectators for channels access.

    Judges might not have role management permissions on a server.
    """

    UPDATE = db.UpdateLevel.WRITE
    ACCESS = CommandAccess.JUDGE
    DESCRIPTION = "JUDGE: Appoint judges, bots and spectators"
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
    ) -> None:
        await self.deferred(flags=hikari.MessageFlag.EPHEMERAL)
        if role in ["JUDGE", "BOT"]:
            self.discord.judges.append(user)
            await asyncio.gather(
                *(
                    self.bot.rest.add_role_to_member(
                        self.guild_id,
                        user,
                        self.discord.roles[Role.JUDGE].id,
                        reason=self.reason,
                    ),
                    self.bot.rest.add_role_to_member(
                        self.guild_id,
                        user,
                        self.discord.roles[Role.ROOT_JUDGE].id,
                        reason=self.reason,
                    ),
                )
            )
        else:
            self.discord.spectators.append(user)
            await self.bot.rest.add_role_to_member(
                self.guild_id,
                user,
                self.discord.roles[Role.SPECTATOR].id,
                reason=self.reason,
            )
        await self.create_or_edit_response(
            f"Appointed <@{user}> as {role}",
            flags=hikari.MessageFlag.EPHEMERAL,
        )


class Round(BaseCommand):
    """Handle rounds.

    start: start a round with checked-in players
    finish: finish a round (checks for VPs consistency)
    reset: cancel the round and seating
    add: add a player on a table where the game has not started yet
    remove: remove a player from a table where the game has not started yet
    """

    UPDATE = db.UpdateLevel.EXCLUSIVE_WRITE
    ACCESS = CommandAccess.JUDGE
    DESCRIPTION = "JUDGE: Handle rounds"
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
            options=[
                hikari.CommandOption(
                    type=hikari.OptionType.BOOLEAN,
                    name="keep_checkin",
                    description="Keep current check-in state despite configuration",
                    is_required=False,
                )
            ],
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
                    type=hikari.OptionType.INTEGER,
                    name="table",
                    description="Table number to add the user to",
                    is_required=True,
                    min_value=1,
                ),
                hikari.CommandOption(
                    type=hikari.OptionType.USER,
                    name="user",
                    description="The user to add to the round",
                    is_required=False,
                ),
                hikari.CommandOption(
                    type=hikari.OptionType.STRING,
                    name="vekn",
                    description="The user ID to add to the round",
                    is_required=False,
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
                    description="The user to remove from the round",
                    is_required=False,
                ),
                hikari.CommandOption(
                    type=hikari.OptionType.STRING,
                    name="vekn",
                    description="The user ID to remove from the round",
                    is_required=False,
                ),
            ],
        ),
    ]

    async def __call__(self, *args, **kwargs) -> None:
        """Call subcommand (start, finish, reset, add, remove)"""
        logger.debug("%s | %s", args, kwargs)
        for option in self.interaction.options or []:
            await getattr(self, option.name)(
                **{subopt.name: subopt.value for subopt in (option.options or [])}
            )

    async def _progress(self, step, **kwargs) -> None:
        """Progress bar for the start subcommand"""
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

    async def _display_seating(self, table_num) -> None:
        """Display the seating in the table channel."""
        table = self.tournament.rounds[-1].seating[table_num - 1]
        voice_channel = self.discord.get_table_voice_channel(table_num).id
        embed = hikari.Embed(
            title=f"Table {table_num} seating",
            description="\n".join(
                f"{j}. {self._player_display(p)}" for j, p in enumerate(table, 1)
            )
            + "\n\nThe first player should create the table.",
        )
        embed.add_field(
            name="Start the timer",
            value="`/timer start hours:2`",
            inline=True,
        )
        embed.set_thumbnail(hikari.UnicodeEmoji("🪑"))
        await self.bot.rest.create_message(voice_channel, embed=embed)

    async def start(self) -> None:
        """Start a round. Dynamically optimise seating to follow official VEKN rules.

        Assign roles and create text and voice channels for players.
        """
        players_count = len([p for p in self.tournament.players.values() if p.playing])
        if players_count in [6, 7, 11] and not (
            self.tournament.flags & tournament.TournamentFlag.STAGGERED
        ):
            await self.create_or_edit_response(
                embed=hikari.Embed(
                    title="Wrong players count",
                    description=(
                        f"You cannot have a standard tournament with {players_count} "
                        f"players. Add or drop players, or use {Stagger.mention()} "
                        "to make this a staggered tournament."
                    ),
                )
            )
            return
        await self.create_or_edit_response(
            embed=hikari.Embed(
                title="Seating players...",
                description="_" * 20,
            )
        )
        round, score = await self.tournament.start_round(self._progress)
        await self.create_or_edit_response(
            embed=hikari.Embed(
                title="Assigning tables...",
                description="Table channels are being opened and roles assigned",
            )
        )
        await self._align_roles()
        await self._align_channels()
        await self.update()
        await asyncio.gather(
            *(self._display_seating(i + 1) for i in range(round.seating.tables_count()))
        )
        embed = hikari.Embed(
            title=f"Round {self.tournament.current_round} Seating",
        )
        for i, table in enumerate(round.seating.iter_tables(), 1):
            embed.add_field(
                name=f"Table {i}",
                value="\n".join(
                    f"{j}. {self._player_display(vekn)}"
                    for j, vekn in enumerate(table, 1)
                ),
                inline=True,
            )
        embed.set_thumbnail(hikari.UnicodeEmoji("🪑"))
        embed.set_author(
            name="See seating criteria",
            url=(
                "https://groups.google.com/g/"
                "rec.games.trading-cards.jyhad/c/4YivYLDVYQc/m/CCH-ZBU5UiUJ"
            ),
        )
        if score:
            embed.set_footer(
                "Seating score: "
                + ", ".join(f"R{i}: {v:.2g}" for i, v in enumerate(score.rules, 1))
            )
        await self.create_or_edit_response(
            embeds=_paginate_embed(embed),
            user_mentions=True,
        )

    async def _delete_round_tables(self, finals: bool = False) -> None:
        """Delete table channels and roles used for the round."""
        await self._align_roles(silence_exceptions=True)
        await self._align_channels(silence_exceptions=True)

    async def finish(self, keep_checkin: bool = False) -> None:
        """Finish round (checks scores consistency)"""
        await self.deferred()
        round = self.tournament.finish_round(keep_checkin)
        await self._delete_round_tables(round.finals)
        await self.update()
        await self.create_or_edit_response("Round finished")

    async def reset(self) -> None:
        """Rollback round"""
        await self.deferred()
        round = self.tournament.reset_round()
        await self._delete_round_tables(round.finals)
        await self.update()
        await self.create_or_edit_response("Round reset")

    async def add(
        self,
        table: int,
        user: Optional[hikari.Snowflake] = None,
        vekn: Optional[str] = None,
    ) -> None:
        """Add player to a 4-players table"""
        await self.deferred()
        vekn = vekn or self.discord.get_vekn(user)
        self.tournament.round_add(vekn, table)
        if user:
            await self.bot.rest.add_role_to_member(
                self.guild_id,
                user,
                self.discord.roles[Role.PLAYER].id,
                reason=self.reason,
            )
        await self._display_seating(table)
        await self.update()
        await self.create_or_edit_response(f"Player added to table {table}")

    async def remove(
        self, user: Optional[hikari.Snowflake] = None, vekn: Optional[str] = None
    ) -> None:
        """Remove player from a 5-players table"""
        await self.deferred()
        vekn = vekn or self.discord.get_vekn(user)
        table = self.tournament.round_remove(vekn)
        if user:
            await self.bot.rest.remove_role_from_member(
                self.guild_id,
                user,
                self.discord.roles[Role.PLAYER].id,
                reason=self.reason,
            )
        await self._display_seating(table)
        await self.update()
        await self.create_or_edit_response(f"Player removed from table {table}")


class Finals(BaseCommand):
    """Start finals (auto toss for a spot in case of points draw)."""

    UPDATE = db.UpdateLevel.EXCLUSIVE_WRITE
    ACCESS = CommandAccess.JUDGE
    DESCRIPTION = "JUDGE: Start the finals"
    OPTIONS = []

    async def __call__(self) -> None:
        round = self.tournament.start_finals()
        table = round.seating[0]
        await self.create_or_edit_response(
            embed=hikari.Embed(
                title="Creating channels...",
                description="Finals channels are being opened and roles assigned",
            )
        )
        await self._align_roles()
        await self._align_channels()
        await self.update()
        seeding_embed = hikari.Embed(
            title="Finals seeding",
            description="\n".join(
                f"{j}. {self._player_display(p)}" for j, p in enumerate(table, 1)
            ),
        )
        await self.bot.rest.create_message(
            self.discord.get_table_voice_channel(1).id,
            embed=seeding_embed,
        )
        await self.create_or_edit_response(
            embed=seeding_embed,
            user_mentions=True,
        )


class Report(BaseCommand):
    """Report number of VPs scored"""

    UPDATE = db.UpdateLevel.WRITE
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

    async def __call__(self, vp: float) -> None:
        if self.tournament.state != tournament.TournamentState.PLAYING:
            raise CommandFailed("Scores can only be reported when a round is ongoing")
        vekn = self.discord.get_vekn(self.author.id)
        self.tournament.report(vekn, vp)
        await self.update()
        info = self.tournament.player_info(vekn)
        if not info.table:
            return
        embed = hikari.Embed(
            title="Game report",
            description=(
                f"{self._player_display(vekn)} has reported "
                f"{vp:.2g}VP{'s' if vp > 1 else ''}"
            ),
        )
        channel_id = self.discord.get_table_voice_channel(info.table).id
        await self.bot.rest.create_message(channel_id, embed=embed)
        await self.create_or_edit_response(
            content="Result registered", flags=hikari.MessageFlag.EPHEMERAL
        )


class FixReport(BaseCommand):
    """Fix a VP score on any table, any round."""

    UPDATE = db.UpdateLevel.WRITE
    ACCESS = CommandAccess.JUDGE
    DESCRIPTION = "JUDGE: Fix a VP score"
    OPTIONS = [
        hikari.CommandOption(
            type=hikari.OptionType.FLOAT,
            name="vp",
            description="Number of VPs won",
            is_required=True,
            min_value=0,
            max_value=5,
        ),
        hikari.CommandOption(
            type=hikari.OptionType.USER,
            name="user",
            description="User whose result should be changed",
            is_required=False,
        ),
        hikari.CommandOption(
            type=hikari.OptionType.STRING,
            name="vekn",
            description="User ID whose result should be changed",
            is_required=False,
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
        vp: float,
        round: Optional[int] = None,
        user: Optional[hikari.Snowflake] = None,
        vekn: Optional[str] = None,
    ) -> None:
        vekn = vekn or self.discord.get_vekn(user)
        self.tournament.report(vekn, vp, round)
        await self.update()
        await self.create_or_edit_response(
            content=(
                f"Result registered: {vp:.2g} VPs for {self._player_display(vekn)}"
            ),
            flags=hikari.UNDEFINED
            if self._is_judge_channel()
            else hikari.MessageFlag.EPHEMERAL,
        )
        if round is not None:
            return
        info = self.tournament.player_info(vekn)
        if not info.table:
            return
        embed = hikari.Embed(
            title="Game report",
            description=(
                f"A judge has reported {vp:.2g}VP{'s' if vp > 1 else ''} for "
                f"{self._player_display(vekn)}"
            ),
        )
        channel_id = self.discord.get_table_voice_channel(info.table).id
        await self.bot.rest.create_message(channel_id, embed=embed)


class ValidateScore(BaseCommand):
    """Validate an odd VP situation (inconsistent score due to a judge ruling)"""

    UPDATE = db.UpdateLevel.WRITE
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

    async def __call__(
        self, table: int, note: str, round: Optional[int] = None
    ) -> None:
        self.tournament.validate_score(table, self.author.id, note, round)
        await self.update()
        await self.create_or_edit_response(
            content=f"Score validated for table {table}: {note}",
            flags=hikari.UNDEFINED
            if self._is_judge_channel()
            else hikari.MessageFlag.EPHEMERAL,
        )


def note_level_int(level: tournament.NoteLevel) -> int:
    """Note level as int. The higher the penalty, the higher the int."""
    return list(tournament.NoteLevel.__members__.values()).index(level)


def note_level_str(level: tournament.NoteLevel) -> str:
    """Note level label"""
    return {
        tournament.NoteLevel.OVERRIDE: "Override",
        tournament.NoteLevel.NOTE: "Note",
        tournament.NoteLevel.CAUTION: "Caution",
        tournament.NoteLevel.WARNING: "Warning",
    }[level]


def notes_by_level(notes: Iterable[tournament.Note]) -> List[List[tournament.Note]]:
    """Group notes by level"""
    ret = []
    notes = sorted(notes, key=lambda n: note_level_int(n.level))
    for _, level_notes in itertools.groupby(
        notes, key=lambda n: note_level_int(n.level)
    ):
        level_notes = list(level_notes)
        ret.append(list(level_notes))
    return ret


def partialclass(cls, *args, **kwds):
    """Useful util to pass some attributes to a subclass."""

    class NewCls(cls):
        __init__ = functools.partialmethod(cls.__init__, *args, **kwds)

    return NewCls


class Note(BaseCommand):
    """Allow a Judge to take a note on or deliver a caution or warning to a player.

    If previous notes have been taken on this player,
    ask the judge to review them and potentially adapt their note level
    (upgrade to caution, warning or disqualification).
    """

    UPDATE = db.UpdateLevel.READ_ONLY
    ACCESS = CommandAccess.JUDGE
    DESCRIPTION = "JUDGE: Take a note on a player, or deliver a caution or warning"
    OPTIONS = [
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
        hikari.CommandOption(
            type=hikari.OptionType.USER,
            name="user",
            description="User whose result should be changed",
            is_required=False,
        ),
        hikari.CommandOption(
            type=hikari.OptionType.STRING,
            name="vekn",
            description="User ID whose result should be changed",
            is_required=False,
        ),
    ]

    async def __call__(
        self,
        level: tournament.NoteLevel,
        note: str,
        user: Optional[hikari.Snowflake] = None,
        vekn: Optional[str] = None,
    ) -> None:
        await self.deferred(hikari.MessageFlag.EPHEMERAL)
        vekn = vekn or self.discord.get_vekn(user)
        self.tournament._check_player(vekn)
        previous_level, previous_notes = None, None
        if vekn in self.tournament.notes:
            previous_notes = notes_by_level(self.tournament.notes[vekn])[-1]
            previous_level = previous_notes[0].level
            if note_level_int(previous_level) < note_level_int(level):
                previous_level = previous_notes = None

        if previous_notes and previous_level == tournament.NoteLevel.WARNING:
            upgrade_component = (
                "note-upgrade",
                "Disqualification",
                partialclass(
                    Note.ApplyNote, user, vekn, note, tournament.NoteLevel.WARNING, True
                ),
            )
        elif previous_notes and previous_level == tournament.NoteLevel.CAUTION:
            upgrade_component = (
                "note-upgrade",
                "Warning",
                partialclass(
                    Note.ApplyNote,
                    user,
                    vekn,
                    note,
                    tournament.NoteLevel.WARNING,
                    False,
                ),
            )
        elif previous_notes and previous_level == tournament.NoteLevel.NOTE:
            upgrade_component = (
                "note-upgrade",
                "Caution",
                partialclass(
                    Note.ApplyNote,
                    user,
                    vekn,
                    note,
                    tournament.NoteLevel.CAUTION,
                    False,
                ),
            )

        confirmation = self.bot.rest.build_message_action_row()
        if previous_notes:
            confirmation = confirmation.add_interactive_button(
                hikari.ButtonStyle.DANGER,
                f"note-upgrade-{self.author.id}",
                label=f"Upgrade to {upgrade_component[1]}",
            )
            COMPONENTS[f"note-upgrade-{self.author.id}"] = upgrade_component[2]
        confirmation = confirmation.add_interactive_button(
            hikari.ButtonStyle.PRIMARY,
            f"note-continue-{self.author.id}",
            label="Continue",
        ).add_interactive_button(
            hikari.ButtonStyle.SECONDARY, "note-cancel", label="Cancel"
        )
        COMPONENTS[f"note-continue-{self.author.id}"] = partialclass(
            Note.ApplyNote, user, vekn, note, level, False
        )
        COMPONENTS["note-cancel"] = Note.Cancel
        if previous_notes:
            embed = hikari.Embed(
                title="Review note level",
                description=(
                    "There are already some notes for this player, "
                    "you might want to upgrade your note level."
                ),
            )
            embed.add_field(
                name=f"Previous {note_level_str(previous_level)}",
                value="\n".join(f"- <@{p.judge}> {p.text}" for p in previous_notes),
            )
        else:
            embed = hikari.Embed(
                title="Confirmation",
                description="",
            )
        embed.add_field(
            name=f"Your {note_level_str(level)}",
            value=note,
        )
        await self.create_or_edit_response(
            embed=embed,
            components=[confirmation],
            flags=hikari.MessageFlag.EPHEMERAL,
        )

    class ApplyNote(BaseComponent):
        """Apply the note. Post a message on table channel for cautions and warnings."""

        UPDATE = db.UpdateLevel.WRITE

        def __init__(
            self,
            user: Optional[hikari.Snowflake],
            vekn: str,
            note: str,
            level: tournament.NoteLevel,
            disqualify: bool,
            *args,
            **kwargs,
        ):
            self.user = user
            self.vekn = vekn
            self.note = note
            self.level = level
            self.disqualify = disqualify
            super().__init__(*args, **kwargs)

        async def __call__(self):
            self.tournament.note(self.vekn, self.author.id, self.level, self.note)
            if self.disqualify:
                self.tournament.drop(self.vekn, tournament.DropReason.DISQUALIFIED)
                if self.user:
                    await self.bot.rest.remove_role_from_member(
                        self.guild_id,
                        self.user,
                        self.discord.roles[Role.PLAYER].id,
                        reason=self.reason,
                    )
            await self.update()
            if self.level == tournament.NoteLevel.NOTE:
                await self.create_or_edit_response(
                    embed=hikari.Embed(title="Note taken", description=self.note),
                    flags=hikari.MessageFlag.EPHEMERAL,
                    components=[],
                )
            else:
                embed = hikari.Embed(
                    title=f"{note_level_str(self.level)} delivered",
                    description=f"{self._player_display(self.vekn)}: {self.note}",
                )
                table_channel = None
                info = self.tournament.player_info(self.vekn)
                if info.table:
                    table_channel = self.discord.get_table_voice_channel(info.table).id
                coroutines = [
                    self.create_or_edit_response(
                        embed=embed,
                        components=[],
                    )
                ]
                if table_channel:
                    coroutines.append(
                        self.bot.rest.create_message(table_channel, embed=embed)
                    )
                await asyncio.gather(*coroutines)

    class Cancel(BaseComponent):
        UPDATE = db.UpdateLevel.READ_ONLY

        async def __call__(self):
            await self.create_or_edit_response(
                "Cancelled",
                flags=hikari.MessageFlag.EPHEMERAL,
                components=[],
                embeds=[],
            )


class Announce(BaseCommand):
    """Standard announcement - depends on the tournament state / step.

    It's the core helper. It provides instructions and guidance for judges and players.
    """

    UPDATE = db.UpdateLevel.READ_ONLY
    ACCESS = CommandAccess.JUDGE
    DESCRIPTION = "Make the standard announcement (depends on the tournament state)"
    OPTIONS = []

    async def __call__(self) -> None:
        if self.interaction_context.has_response:
            follow_up = True
        else:
            follow_up = False
            await self.deferred()
        judges_channel_id = self.discord.channels["TEXT"][Role.JUDGE].id
        players_channel_id = self.discord.main_channel_id
        current_round = self.tournament.current_round
        if self.tournament.state in [
            tournament.TournamentState.CHECKIN,
            tournament.TournamentState.WAITING_FOR_START,
        ]:
            current_round += 1
        if self.tournament.rounds and self.tournament.rounds[-1].finals:
            current_round = "Finals"
        else:
            current_round = f"Round {current_round}"
        if self.tournament.state == tournament.TournamentState.REGISTRATION:
            players_embed = hikari.Embed(
                title=f"{self.tournament.name} — Registrations open",
                description=(
                    f"{len(self.tournament.players)} players registered\n"
                    f"**Use the {Register.mention()} command to register.**"
                ),
            )
            if self.tournament.flags & tournament.TournamentFlag.VEKN_REQUIRED:
                players_embed.add_field(
                    name="VEKN ID# required",
                    value=(
                        "A VEKN ID is required to register to this tournament. "
                        "You can find yours on the "
                        "[VEKN website](https://www.vekn.net/player-registry). "
                        "If you do not have one, ask the Judges or your Prince."
                    ),
                )
            if self.tournament.flags & tournament.TournamentFlag.DECKLIST_REQUIRED:
                players_embed.add_field(
                    name="Decklist required",
                    value=(
                        "A decklist is required for this tournament. "
                        "You can register without one, but you need to provide "
                        "it before the first round. "
                        f"Use the {UploadDeck.mention()} command "
                        "to add your decklist."
                    ),
                )
            if self.tournament.flags & tournament.TournamentFlag.CHECKIN_EACH_ROUND:
                checkin_time = "each round"
            else:
                checkin_time = "the first round"
            players_embed.add_field(
                name="Check-in",
                value=(
                    "Once registered, you will need to check in before "
                    f"{checkin_time} with {CheckIn.mention()}."
                ),
            )
            judges_embed = hikari.Embed(
                title=players_embed.title,
                description=(
                    f"- {PlayersList.mention()} to check progression\n"
                    "  - ✅ Checked in and ready to play\n"
                    "  - ▶️ Playing\n"
                    "  - ⌛ Need to check-in\n"
                    "  - 📄 Missing decklist\n"
                    "  - Ⓜ️ Maximum number of rounds played\n"
                    "  - ❌ Checked out (dropped)\n"
                    "  - ⛔️ Disqualified\n"
                    f"- {RegisterPlayer.mention()} to register and check players in "
                    "yourself\n"
                    f"- {DropPlayer.mention()} to remove a player\n"
                    f"- {UploadDeckFor.mention()} to upload a player's decklist\n"
                    f"- {BatchRegister.mention()} to upload registrations file\n"
                    f"- {OpenCheckIn.mention()} to allow check-in. "
                    "Registration will still be possible once you open check-in.\n"
                    "\n"
                    "⚠️ **Important**\n"
                    "If you're hosting a live event or if the first round is about "
                    f"to start, you should use {OpenCheckIn.mention()} now. "
                    "This way, a registration will mark the player as checked in and "
                    "ready to play."
                ),
            )
            judges_embed.add_field(
                name="Player Registration",
                value=(
                    f"When you use the {RegisterPlayer.mention()} command, "
                    "you need to provide _either_ the user or their VEKN ID#. "
                    "With a *single command*, you can provide both, "
                    "and also the decklist. If the player is listed already, "
                    "the command will update the information (VEKN ID# and/or deck). "
                    "If not, the command will register them as a new player."
                ),
            )
            judges_embed.add_field(
                name="No VEKN ID#",
                value=(
                    "If the tournament requires a VEKN ID# and the player does not "
                    "have one yet, they cannot register. As judge, you can register "
                    f"them with the {RegisterPlayer.mention()} command. "
                    "If you do not provide a VEKN ID#, the bot will issue "
                    "a temporary ID to use as VEKN ID#: a 6-digit number prefixed with "
                    "`P`."
                ),
            )
        elif self.tournament.state == tournament.TournamentState.CHECKIN:
            players_embed = hikari.Embed(
                title=(f"{self.tournament.name} — CHECK-IN — {current_round}"),
                description=(
                    "⚠️ **Check-in is required to play**\n"
                    f"Please confirm your participation with the {CheckIn.mention()} "
                    "command.\n"
                    f"You can use {Status.mention()} to verify your status."
                ),
            )
            if (
                self.tournament.current_round == 0
                or self.tournament.flags & tournament.TournamentFlag.REGISTER_BETWEEN
            ):
                players_embed.add_field(
                    name="Registration",
                    value=(
                        "If you are not registered yet, you can still do so "
                        f"by using the {Register.mention()} command. "
                        "You will be checked in automatically."
                    ),
                )
            judges_embed = hikari.Embed(
                title=players_embed.title,
                description=(
                    f"- {PlayersList.mention()} to check progression\n"
                    f"- {RegisterPlayer.mention()} to register & check players in\n"
                    f"- {UploadDeckFor.mention()} to upload a player's decklist\n"
                    f"- {DropPlayer.mention()} to remove a player\n"
                    f"- {Round.mention('start')} when you're ready\n"
                ),
            )
            if self.tournament.flags & tournament.TournamentFlag.STAGGERED:
                judges_embed.description += (
                    f"- {UnStagger.mention()} to switch back to a standard tournament\n"
                )
            else:
                judges_embed.description += (
                    f"- {Stagger.mention()} to switch to a staggered tournament "
                    "(6, 7, or 11 players)\n"
                )
        elif self.tournament.state == tournament.TournamentState.WAITING_FOR_CHECKIN:
            players_embed = hikari.Embed(
                title=(f"{self.tournament.name} — {current_round} finished"),
                description=(
                    "Waiting for next round to begin.\n"
                    f"You can use the {Status.mention()} command to verify your status."
                ),
            )
            players_embed.add_field(
                name="Check-in required",
                value=(
                    "Players will need to **check in again** for next round "
                    "(unless it is the finals)."
                ),
            )
            if self.tournament.flags & tournament.TournamentFlag.REGISTER_BETWEEN:
                players_embed.add_field(
                    name="Registration",
                    value=(
                        "If you are not registered yet, you can do so "
                        f"by using the {Register.mention()}  command. "
                        "You will also need to check in for the next round "
                        "before it begins, once the check-in is open."
                    ),
                )
            if self.tournament.flags & tournament.TournamentFlag.MULTIDECK:
                players_embed.add_field(
                    name="Change of deck",
                    value=(
                        "This is a multideck tournament, you can change your deck "
                        "for the next round.\n"
                        f"Use {UploadDeck.mention()} to record it. "
                    ),
                )
            judges_embed = hikari.Embed(
                title=players_embed.title,
                description=(
                    f"- {OpenCheckIn.mention()}  to open the check-in for next round\n"
                    f"- {Standings.mention()}  to get current standings\n"
                    f"- {PlayersList.mention()}  to check the list of players\n"
                    f"- {RegisterPlayer.mention()}  to add a player\n"
                    f"- {DropPlayer.mention()}  to remove a player\n"
                    f"- {UploadDeckFor.mention()}  to change a player's deck\n"
                    f"- {Finals.mention()}  to start the finals\n"
                ),
            )
        elif self.tournament.state == tournament.TournamentState.WAITING_FOR_START:
            players_embed = hikari.Embed(
                title=(f"{self.tournament.name} — {current_round} starting soon"),
                description=(
                    "Waiting for the round to begin.\n"
                    f"You can use the {Status.mention()} command to verify your status."
                ),
            )
            if self.tournament.flags & tournament.TournamentFlag.REGISTER_BETWEEN:
                players_embed.add_field(
                    name="Registration",
                    value=(
                        "If you are not registered yet, you can still do so "
                        f"by using the {Register.mention()}  command. "
                        "You will be checked in automatically."
                    ),
                )
            if self.tournament.flags & tournament.TournamentFlag.MULTIDECK:
                players_embed.add_field(
                    name="Change of deck",
                    value=(
                        "This is a multideck tournament, you can change your deck "
                        "for the next round.\n"
                        f"Use {UploadDeck.mention()} to record it. "
                    ),
                )
            judges_embed = hikari.Embed(
                title=players_embed.title,
                description=(
                    f"- {Standings.mention()}  to get current standings\n"
                    f"- {Round.mention('start')}  to start the next round\n"
                    f"- {PlayersList.mention()}  to check the list of players\n"
                    f"- {DropPlayer.mention()}  to remove a player\n"
                    f"- {Finals.mention()}  to start the finals\n"
                ),
            )
        elif self.tournament.state == tournament.TournamentState.FINISHED:
            description = f"The {self.tournament.name} is finished.\n"
            winner = self.tournament.players.get(self.tournament.winner, None)
            if winner:
                description += (
                    f"Congratulations {self._player_display(winner.vekn)} "
                    "for your victory!"
                )
            players_embed = hikari.Embed(
                title=(f"{self.tournament.name} — {current_round} finished"),
                description=description,
            )
            if winner.deck:
                players_embed.add_field(
                    name="Decklist", value=self._deck_display(winner.deck)
                )
            judges_embed = hikari.Embed(
                title=players_embed.title,
                description=(
                    f"- {DownloadReports.mention()} to get the report files\n"
                    f"- {CloseTournament.mention()} to close this tournament\n"
                ),
            )
        elif self.tournament.state == tournament.TournamentState.PLAYING:
            players_embed = hikari.Embed(
                title=(f"{self.tournament.name} — {current_round} in progress"),
                description=(
                    "Join your assigned table channels and enjoy your game.\n"
                    f"Use the {Status.mention()} command to **find your table**."
                ),
            )
            players_embed.add_field(
                name="Report your results",
                value=(
                    f"Use the {Report.mention()}  command to report "
                    "your Victory Points.\n"
                    "No need to report scores of zero."
                ),
            )
            if self.tournament.rounds[-1].finals:
                judges_embed = hikari.Embed(
                    title=players_embed.title,
                    description=(
                        f"- {Round.mention('reset')} to cancel (the toss stays)\n"
                        f"- {FixReport.mention()}  to register the score\n"
                        f"- {Round.mention('finish')}  when all is good\n"
                        "\n"
                        "Once this is done, you can get the reports with "
                        f"{DownloadReports.mention()}  and close the tournament with "
                        f"{CloseTournament.mention()} ."
                    ),
                )
            else:
                judges_embed = hikari.Embed(
                    title=players_embed.title,
                    description=(
                        f"- {Round.mention('add')} to add a player before game\n"
                        f"- {Round.mention('remove')} to remove a player before game \n"
                        f"- {Round.mention('reset')} to cancel the round and seating\n"
                        f"- {Note.mention()} to deliver cautions and warnings\n"
                        f"- {PlayerInfo.mention()} to check a specific player's info\n"
                        f"- {Results.mention()} to see the results\n"
                        f"- {FixReport.mention()} to correct them if needed\n"
                        f"- {ValidateScore.mention()} to force odd scores validation\n"
                        f"- {Round.mention('finish')} when all is good\n"
                        "\n"
                        f"You can still register a late arrival with "
                        f"{RegisterPlayer.mention()} then add them "
                        "to a 4-players table that has not started (if any) "
                        f"with {Round.mention('add')}."
                    ),
                )
        comp = []
        if self.tournament.is_limited() and self.vdb_format:
            comp = [
                self.bot.rest.build_message_action_row().add_interactive_button(
                    hikari.ButtonStyle.PRIMARY,
                    "vdb-format",
                    label="Download VDB format",
                )
            ]
            COMPONENTS["vdb-format"] = DownloadVDBFormat
        players_role_id = self.discord.roles[Role.PLAYER].id
        if self.channel_id == players_channel_id and not follow_up:
            messages = [
                self.create_or_edit_response(
                    embed=players_embed,
                    content=f"<@&{players_role_id}>",
                    role_mentions=[players_role_id],
                    components=comp,
                ),
                self.bot.rest.create_message(judges_channel_id, embed=judges_embed),
            ]
        elif self.channel_id == judges_channel_id and not follow_up:
            messages = [
                self.bot.rest.create_message(
                    players_channel_id,
                    embed=players_embed,
                    content=f"<@&{players_role_id}>",
                    role_mentions=[players_role_id],
                    components=comp,
                ),
                self.create_or_edit_response(embed=judges_embed),
            ]
        else:
            messages = [
                self.bot.rest.create_message(
                    players_channel_id,
                    embed=players_embed,
                    content=f"<@&{players_role_id}>",
                    role_mentions=[players_role_id],
                    components=comp,
                ),
                self.bot.rest.create_message(judges_channel_id, embed=judges_embed),
            ]
            if not follow_up:
                messages.append(self.create_or_edit_response("Announcement made"))
        await asyncio.gather(*messages)


class Status(BaseCommand):
    """Player status. Provides guidance for lost souls."""

    UPDATE = db.UpdateLevel.READ_ONLY
    DESCRIPTION = "Check your current status"
    OPTIONS = []

    async def __call__(self) -> None:
        await self.deferred(hikari.MessageFlag.EPHEMERAL)
        judge_role_id = self.discord.roles[Role.JUDGE].id
        embed = hikari.Embed(
            title=f"{self.tournament.name} — {len(self.tournament.players)} players"
        )
        vekn = self.discord.get_vekn(self.author.id)
        if not vekn:
            if self.tournament.rounds and not (
                self.tournament.flags & tournament.TournamentFlag.REGISTER_BETWEEN
            ):
                embed.description = "Tournament in progress. You're not participating."
            elif (
                self.tournament.state == tournament.TournamentState.WAITING_FOR_CHECKIN
            ):
                embed.description = "Waiting for registrations to open."
            else:
                embed.description = (
                    f"{len(self.tournament.players)} players registered.\n"
                    f"Register using the {Register.mention()} command."
                )
                if self.tournament.flags & tournament.TournamentFlag.VEKN_REQUIRED:
                    embed.description += (
                        "\nThis tournament requires a **VEKN ID#**. "
                        f"If you do not have one, ask a <@&{judge_role_id}> to help "
                        "with your registration."
                    )
        else:
            info = self.tournament.player_info(vekn)
            logger.debug("Player info: %s", info)
            embed.description = ""
            if info.status == tournament.PlayerStatus.DROPPED_OUT:
                embed.description = "**DROPPED**\n"
            elif info.status == tournament.PlayerStatus.DISQUALIFIED:
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
                if self.tournament.rounds[-1].finals:
                    seat = "seed"
                else:
                    seat = "seat"
                embed.description = (
                    f"You are {seat} {info.position} on table {info.table}\n"
                )
                voice_chan_id = self.discord.channels["VOICE"].get(info.table, None).id
                if voice_chan_id:
                    embed.description += f"\n**Join vocal:** <#{voice_chan_id}>"
                embed.description += (
                    f"\nUse the {Report.mention()} command to register your VPs"
                )
            elif info.status == tournament.PlayerStatus.CHECKED_IN:
                embed.description = (
                    "You are ready to play.\n"
                    "You will be assigned a table and seat when the round starts."
                )
            elif info.status == tournament.PlayerStatus.CHECKIN_REQUIRED:
                embed.description = (
                    "⚠️ **You need to check-in**\n"
                    f"Use the {CheckIn.mention()} command to check in "
                    "for the upcoming round."
                )
            elif info.status == tournament.PlayerStatus.MAX_ROUNDS_PLAYED:
                embed.description = (
                    "You played the maximum number of preliminary rounds."
                )
            elif info.status == tournament.PlayerStatus.MISSING_DECK:
                embed.description = (
                    "⚠️ **You need to provide your decklist**\n"
                    f"Please use the {UploadDeck.mention()} command "
                    "to provide your decklist.\n"
                )
            elif info.status == tournament.PlayerStatus.WAITING:
                if self.tournament.current_round == 0:
                    embed.description = (
                        "You are registered. Waiting for check-in to open."
                    )
                    if info.player.deck:
                        embed.description += (
                            "\nYour decklist has been saved. You can use the "
                            f"{UploadDeck.mention()} command again to update it."
                        )
                elif self.tournament.rounds[-1].finals:
                    embed.description = (
                        "You are done. Thanks for participating in this event!"
                    )
                elif (
                    self.tournament.flags & tournament.TournamentFlag.CHECKIN_EACH_ROUND
                ):
                    embed.description = (
                        f"You will need to {CheckIn.mention()} for next round, if any."
                    )
                    if self.tournament.flags & tournament.TournamentFlag.MULTIDECK:
                        embed.description += (
                            f"\nYou can change your deck, use {UploadDeck.mention()}"
                            "to upload it."
                        )
                else:
                    embed.description = (
                        "You are ready to play. Waiting for next round to start."
                    )
                    if self.tournament.flags & tournament.TournamentFlag.MULTIDECK:
                        embed.description += (
                            f"\nYou can change your deck, use {UploadDeck.mention()}"
                            "to upload it."
                        )
            elif info.status == tournament.PlayerStatus.CHECKED_OUT:
                embed.description = "You are not checked in. Check-in is closed, sorry."
            else:
                raise RuntimeError("Unexpected tournament state")
            if self.tournament.rounds:
                if (
                    self.tournament.state == tournament.TournamentState.PLAYING
                    and info.player.playing
                ):
                    if self.tournament.rounds[-1].finals:
                        embed.description = (
                            f"**You are playing in the finals** {info.score}\n"
                            + embed.description
                        )
                    else:
                        ORDINAL = {
                            1: "st",
                            2: "nd",
                            3: "rd",
                        }
                        embed.description = (
                            f"**You are playing your {info.rounds}"
                            f"{ORDINAL.get(info.rounds, 'th')} round {info.score}**\n"
                            + embed.description
                        )
                else:
                    embed.description = (
                        f"You played {info.rounds} rounds {info.score}\n"
                        + embed.description
                    )
        await self.create_or_edit_response(
            embed=embed, flags=hikari.MessageFlag.EPHEMERAL
        )


class Help(BaseCommand):
    """Useful alias."""

    UPDATE = db.UpdateLevel.READ_ONLY
    REQUIRES_TOURNAMENT = False
    DESCRIPTION = "Ask me what to do"
    OPTIONS = []

    async def __call__(self) -> None:
        if not self.tournament:
            await self.create_or_edit_response(
                "No tournament in progress, "
                f"use {OpenTournament.mention()} to start one.\n"
                "This command may be available only to specific roles in the server."
            )
            return
        if self._is_judge():
            next_step = Announce.copy_from_interaction(self)
        else:
            next_step = Status.copy_from_interaction(self)
        return await next_step()


class Standings(BaseCommand):
    """Standings of all players. Private (ephemeral) answer by default."""

    UPDATE = db.UpdateLevel.READ_ONLY
    ACCESS = CommandAccess.JUDGE
    DESCRIPTION = "JUDGE: Display current standings"
    OPTIONS = [
        hikari.CommandOption(
            type=hikari.OptionType.BOOLEAN,
            name="public",
            description="Display the standings publicly (default is private)",
            is_required=False,
        ),
    ]

    async def __call__(self, public: bool = False) -> None:
        winner, ranking = self.tournament.standings()
        embed = hikari.Embed(
            title="Standings",
            description="\n".join(
                ("*WINNER* " if winner == vekn else f"{rank}. ")
                + f"{self._player_display(vekn)} {score}"
                for rank, vekn, score in ranking
            ),
        )
        embed.set_thumbnail(hikari.UnicodeEmoji("📋"))
        await self.create_or_edit_response(
            embeds=_paginate_embed(embed),
            flags=(
                hikari.UNDEFINED
                if public or self._is_judge_channel()
                else hikari.MessageFlag.EPHEMERAL
            ),
        )


class PlayerInfo(BaseCommand):
    """Player information. Includes notes."""

    UPDATE = db.UpdateLevel.READ_ONLY
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
        user: Optional[hikari.Snowflake] = None,
    ) -> None:
        vekn = vekn or self.discord.get_vekn(user)
        info = self.tournament.player_info(vekn)
        description = self._player_display(vekn)
        description += (
            f"\n{info.rounds} round{'s' if info.rounds > 1 else ''} played {info.score}"
        )
        if info.status == tournament.PlayerStatus.DROPPED_OUT:
            description += "\n**DROPPED**"
        elif info.status == tournament.PlayerStatus.DISQUALIFIED:
            description += "\n**DISQUALIFIED**"
        embed = hikari.Embed(
            title="Player Info",
            description=description,
        )
        if info.player.deck:
            if self.tournament.rounds:
                embed.add_field(
                    name="Decklist", value=self._deck_display(info.player.deck)
                )
            else:
                embed.add_field(
                    name="Decklist registered",
                    value=(
                        "You will have access to the list "
                        "after the first round begins."
                    ),
                )
        if info.player.playing:
            if self.tournament.state in [
                tournament.TournamentState.CHECKIN,
                tournament.TournamentState.WAITING_FOR_START,
            ]:
                embed.add_field(
                    name="Checked-in",
                    value=("Player is checked-in and ready to play."),
                )
            elif self.tournament.state == tournament.TournamentState.PLAYING:
                if self.tournament.rounds[-1].finals:
                    seat = "seed"
                else:
                    seat = "seat"
                description = (
                    f"Player is {seat} {info.position} on table {info.table}\n"
                )
                voice_chan_id = self.discord.channels["VOICE"].get(info.table, None).id
                if voice_chan_id:
                    description += f"\n**Vocal:** <#{voice_chan_id}>"
                embed.add_field(
                    name="Playing",
                    value=description,
                )
        for notes in notes_by_level(info.notes):
            embed.add_field(
                name=note_level_str(notes[0].level),
                value="\n".join(f"- <@{n.judge}> {n.text}" for n in notes),
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
    """Round results. Defaults to current round."""

    UPDATE = db.UpdateLevel.READ_ONLY
    ACCESS = CommandAccess.JUDGE
    DESCRIPTION = "JUDGE: Display current round results"
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
    ) -> None:
        round_number = round or self.tournament.current_round
        try:
            round: tournament.Round = self.tournament.rounds[round_number - 1]
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
        incorrect = round.score()
        judge_role_id = self.discord.roles[Role.JUDGE].id
        for i, table in enumerate(round.seating.iter_tables(), 1):
            scores = []
            for j, vekn in enumerate(table, 1):
                score = round.results.get(vekn, None) or tournament.Score()
                scores.append(f"{j}. {self._player_display(vekn)} {score}")
            embed.add_field(
                name=f"Table {i} " + ("⚠️" if i in incorrect else "☑️"),
                value="\n".join(scores)
                + (
                    f"\n_Odd score validated by <@&{judge_role_id}> "
                    f'because "{round.overrides[i].text}"_'
                    if i in round.overrides
                    else ""
                ),
                inline=True,
            )
        embeds = _paginate_embed(embed)
        await self.create_or_edit_response(embeds=embeds, flags=flag)


def status_icon(status: tournament.PlayerStatus) -> str:
    return {
        tournament.PlayerStatus.CHECKED_IN: "✅",  # checked
        tournament.PlayerStatus.DISQUALIFIED: "⛔",  # forbidden
        tournament.PlayerStatus.PLAYING: "▶",  # play
        tournament.PlayerStatus.MISSING_DECK: "📄",  # page
        tournament.PlayerStatus.MAX_ROUNDS_PLAYED: "Ⓜ️",  # circled M
        tournament.PlayerStatus.CHECKIN_REQUIRED: "⌛",  # hourglass
        tournament.PlayerStatus.WAITING: "",
        tournament.PlayerStatus.CHECKED_OUT: "❌",  # red cross
    }[status]


class PlayersList(BaseCommand):

    """Players list with status icon - useful to sheperd the flock."""

    UPDATE = db.UpdateLevel.READ_ONLY
    ACCESS = CommandAccess.JUDGE
    DESCRIPTION = "JUDGE: Display the list of players"
    OPTIONS = [
        hikari.CommandOption(
            type=hikari.OptionType.BOOLEAN,
            name="public",
            description="Display the list publicly (default is private)",
            is_required=False,
        ),
    ]

    async def __call__(self, public: bool = False) -> None:
        if public or self._is_judge_channel():
            flag = hikari.UNDEFINED
        else:
            flag = hikari.MessageFlag.EPHEMERAL
        players = sorted(self.tournament.players.values(), key=lambda p: p.vekn)
        playing = len([p for p in self.tournament.players.values() if p.playing])
        total = len(self.tournament.players)
        embed = hikari.Embed(title=f"Players ({playing}/{total})")
        player_lines = []
        for p in players:
            info = self.tournament.player_info(p.vekn)
            player_lines.append(
                f"- {status_icon(info.status)} {self._player_display(p.vekn)}"
            )
        embed.description = "\n".join(player_lines)
        embeds = _paginate_embed(embed)
        await self.create_or_edit_response(embeds=embeds, flags=flag)


class DownloadReports(BaseCommand):
    """Download reports. Archon-compatible reports if VEKN ID# were required."""

    UPDATE = db.UpdateLevel.READ_ONLY
    ACCESS = CommandAccess.JUDGE
    DESCRIPTION = "JUDGE: Get CSV reports for the tournament"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._report_number = {}

    async def __call__(self) -> None:
        if self.tournament.state == tournament.TournamentState.PLAYING:
            raise CommandFailed("Finish the current round before exporting results")
        await self.deferred(hikari.MessageFlag.EPHEMERAL)
        self._report_number.clear()
        reports = [self._build_results_csv()]
        if self.tournament.flags & tournament.TournamentFlag.DECKLIST_REQUIRED:
            reports.append(self._build_decks_json())
        if self.tournament.flags & tournament.TournamentFlag.VEKN_REQUIRED:
            reports.append(self._build_methuselahs_csv())
            reports.extend(f for f in self._build_rounds_csvs())
            if self.tournament.state == tournament.TournamentState.FINISHED:
                reports.append(self._build_finals_csv())
        await self.create_or_edit_response(
            embed=hikari.Embed(
                title="Reports",
                description=(
                    "Download those file and store them safely before you close "
                    "the tournament."
                ),
            ),
            attachments=reports,
            flags=hikari.MessageFlag.EPHEMERAL,
        )

    def _build_csv(self, filename: str, it: Iterable[str], columns=None):
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        if columns:
            writer.writerow(columns)
        writer.writerows(it)
        buffer = io.BytesIO(buffer.getvalue().encode("utf-8"))
        return hikari.Bytes(buffer, filename, mimetype="text/csv")

    def _build_json(self, filename: str, data: str):
        buffer = io.StringIO()
        json.dump(data, buffer, indent=2)
        buffer = io.BytesIO(buffer.getvalue().encode("utf-8"))
        return hikari.Bytes(buffer, filename, mimetype="application/json")

    def _build_results_csv(self):
        _winner, ranking = self.tournament.standings()
        data = []
        report_number = 1
        for rank, vekn, score in ranking:
            if vekn in self.tournament.dropped:
                rank = "DQ"
            info = self.tournament.player_info(vekn)
            if info.rounds <= 0:
                self._report_number[vekn] = None
            else:
                self._report_number[vekn] = report_number
                report_number += 1
                data.append(
                    [
                        self._report_number[vekn],
                        info.player.vekn,
                        info.player.name,
                        info.rounds,
                        score.gw,
                        score.vp,
                        info.player.seed or "",
                        rank,
                    ]
                )
        return self._build_csv(
            "Report.csv",
            data,
            columns=[
                "Player Num",
                "V:EKN Num",
                "Name",
                "Games Played",
                "Games Won",
                "Total VPs",
                "Finals Position",
                "Rank",
            ],
        )

    def _build_decks_json(self):
        """List of decks."""
        data = []
        for player in sorted(
            self.tournament.players.values(),
            key=lambda p: self._report_number.get(p.vekn, 0),
        ):
            info = self.tournament.player_info(player.vekn)
            if not self._report_number.get(player.vekn, None):
                continue
            data.append(
                {
                    "vekn": player.vekn,
                    "finals_seed": player.seed,
                    "rounds": info.rounds,
                    "score": asdict(info.score),
                    "deck": player.deck,
                }
            )
        return self._build_json("Decks.json", data)

    def _player_first_last_name(self, player):
        if not player.name:
            return ["", ""]
        match = re.search(r"\(([^\)]*)\)", player.name)
        if match:
            name = match.group(1)
        else:
            name = player.name
        name = name.split(" ", 1)
        if len(name) < 2:
            name.append("")
        return name

    def _build_methuselahs_csv(self):
        data = []
        for player in sorted(
            self.tournament.players.values(),
            key=lambda p: self._report_number.get(p.vekn, 0),
        ):
            if not self._report_number.get(player.vekn, None):
                continue
            name = self._player_first_last_name(player)
            info = self.tournament.player_info(player.vekn)
            data.append(
                [
                    self._report_number[player.vekn],
                    name[0],
                    name[1],
                    "",  # country
                    player.vekn,
                    info.rounds,
                    "DQ"
                    if info.status
                    in [
                        tournament.PlayerStatus.DISQUALIFIED
                        or tournament.PlayerStatus.DROPPED_OUT
                    ]
                    else "",
                ]
            )
        return self._build_csv("Methuselahs.csv", data)

    def _build_rounds_csvs(self):
        for i, round in enumerate(self.tournament.rounds, 1):
            if not round.results:
                break
            if round.finals:
                break
            data = []
            for j, table in enumerate(round.seating, 1):
                for vekn in table:
                    player = self.tournament.players[vekn]
                    name = self._player_first_last_name(player)
                    data.append(
                        [
                            self._report_number[player.vekn],
                            name[0],
                            name[1],
                            j,
                            round.results.get(player.vekn, tournament.Score()).vp,
                        ]
                    )
                if len(table) < 5:
                    data.append(["", "", "", "", ""])
            yield self._build_csv(f"Round-{i}.csv", data)

    def _build_finals_csv(self):
        data = []
        round = self.tournament.rounds[-1]
        if not round.finals:
            raise RuntimeError("No finals")
        players = sorted(
            [self.tournament.players[n] for n in round.seating[0]], key=lambda p: p.seed
        )
        for player in players:
            name = self._player_first_last_name(player)
            data.append(
                [
                    self._report_number[player.vekn],
                    name[0],
                    name[1],
                    1,  # table
                    player.seed,  # seat
                    round.results.get(player.vekn, tournament.Score()).vp,
                ]
            )
        return self._build_csv("Finals.csv", data)


class Raffle(BaseCommand):
    """Could come in handy: select a count of players randomly."""

    UPDATE = db.UpdateLevel.READ_ONLY
    ACCESS = CommandAccess.JUDGE
    DESCRIPTION = "JUDGE: Select random players"
    OPTIONS = [
        hikari.CommandOption(
            type=hikari.OptionType.INTEGER,
            name="count",
            description="JUDGE: Number of players to select (defaults to one)",
            is_required=False,
        ),
    ]

    async def __call__(self, count: Optional[int] = None) -> None:
        await self.deferred()
        count = count or 1
        active_players = len(
            set(self.tournament.players.keys()) - set(self.tournament.dropped.keys())
        )
        if count < 1 or count > active_players:
            raise CommandFailed(
                f"Invalid count: choose a number between 1 and {active_players}"
            )
        players = random.sample(
            [
                vekn
                for vekn in self.tournament.players.keys()
                if vekn and vekn not in self.tournament.dropped
            ],
            k=count,
        )
        embed = hikari.Embed(
            title="Raffle Winners",
            description="\n".join(
                f"- {self._player_display(vekn)}" for vekn in players
            ),
        )
        await asyncio.sleep(3)
        await self.create_or_edit_response(embed=embed)


class ResetChannelsAndRoles(BaseCommand):
    """For dev purposes and in case of bug: realign the channels."""

    UPDATE = db.UpdateLevel.WRITE
    ACCESS = CommandAccess.ADMIN
    DESCRIPTION = "ADMIN: Reset tournament channels and roles"
    OPTIONS = []

    async def __call__(self) -> None:
        """Realign channels on what we have registered."""
        await self.deferred()
        await self._align_roles()
        await self._align_channels()
        await self.update()
        embed = hikari.Embed(
            title="Channels reset",
            description="Channels have been realigned.",
        )
        await self.create_or_edit_response(embed=embed)
