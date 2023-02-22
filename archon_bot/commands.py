import asyncio

import csv
import enum
import functools
import itertools
import io
import json
import logging
import random
from typing import Dict, Iterable, List, Optional, Union

import hikari
import hikari.channels
import hikari.guilds
import hikari.users
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
SUB_COMMANDS = {}
COMMANDS_TO_REGISTER = {}
COMPONENTS = {}

VDB_URL = "https://vdb.im"
AMARANTH_URL = "https://amaranth.vtes.co.nz"


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
            for field in fields:
                embed.add_field(
                    name=field.name, value=field.value, inline=field.is_inline
                )
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


class DiscordExtra:
    def __init__(self, app, *args, **kwargs):
        self.prefix: str = kwargs.get("prefix", "")
        self.judges: List[hikari.Snowflake] = [
            hikari.Snowflake(i) for i in kwargs.get("judges", [])
        ]
        self.spectators: List[hikari.Snowflake] = [
            hikari.Snowflake(i) for i in kwargs.get("spectators", [])
        ]
        self.roles: Dict[str, hikari.guilds.PartialRole] = {}
        for key, data in kwargs.get("roles", {}).items():
            self.roles[key] = hikari.guilds.PartialRole(app=app, **data)
        self.channels: Dict[str, Dict[str, hikari.channels.PartialChannel]] = {}
        for type, dic in kwargs.get("channels", {}).items():
            self.channels.setdefault(type, {})
            for key, data in dic.items():
                self.channels[type][key] = hikari.channels.PartialChannel(
                    app=app, **data
                )

    def to_json(self) -> dict:
        return {
            "prefix": self.prefix,
            "judges": self.judges,
            "spectators": self.spectators,
            "roles": {
                key: {"id": role.id, "name": role.name}
                for key, role in self.roles.items()
            },
            "channels": {
                typ: {
                    key: {"id": chan.id, "name": chan.name, "type": chan.type}
                    for key, chan in channels.items()
                }
                for typ, channels in self.channels.items()
            },
        }

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

    def table_roles(self) -> List[hikari.PartialRole]:
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

    def set_judge_text_channel(self, channel: hikari.PartialChannel):
        self.channels.setdefault("TEXT", {})
        self.channels["TEXT"][Role.JUDGE] = channel

    def get_judge_voice_channel(self):
        return self.channels["VOICE"][Role.JUDGE]

    def set_judge_voice_channel(self, channel: hikari.PartialChannel):
        self.channels.setdefault("VOICE", {})
        self.channels["VOICE"][Role.JUDGE] = channel

    def get_table_text_channel(self, table_num: int):
        return self.channels["TEXT"][str(table_num)]

    def set_table_text_channel(self, table_num: int, channel: hikari.PartialChannel):
        self.channels.setdefault("TEXT", {})
        self.channels["TEXT"][str(table_num)] = channel

    def get_table_voice_channel(self, table_num: int):
        return self.channels["VOICE"][str(table_num)]

    def set_table_voice_channel(self, table_num: int, channel: hikari.PartialChannel):
        self.channels.setdefault("VOICE", {})
        self.channels["VOICE"][str(table_num)] = channel

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
        self.discord = DiscordExtra(self.interaction.app, **discord_data)
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

    def update(self) -> None:
        """Update tournament data."""
        if self.UPDATE < db.UpdateLevel.WRITE:
            raise RuntimeError("Command is not marked as UPDATE")
        self.tournament.extra["discord"] = self.discord.to_json()
        data = self.tournament.to_json()
        db.update_tournament(
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

    def _player_display(self, player_id: tournament.PlayerID) -> str:
        """How to display a player."""
        player = self.tournament.players[player_id]
        return (
            ("**[D]** " if player.vekn in self.tournament.dropped else "")
            + (
                f"{player.name[:29] + '...' if len(player.name) > 32 else player.name} "
                f"#{player.vekn} "
                if player.name
                else f"#{player.vekn} "
            )
            + (f"<@{player.discord}>" if player.discord else "")
        )

    def _deck_display(self, data: dict) -> str:
        deck = krcg.deck.Deck()
        deck.from_json(data)
        return f"[{deck.name}]({deck.to_vdb()})"

    async def _align_roles(
        self,
        raise_on_exists: bool = False,
        silence_exceptions: bool = False,
    ) -> None:
        # list what is expected
        expected = [(r, self.discord.role_name(r)) for r in Role]
        if self.tournament.state == tournament.TournamentState.PLAYING:
            for table_num in range(1, self.tournament.tables_count() + 1):
                expected.append(
                    (str(table_num), self.discord.role_name(Role.PLAYER, table_num))
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
            self.discord.roles[Role.ROOT_JUDGE] = root_judge[0]
        existing = [r for r in existing if r.name.startswith(self.discord.prefix + "-")]
        if existing and raise_on_exists:
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
            self.discord.roles[key] = role
            # when we're recreating JUDGE / TABLE role,
            # we must drop the matching channels
            self.discord.channels.get("TEXT", {}).pop(key, None)
            self.discord.channels.get("VOICE", {}).pop(key, None)
            if key == Role.PLAYER:
                id_roles.extend(
                    (p.discord, role) for p in self.tournament.players.iter_players()
                )
            elif key == Role.ROOT_JUDGE:
                id_roles.extend((uid, role) for uid in self.discord.judges)
                id_roles.append((self.bot.get_me().id, role))
                logger.warning(
                    "Recreating Root judge role, review commands permissions settings"
                )
            elif key == Role.JUDGE:
                id_roles.extend((uid, role) for uid in self.discord.judges)
                id_roles.append((self.bot.get_me().id, role))
                logger.warning(
                    "Recreating Judge role, bot might miss access to previous channels"
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
                for number in table:
                    if number not in self.tournament.players:
                        continue
                    discord_id = self.tournament.players[number].discord
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
        raise_on_exists: bool = False,
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
                        ("TEXT", str(table_num)),
                        self.discord.channel_name(
                            Role.PLAYER, hikari.ChannelType.GUILD_TEXT, table_num
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
                                deny=perm.TEXT,
                            ),
                            hikari.PermissionOverwrite(
                                id=self.discord.roles[str(table_num)].id,
                                type=hikari.PermissionOverwriteType.ROLE,
                                allow=perm.TEXT,
                            ),
                            hikari.PermissionOverwrite(
                                id=self.discord.roles[Role.JUDGE].id,
                                type=hikari.PermissionOverwriteType.ROLE,
                                allow=perm.TEXT,
                            ),
                            hikari.PermissionOverwrite(
                                id=self.discord.roles[Role.SPECTATOR].id,
                                type=hikari.PermissionOverwriteType.ROLE,
                                allow=perm.SPECTATE_TEXT,
                            ),
                        ],
                    )
                )
                expected.append(
                    (
                        ("VOICE", str(table_num)),
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
                                id=self.discord.roles[str(table_num)].id,
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
        if existing and raise_on_exists:
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
            self.discord.channels[key[0]][key[1]] = res
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
        await self.deferred()
        logger.debug("Creating tournament...")
        self.tournament = tournament.Tournament(name=name)
        self.discord.prefix = "".join([w[0] for w in name.split()][:3])
        self.discord.judges.append(self.author.id)
        await self._align_roles(raise_on_exists=True)
        await self._align_channels(raise_on_exists=True)
        # author is now a judge, he can configure (next step)
        self.author.role_ids.append(self.discord.roles[Role.JUDGE].id)
        logger.debug("Register tournament in DB...")
        self.tournament.extra["discord"] = self.discord.to_json()
        db.create_tournament(
            self.connection,
            self.guild_id,
            self.category_id,
            self.tournament.to_json(),
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
            elif self.interaction.custom_id == "register-between":
                self.tournament.flags ^= tournament.TournamentFlag.REGISTER_BETWEEN
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
        multideck = self.tournament.flags & tournament.TournamentFlag.MULTIDECK
        between = self.tournament.flags & tournament.TournamentFlag.REGISTER_BETWEEN
        staggered = self.tournament.flags & tournament.TournamentFlag.STAGGERED
        if getattr(self.interaction, "custom_id", None) == "validate":
            components = []
            COMPONENTS.pop("vekn-required", None)
            COMPONENTS.pop("decklist-required", None)
            COMPONENTS.pop("checkin-each-round", None)
            COMPONENTS.pop("multideck", None)
            COMPONENTS.pop("register-between", None)
            COMPONENTS.pop("staggered", None)
            COMPONENTS.pop("validate", None)
        else:
            components = [
                self.bot.rest.build_message_action_row()
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
                    if multideck
                    else hikari.ButtonStyle.PRIMARY,
                    "multideck",
                )
                .set_label("Single Deck" if multideck else "Multideck")
                .add_to_container()
                .add_button(
                    hikari.ButtonStyle.SECONDARY
                    if between
                    else hikari.ButtonStyle.PRIMARY,
                    "register-between",
                )
                .set_label("No late joiners" if between else "Join anytime")
                .add_to_container(),
                self.bot.rest.build_message_action_row()
                .add_button(
                    hikari.ButtonStyle.SECONDARY
                    if staggered
                    else hikari.ButtonStyle.PRIMARY,
                    "staggered",
                )
                .set_label("Normal" if staggered else "Staggered")
                .add_to_container()
                .add_button(hikari.ButtonStyle.SUCCESS, "validate")
                .set_label("OK")
                .add_to_container(),
            ]
            COMPONENTS["vekn-required"] = ConfigureTournament
            COMPONENTS["decklist-required"] = ConfigureTournament
            COMPONENTS["checkin-each-round"] = ConfigureTournament
            COMPONENTS["multideck"] = ConfigureTournament
            COMPONENTS["register-between"] = ConfigureTournament
            COMPONENTS["staggered"] = ConfigureTournament
            COMPONENTS["validate"] = ConfigureTournament

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
                + (
                    " - Tournament is staggered\n"
                    if staggered
                    else (
                        "\nIf you have 6, 7 or 11 players, you can configure a "
                        '"staggered" tournament, where each player sits a round out.\n'
                    )
                )
                + (
                    (
                        "\nIf this is a league (ie. more rounds are organized than the "
                        "number each player is authorized to play individually), "
                        f"you should use the {SetMaxRounds.mention()} command to set "
                        "this individual limit."
                    )
                    if between
                    else ""
                )
            ),
        )
        if not components:
            embed.description += (
                "\nRegistrations are now open.\n"
                f"Use the {Appoint.mention()} command to appoint judges, "
                "bots and spectators.\n"
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
        self.update()
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
            .add_button(hikari.ButtonStyle.DANGER, "confirm-close")
            .set_label("Close tournament")
            .add_to_container()
            .add_button(hikari.ButtonStyle.SECONDARY, "cancel-close")
            .set_label("Cancel")
            .add_to_container()
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
                            self.guils_id, judge, root_judge
                        )
                        for judge in root_judge_remove
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
                        self.bot.rest.delete_role(self.guild_id, role)
                        for role in self.discord.roles.values()
                    ),
                    return_exceptions=True,
                )
            )
            self.discord.channels.clear()
            self.discord.roles.clear()
            self.update()
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
        hikari.CommandOption(
            type=hikari.OptionType.STRING,
            name="decklist",
            description="Your decklist (Amaranth or VDB URL)",
            is_required=False,
        ),
    ]

    async def __call__(
        self,
        vekn: Optional[str] = None,
        name: Optional[str] = None,
        decklist: Optional[str] = None,
    ) -> None:
        if not self.tournament:
            raise CommandFailed("No tournament in progress")
        await self.deferred(flags=hikari.MessageFlag.EPHEMERAL)
        name = name and name[:4096]
        deck = None
        if decklist:
            deck = krcg.deck.Deck.from_url(decklist)
        discord_id = self.author.id
        try:
            player = await self.tournament.add_player(
                vekn, name, discord=discord_id, deck=deck, judge=False
            )
        except tournament.ErrorDecklistRequired:
            await self.create_or_edit_response(
                embed=hikari.Embed(
                    title="Decklist required",
                    description=(
                        f"You need to provide a decklist with {Register.mention()}. "
                        "You do not need to provide the other parameters (eg. `vekn`) "
                        "if you have done so already: only your decklist will update."
                    ),
                )
            )
            return
        await self.bot.rest.add_role_to_member(
            self.guild_id,
            discord_id,
            self.discord.roles[Role.PLAYER],
            reason=self.reason,
        )
        description = "You are successfully registered for the tournament."
        if player.playing:
            description = "You are ready to play."
        elif (
            self.tournament.flags & tournament.TournamentFlag.DECKLIST_REQUIRED
            and not player.deck
        ):
            description += (
                "\nA decklist is required to participate, please use this command "
                "again to provide one before the tournament begins."
            )
        else:
            description += (
                "\nPlease note you will need to confirm your presence by "
                "using the `checkin` command before the next round begins."
            )
        description += f"\n\nUse {Status.mention()} anytime to check your status."
        self.update()
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
            description="Your VEKN ID#",
            is_required=False,
        ),
        hikari.CommandOption(
            type=hikari.OptionType.STRING,
            name="name",
            description="Your name",
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
            description="user to register (if any)",
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
        deck = None
        if decklist:
            deck = krcg.deck.Deck.from_url(decklist)
        try:
            player = await self.tournament.add_player(
                vekn, name, discord=user, deck=deck, judge=True
            )
        except tournament.ErrorDecklistRequired:
            await self.create_or_edit_response(
                embed=hikari.Embed(
                    title="Decklist required",
                    description="Check-in is open: you need to provide a decklist.",
                )
            )
            return
        if user:
            await self.bot.rest.add_role_to_member(
                self.guild_id,
                user,
                self.discord.roles[Role.PLAYER],
                reason=self.reason,
            )
        self.update()
        player_display = self._player_display(player.vekn)
        description = f"{player_display} is successfully registered for the tournament."
        if player.playing:
            description = f"{player_display} is ready to play."
        elif (
            self.tournament.flags & tournament.TournamentFlag.DECKLIST_REQUIRED
            and not player.deck
        ):
            description += (
                "\nA decklist is required to participate, please use this command "
                "again to provide one before the tournament begins."
            )
        else:
            if self.tournament.state == tournament.TournamentState.PLAYING:
                description += (
                    "\nYou can add the player to the current round if you find a spot "
                    "for them on a table that has not yet begun to play by using "
                    f"{Round.mention('add')}."
                )
            else:
                description += (
                    "\nThe user will need to confirm their presence with the "
                    f"{CheckIn.mention()} command before next round begins.\n"
                )
        await self.create_or_edit_response(
            embed=hikari.Embed(
                title="Player registered",
                description=description,
            ),
        )


class CheckIn(Register):
    """Just an alias."""

    UPDATE = db.UpdateLevel.WRITE
    ACCESS = CommandAccess.PLAYER
    DESCRIPTION = "Check-in to play the next round"
    OPTIONS = []


class OpenCheckIn(BaseCommand):
    """Open the check-in so players can join the incoming round."""

    UPDATE = db.UpdateLevel.WRITE
    ACCESS = CommandAccess.JUDGE
    DESCRIPTION = "JUDGE: Open check-in to players for next round"
    OPTIONS = []

    async def __call__(self) -> None:
        self.tournament.open_checkin()
        self.update()
        await self.create_or_edit_response("Check-in is open")


class Drop(BaseCommand):
    """Drop from the tournament."""

    UPDATE = db.UpdateLevel.WRITE
    ACCESS = CommandAccess.PLAYER
    DESCRIPTION = "Drop from the tournament"
    OPTIONS = []

    async def __call__(self) -> None:
        await self.bot.rest.remove_role_from_member(
            self.guild_id,
            self.author,
            self.discord.roles[Role.PLAYER],
            reason=self.reason,
        )
        self.tournament.drop(self.author.id)
        self.update()
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
            await self.bot.rest.remove_role_from_member(
                self.guild_id,
                user,
                self.discord.roles[Role.PLAYER],
                reason=self.reason,
            )
        self.tournament.drop(user or vekn)
        self.update()
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
        self.tournament.drop(user or vekn, reason=tournament.DropReason.DISQUALIFIED)
        if note:
            self.tournament.note(
                user or vekn, self.author.id, tournament.NoteLevel.WARNING, note
            )
        self.update()
        await self.create_or_edit_response(
            f"<@{user}> Disqualified",
            user_mentions=[user],
        )


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
                        self.discord.roles[Role.JUDGE],
                        reason=self.reason,
                    ),
                    self.bot.rest.add_role_to_member(
                        self.guild_id,
                        user,
                        self.discord.roles[Role.ROOT_JUDGE],
                        reason=self.reason,
                    ),
                )
            )
        else:
            self.discord.spectators.append(user)
            await self.bot.rest.add_role_to_member(
                self.guild_id,
                user,
                self.discord.roles[Role.SPECTATOR],
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
        channel_id = self.discord.get_table_text_channel(table_num).id
        voice_channel = self.discord.get_table_voice_channel(table_num).id
        embed = hikari.Embed(
            title=f"Table {table_num} seating",
            description="\n".join(
                f"{j}. {self._player_display(p)}" for j, p in enumerate(table, 1)
            )
            + "\n\nThe first player should create the table.",
        )
        if voice_channel:
            embed.add_field(name="Join vocal", value=f"<#{voice_channel}>", inline=True)
        embed.add_field(
            name="Start the timer",
            value="`/timer start hours:2 minutes:30`",
            inline=True,
        )
        embed.set_thumbnail(hikari.UnicodeEmoji("🪑"))
        await self.bot.rest.create_message(channel_id, embed=embed)

    async def start(self) -> None:
        """Start a round. Dynamically optimise seating to follow official VEKN rules.

        Assign roles and create text and voice channels for players.
        """
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
        await self._align_roles()
        await self._align_channels()
        self.update()
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
        self.update()
        await self.create_or_edit_response("Round finished")

    async def reset(self) -> None:
        """Rollback round"""
        await self.deferred()
        round = self.tournament.reset_round()
        await self._delete_round_tables(round.finals)
        self.update()
        await self.create_or_edit_response("Round reset")

    async def add(
        self,
        table: int,
        user: Optional[hikari.Snowflake] = None,
        vekn: Optional[str] = None,
    ) -> None:
        """Add player to a 4-players table"""
        await self.deferred()
        self.tournament.round_add(user or vekn, table)
        if user:
            await self.bot.rest.add_role_to_member(
                self.guild_id,
                user,
                self.discord.roles[Role.PLAYER],
                reason=self.reason,
            )
        await self._display_seating(table)
        self.update()
        await self.create_or_edit_response(f"Player added to table {table}")

    async def remove(
        self, user: Optional[hikari.Snowflake] = None, vekn: Optional[str] = None
    ) -> None:
        """Remove player from a 5-players table"""
        await self.deferred()
        table = self.tournament.round_remove(user or vekn)
        if user:
            await self.bot.rest.remove_role_from_member(
                self.guild_id,
                user,
                self.discord.roles[Role.PLAYER],
                reason=self.reason,
            )
        await self._display_seating(table)
        self.update()
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
        self.update()
        seeding_embed = hikari.Embed(
            title="Finals seeding",
            description="\n".join(
                f"{j}. {self._player_display(p)}" for j, p in enumerate(table, 1)
            ),
        )
        await self.bot.rest.create_message(
            self.discord.get_table_text_channel(1),
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
        self.tournament.report(self.author.id, vp)
        self.update()
        info = self.tournament.player_info(self.author.id)
        if not info.table:
            return
        embed = hikari.Embed(
            title="Game report",
            description=(
                f"{self._player_display(self.author.id)} has reported "
                f"{vp}VP{'s' if vp > 1 else ''}"
            ),
        )
        channel_id = self.discord.get_table_text_channel(info.table).id
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
        self.tournament.report(user or vekn, vp, round)
        self.update()
        await self.create_or_edit_response(
            content=(
                f"Result registered: {vp} VPs for {self._player_display(user or vekn)}"
            ),
            flags=hikari.UNDEFINED
            if self._is_judge_channel()
            else hikari.MessageFlag.EPHEMERAL,
        )
        if round is not None:
            return
        info = self.tournament.player_info(user or vekn)
        if not info.table:
            return
        embed = hikari.Embed(
            title="Game report",
            description=(
                f"A judge has reported {vp}VP{'s' if vp > 1 else ''} for "
                f"{self._player_display(user or vekn)}"
            ),
        )
        channel_id = self.discord.get_table_text_channel(info.table).id
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
        self.update()
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
        vekn = self.tournament._check_player_id(user or vekn)
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
            confirmation = (
                confirmation.add_button(
                    hikari.ButtonStyle.DANGER, f"note-upgrade-{self.author.id}"
                )
                .set_label(f"Upgrade to {upgrade_component[1]}")
                .add_to_container()
            )
            COMPONENTS[f"note-upgrade-{self.author.id}"] = upgrade_component[2]
        confirmation = (
            confirmation.add_button(
                hikari.ButtonStyle.PRIMARY, f"note-continue-{self.author.id}"
            )
            .set_label("Continue")
            .add_to_container()
            .add_button(hikari.ButtonStyle.SECONDARY, "note-cancel")
            .set_label("Cancel")
            .add_to_container()
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
                name=f"Previous {previous_level}",
                value="\n".join(f"- <@{p.judge}> {p.text}" for p in previous_notes),
            )
        else:
            embed = hikari.Embed(
                title="Confirmation",
                description="",
            )
        embed.add_field(
            name=f"Your {level}",
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
                        self.discord.roles[Role.PLAYER],
                        reason=self.reason,
                    )
            self.update()
            if self.level == tournament.NoteLevel.NOTE:
                await self.create_or_edit_response(
                    embed=hikari.Embed(title="Note taken", description=self.note),
                    flags=hikari.MessageFlag.EPHEMERAL,
                    components=[],
                )
            else:
                embed = hikari.Embed(
                    title=f"{self.level} delivered",
                    description=f"{self._player_display(self.vekn)}: {self.note}",
                )
                table_text = None
                info = self.tournament.player_info(self.vekn)
                if info.table:
                    table_text = self.discord.get_table_text_channel(info.table)
                coroutines = [
                    self.create_or_edit_response(
                        embed=embed,
                        components=[],
                    )
                ]
                if table_text:
                    coroutines.append(
                        self.bot.rest.create_message(table_text, embed=embed)
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
        await self.deferred()
        judges_channel = self.discord.channels["TEXT"][Role.JUDGE]
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
            embed = hikari.Embed(
                title=f"{self.tournament.name} — Registrations open",
                description=(
                    f"{self.tournament.players.count} players registered\n"
                    f"**Use the {Register.mention()} command to register.**"
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
                        f"Use the {Register.mention()} command again "
                        "to add your decklist."
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
                    f"{checkin_time} using the {CheckIn.mention()} command."
                ),
            )
            judges_embed = hikari.Embed(
                title=embed.title,
                description=(
                    f"- {PlayersList.mention()} to check progression\n"
                    "    ✅ Checked in and ready to play\n"
                    "    ▶️ Playing\n"
                    "    ⌛️ Waiting for check-in\n"
                    "    📄 Missing decklist\n"
                    "    ❌ Checked out (dropped)\n"
                    "    ⛔️ Disqualified\n"
                    f"- {RegisterPlayer.mention()} to register players yourself\n"
                    f"- {DropPlayer.mention()} to remove a player\n"
                    f"- {OpenCheckIn.mention()} to allow check-in for the first round. "
                    "Registration will still be possible once you open check-in.\n"
                    "\n"
                    "**Important**\n"
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
                    "you need to provide either the user or their VEKN ID#. "
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
                    "a temporary ID to use as VEKN ID#, a short number prefixed with "
                    "`P-`."
                ),
            )
            await asyncio.gather(
                *(
                    self.create_or_edit_response(embed=embed),
                    self.bot.rest.create_message(judges_channel, embed=judges_embed),
                )
            )

        elif self.tournament.state == tournament.TournamentState.CHECKIN:
            players_role = self.discord.roles[Role.PLAYER]
            embed = hikari.Embed(
                title=(f"{self.tournament.name} — CHECK-IN — {current_round}"),
                description=(
                    "⚠️ **Check-in is required to play** ⚠️\n"
                    f"Please confirm your participation with the {CheckIn.mention()} "
                    "command.\n"
                    f"You can use {Status.mention()} to verify your status."
                ),
            )
            if (
                self.tournament.current_round == 0
                or self.tournament.flags & tournament.TournamentFlag.REGISTER_BETWEEN
            ):
                embed.add_field(
                    name="Registration",
                    value=(
                        "If you are not registered yet, you can still do so "
                        f"by using the {Register.mention()} command. "
                        "You will be checked in automatically."
                    ),
                )
            judges_embed = hikari.Embed(
                title=embed.title,
                description=(
                    f"- {PlayersList.mention()} to check progression\n"
                    f"- {RegisterPlayer.mention()} to register & check players in\n"
                    f"- {Round.mention('start')} when you're ready\n"
                ),
            )
            await asyncio.gather(
                *(
                    self.create_or_edit_response(
                        embed=embed,
                        content=f"<@&{players_role}>",
                        role_mentions=[players_role],
                    ),
                    self.bot.rest.create_message(judges_channel, embed=judges_embed),
                )
            )
        elif self.tournament.state == tournament.TournamentState.WAITING_FOR_CHECKIN:
            embed = hikari.Embed(
                title=(f"{self.tournament.name} — {current_round} finished"),
                description=(
                    "Waiting for next round to begin.\n"
                    f"You can use the {Status.mention()} command to verify your status."
                ),
            )
            embed.add_field(
                name="Check-in required",
                value=(
                    "Players will need to **check in again** for next round "
                    "(unless it is the finals)."
                ),
            )
            if self.tournament.flags & tournament.TournamentFlag.REGISTER_BETWEEN:
                embed.add_field(
                    name="Registration",
                    value=(
                        "If you are not registered yet, you can do so "
                        f"by using the {Register.mention()}  command. "
                        "You will also need to check in for the next round "
                        "before it begins, once the check-in is open."
                    ),
                )
            judges_embed = hikari.Embed(
                title=embed.title,
                description=(
                    f"- {OpenCheckIn.mention()}  to open the check-in for next round\n"
                    f"- {Standings.mention()}  to get current standings\n"
                    f"- {PlayersList.mention()}  to check the list of players\n"
                    f"- {DropPlayer.mention()}  to remove a player\n"
                    f"- {Finals.mention()}  to start the finals\n"
                ),
            )
            await asyncio.gather(
                *(
                    self.create_or_edit_response(embed=embed),
                    self.bot.rest.create_message(judges_channel, embed=judges_embed),
                )
            )
        elif self.tournament.state == tournament.TournamentState.WAITING_FOR_START:
            embed = hikari.Embed(
                title=(f"{self.tournament.name} — {current_round} starting soon"),
                description=(
                    "Waiting for the round to begin.\n"
                    f"You can use the {Status.mention()} command to verify your status."
                ),
            )
            if self.tournament.flags & tournament.TournamentFlag.REGISTER_BETWEEN:
                embed.add_field(
                    name="Registration",
                    value=(
                        "If you are not registered yet, you can still do so "
                        f"by using the {Register.mention()}  command. "
                        "You will be checked in automatically."
                    ),
                )
            judges_embed = hikari.Embed(
                title=embed.title,
                description=(
                    f"- {Standings.mention()}  to get current standings\n"
                    f"- {Round.mention('start')}  to start the next round\n"
                    f"- {PlayersList.mention()}  to check the list of players\n"
                    f"- {DropPlayer.mention()}  to remove a player\n"
                    f"- {Finals.mention()}  to start the finals\n"
                ),
            )
            await asyncio.gather(
                *(
                    self.create_or_edit_response(embed=embed),
                    self.bot.rest.create_message(judges_channel, embed=judges_embed),
                )
            )
        elif self.tournament.state == tournament.TournamentState.FINISHED:
            description = f"The {self.tournament.name} is finished.\n"
            winner = self.tournament.players.get(self.tournament.winner, None)
            if winner:
                description += (
                    f"Congratulations {self._player_display(winner.vekn)} "
                    "for your victory!"
                )
            embed = hikari.Embed(
                title=(f"{self.tournament.name} — {current_round} finished"),
                description=description,
            )
            if winner.deck:
                embed.add_field(name="Decklist", value=self._deck_display(winner.deck))
            judges_embed = hikari.Embed(
                title=embed.title,
                description=(
                    f"- {DownloadReports.mention()} to get the report files\n"
                    f"- {CloseTournament.mention()} to close this tournament\n"
                ),
            )
            await asyncio.gather(
                *(
                    self.create_or_edit_response(embed=embed),
                    self.bot.rest.create_message(judges_channel, embed=judges_embed),
                )
            )
        elif self.tournament.state == tournament.TournamentState.PLAYING:
            embed = hikari.Embed(
                title=(f"{self.tournament.name} — {current_round} in progress"),
                description=(
                    "Join your assigned table channels and enjoy your game.\n"
                    f"Use the {Status.mention()} command to **find your table**."
                ),
            )
            embed.add_field(
                name="Report your results",
                value=(
                    f"Use the {Report.mention()}  command to report "
                    "your Victory Points.\n"
                    "No need to report scores of zero."
                ),
            )
            if self.tournament.rounds[-1].finals:
                judges_embed = hikari.Embed(
                    title=embed.title,
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
                    title=embed.title,
                    description=(
                        f"- {Round.mention('add')} to add a player before game\n"
                        f"- {Round.mention('remove')} to remove a player before game \n"
                        f"- {Round.mention('reset')} to cancel the round and seating\n"
                        f"- {Note.mention()} to deliver cautions and warnings\n"
                        f"- {Results.mention()} to see the results\n"
                        f"- {FixReport.mention()} to correct them if needed\n"
                        f"- {ValidateScore.mention()} to force odd scores validation\n"
                        f"- {Round.mention('finish')} when all is good\n"
                        "\n"
                        f"You can still register a late arrival with "
                        "{RegisterPlayer.mention()} then add them to a 4-players table "
                        f"that has not started (if any) with {Round.mention('add')}."
                    ),
                )
            await asyncio.gather(
                *(
                    self.create_or_edit_response(embed=embed),
                    self.bot.rest.create_message(judges_channel, embed=judges_embed),
                )
            )


class Status(BaseCommand):
    """Player status. Provides guidance for lost souls."""

    UPDATE = db.UpdateLevel.READ_ONLY
    DESCRIPTION = "Check your current status"
    OPTIONS = []

    async def __call__(self) -> None:
        await self.deferred(hikari.MessageFlag.EPHEMERAL)
        judge_role = self.discord.roles[Role.JUDGE]
        embed = hikari.Embed(
            title=f"{self.tournament.name} — {self.tournament.players.count} players"
        )
        if self.author.id not in self.tournament.players:
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
                    f"{self.tournament.players.count} players registered.\n"
                    f"Register using the {Register.mention()} command."
                )
                if self.tournament.flags & tournament.TournamentFlag.VEKN_REQUIRED:
                    embed.description += (
                        "\nThis tournament requires a **VEKN ID#**. "
                        f"If you do not have one, ask a <@&{judge_role.id}> to help "
                        "with your registration."
                    )
        else:
            info = self.tournament.player_info(self.author.id, current_score=True)
            logger.debug("Player info: %s", info)
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
                if self.tournament.rounds[-1].finals:
                    seat = "seed"
                else:
                    seat = "seat"
                text_channel = self.discord.channels["TEXT"].get(info.table, None)
                voice_channel = self.discord.channels["VOICE"].get(info.table, None)
                if text_channel:
                    embed.description = (
                        f"You are {seat} {info.position} on <#{text_channel}>\n"
                    )
                else:
                    embed.description = (
                        f"You are {seat} {info.position} on table {info.table}\n"
                    )
                if voice_channel:
                    embed.description += f"\n**Join vocal:** <#{voice_channel}>"
                embed.description += (
                    f"\nUse the {Report.mention()} command to register your VPs"
                )
            elif info.status == tournament.PlayerStatus.CHECKED_IN:
                embed.description = (
                    "You are ready to play. You will be assigned a table and a seat "
                    "when the round starts."
                )
            elif info.status == tournament.PlayerStatus.CHECKIN_REQUIRED:
                embed.description = (
                    "⚠️ **You need to check-in** ⚠️\n"
                    f"Use the {CheckIn.mention()} command to check in "
                    "for the upcoming round."
                )
            elif info.status == tournament.PlayerStatus.MISSING_DECK:
                embed.description = (
                    "⚠️ **You need to provide your decklist** ⚠️\n"
                    f"Please use the {Register.mention()} command "
                    "to provide your decklist.\n"
                    f"You need to provide a [VDB]({VDB_URL}) "
                    f"or [Amaranth]({AMARANTH_URL}) link (URL)."
                )
            elif info.status == tournament.PlayerStatus.WAITING:
                if self.tournament.current_round == 0:
                    embed.description = (
                        "You are registered. Waiting for check-in to open."
                    )
                    if info.player.deck:
                        embed.description += (
                            "\nYour decklist has been saved. You can use the "
                            f"{Register.mention()} command again to update it."
                        )
                elif self.tournament.rounds[-1].finals:
                    embed.description = (
                        "You are done. Thanks for participating in this event!"
                    )
                elif (
                    self.tournament.flags & tournament.TournamentFlag.CHECKIN_EACH_ROUND
                ):
                    embed.description = (
                        "You will need to **check-in again** for next round, if any."
                    )
                else:
                    embed.description = (
                        "You are ready to play. Waiting for next round to start."
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
        info = self.tournament.player_info(vekn or user, current_score=True)
        description = self._player_display(info.player.vekn)
        description += (
            f"\n{info.rounds} round{'s' if info.rounds > 1 else ''} played {info.score}"
        )
        if info.drop and info.drop == tournament.DropReason.DROP:
            description += "\n**DROPPED**"
        elif info.drop and info.drop == tournament.DropReason.DISQUALIFIED:
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
                text_channel = self.discord.channels["TEXT"].get(info.table, None)
                voice_channel = self.discord.channels["VOICE"].get(info.table, None)
                if text_channel:
                    description = (
                        f"Player is {seat} {info.position} on <#{text_channel}>\n"
                    )
                else:
                    description = (
                        f"Player is {seat} {info.position} on table {info.table}\n"
                    )
                if voice_channel:
                    description += f"\n**Vocal:** <#{voice_channel}>"
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
        round.score()
        incorrect = set(round.incorrect)
        for i, table in enumerate(round.seating.iter_tables(), 1):
            scores = []
            for j, player_number in enumerate(table, 1):
                vekn = self.tournament._check_player_id(player_number)
                score = round.results.get(player_number, None) or tournament.Score()
                scores.append(f"{j}. {self._player_display(vekn)} {score}")
            embed.add_field(
                name=f"Table {i} " + ("⚠️" if i in incorrect else "☑️"),
                value="\n".join(scores),
                inline=True,
            )
        embeds = _paginate_embed(embed)
        await self.create_or_edit_response(embeds=embeds, flags=flag)


def status_icon(status: tournament.PlayerStatus) -> str:
    return {
        tournament.PlayerStatus.CHECKED_IN: "✅",
        tournament.PlayerStatus.DISQUALIFIED: "⛔️",
        tournament.PlayerStatus.PLAYING: "▶️",
        tournament.PlayerStatus.MISSING_DECK: "📄",
        tournament.PlayerStatus.CHECKIN_REQUIRED: "⌛️",
        tournament.PlayerStatus.WAITING: "",
        tournament.PlayerStatus.CHECKED_OUT: "❌",
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
        players = sorted(self.tournament.players.iter_players(), key=lambda p: p.number)
        playing = len([p for p in self.tournament.players.iter_players() if p.playing])
        total = self.tournament.players.count
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
        json.dump(data, buffer)
        buffer = io.BytesIO(buffer.getvalue().encode("utf-8"))
        return hikari.Bytes(buffer, filename, mimetype="application/json")

    def _build_results_csv(self):
        winner, ranking = self.tournament.standings()
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
        """Anonymized list of decks.

        Finding the player from his number is only possible with the official report.
        """
        data = []
        for player in sorted(
            self.tournament.players.iter_players(), key=lambda p: p.number
        ):
            info = self.tournament.player_info(player.vekn)
            if not self._report_number.get(player.vekn):
                continue
            data.append(
                {
                    "vekn": player.vekn,
                    "finals_seed": player.seed,
                    "rounds": info.rounds,
                    "score": info.score.to_json(),
                    "deck": player.deck,
                }
            )
        return self._build_json("Decks.json", data)

    def _player_first_last_name(self, player):
        if not player.name:
            return ["", ""]
        name = player.name.split(" ", 1)
        if len(name) < 2:
            name.append("")
        return name

    def _build_methuselahs_csv(self):
        data = []
        for player in sorted(
            self.tournament.players.iter_players(), key=lambda p: p.number
        ):
            if not self._report_number.get(player.vekn):
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
                    "DQ" if info.drop else "",
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
                for number in table:
                    player = self.tournament.players[number]
                    name = self._player_first_last_name(player)
                    data.append(
                        [
                            self._report_number[player.vekn],
                            name[0],
                            name[1],
                            j,
                            round.results.get(player.number, tournament.Score()).vp,
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
        if count < 1 or count > self.tournament.players.count:
            raise CommandFailed(
                "Invalid count: choose a number between 1 and "
                f"{self.tournament.players.count}"
            )
        players = random.sample(
            [
                p.vekn
                for p in self.tournament.players.iter_players()
                if p.vekn and p.vekn not in self.tournament.dropped
            ],
            k=count,
        )
        embed = hikari.Embed(
            title="Raffle Winners",
            description="\n".join(f"- {self._player_display(p)}" for p in players),
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
        self.update()
        embed = hikari.Embed(
            title="Channels reset",
            description="Channels have been realigned.",
        )
        await self.create_or_edit_response(embed=embed)


# TODO Fix the "last round" access for staggered tournaments
# TODO Upload decklist as txt file attachment
# TODO Make admin access admin-only
# TODO More buttons to guide user (especially on /status)
# TODO Fix player discord mentions during registration (disconnected players)
# TODO Remove TEXT channels, use voice channels chat instead
# TODO Use pydantic for serialization
