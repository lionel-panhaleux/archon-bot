import asyncio
import enum
import inspect
import logging
import os
import stringcase
import uuid

from dataclasses import dataclass, field
from typing import (
    Optional,
    Callable,
    Awaitable,
    Union,
    TypeVar,
    Generator,
    Self,
    Hashable,
    Any,
)

import hikari
import hikari.api.special_endpoints as special

from . import db


logger = logging.getLogger()


class CommandAccess(enum.Enum):
    PUBLIC = enum.auto()
    ADMIN = enum.auto()


@dataclass
class Config:
    access: CommandAccess = CommandAccess.PUBLIC
    update: db.UpdateLevel = db.UpdateLevel.READ_ONLY
    deferred: bool = True
    ephemeral: bool = True
    create: bool = False  # for components: by default do not create a new message
    description: str = ""
    options_kwargs: dict[str, Any] = field(default_factory=dict)


@dataclass
class CommandDecl:
    name: str
    id: Optional[hikari.Snowflake] = None
    handler: Callable[..., Awaitable]
    options: list[hikari.CommandOption]
    config: Config


@dataclass
class SubCommandDecl(CommandDecl):
    base: str


@dataclass
class ComponentDecl:
    custom_id: str = None
    static: bool = False
    handler: Callable[..., Awaitable]
    config: Config


Handler = Callable[..., Awaitable] | type[Callable[..., Awaitable]]


class Register:
    DECLARED: dict[str, Union[CommandDecl, dict[str, SubCommandDecl]]] = {}
    COMMANDS: dict[str, CommandDecl] = {}
    COMPONENTS: dict[str, Handler] = {}
    MODALS: dict[str, Handler] = {}
    BOT_NAME: str = ""
    APPLICATION: hikari.Application | None = None

    @classmethod
    def add_component(cls, declaration: ComponentDecl):
        cls.COMPONENTS[declaration.custom_id] = declaration.handler
        if declaration.static:
            return

        async def delete_component():
            await asyncio.sleep(15 * 60)
            del cls.COMPONENTS[declaration.custom_id]

        asyncio.ensure_future(delete_component())

    @classmethod
    def add_modal(cls, custom_id: str, handler: Handler):
        cls.MODALS[custom_id] = handler

        async def delete_modal():
            await asyncio.sleep(15 * 60)
            del cls.MODALS[custom_id]

        asyncio.ensure_future(delete_modal())

    @classmethod
    def set_bot_name(cls, name: str):
        cls.BOT_NAME = name

    @classmethod
    def get_commands_to_declare(
        cls, api: hikari.RESTAware
    ) -> dict[str, special.SlashCommandBuilder]:
        ret = {}
        for key, value in cls.DECLARED.items():
            if isinstance(value, CommandDecl):
                builder = api.rest.slash_command_builder(
                    value.name, value.config.description
                )
                for option in value.options:
                    builder.add_option(option)
                ret[key] = builder
            else:
                builder = api.rest.slash_command_builder(
                    value.name, value.config.description
                )
                ret[key] = {}
                for subvalue in value.values():
                    if not isinstance(subvalue, CommandDecl):
                        raise NotImplementedError("Subcommand groups not implemented")
                    builder.add_option(
                        hikari.CommandOption(
                            type=hikari.OptionType.SUB_COMMAND,
                            name=subvalue.name,
                            description=subvalue.config.description,
                            options=subvalue.options,
                        )
                    )
                ret[key] = builder


T = TypeVar("T")


TYPE_TO_HIKARI = {
    int: hikari.OptionType.INTEGER,
    float: hikari.OptionType.FLOAT,
    hikari.Attachment: hikari.OptionType.ATTACHMENT,
    bool: hikari.OptionType.BOOLEAN,
    hikari.PartialChannel: hikari.OptionType.CHANNEL,
    Union[hikari.Role, hikari.User]: hikari.OptionType.MENTIONABLE,
    hikari.Role: hikari.OptionType.ROLE,
    str: hikari.OptionType.STRING,
    hikari.User: hikari.OptionType.USER,
}


def _doc_firstline(obj: Any):
    return (getattr(obj, "__doc__", None) or "").splitlines()[0]


def _doc_find(obj: Any, param: str):
    doc = getattr(obj, "__doc__", None) or ""
    pos = doc.find(param)
    if pos < 0:
        return doc.splitlines()[0]
    return doc[pos + len(param) :].strip(":-, ")


def _parse_options(obj: type[T], config) -> Generator[hikari.CommandOption, None, None]:
    spec = inspect.getfullargspec(obj.__call__)
    if spec.args[1:]:
        raise ValueError(f"Non-keyword-only args in {obj.__name__} __call__ method")
    for param in spec.kwonlyargs:
        if param not in spec.annotations:
            raise ValueError(f"No type hint for parameter {param} in {obj.__name__}")
        if spec.annotations[param] not in TYPE_TO_HIKARI:
            raise ValueError(f"Invalid type for parameter {param} in {obj.__name__}")
        param_kwargs = config.get(param, {})
        if "description" not in param_kwargs:
            param_kwargs["description"] = _doc_find(obj.__call__, param)
        if param not in spec.kwonlydefaults:
            param_kwargs["is_required"] = True
        yield hikari.CommandOption(
            type=TYPE_TO_HIKARI[spec.annotations[param]],
            name=param,
            is_required=param not in spec.kwonlydefaults,
            **param_kwargs,
        )


def command(cls: type[T], config: Config) -> type[T]:
    """Decorator to declare bot slash commands"""
    if not config.description:
        config.description = _doc_firstline(cls)
    if "__call__" not in [m[0] for m in inspect.getmembers(cls)]:
        raise ValueError(f"Decorated command {cls.__name__} has no __call__ method")
    name = stringcase.spinalcase(cls.__qualname__.replace(".", " "))
    if name in Register.DECLARED_COMMANDS:
        raise ValueError(f"Command {name} is already declared somewhere else")
    Register.DECLARED_COMMANDS[name] = CommandDecl(
        name=stringcase.spinalcase(cls.__name__),
        handler=cls,
        options=list(_parse_options(cls, config.options_kwargs)),
        config=config,
    )
    return cls


def sub_command(cls: type[T], config: Config) -> type[T]:
    """Decorator to declare bot slash subcommands"""
    tree = cls.__qualname__.split(".")
    if len(tree) > 3:
        raise ValueError("Discord supports only 2 levels of command nesting")
    if len(tree) > 2:
        raise ValueError("Subcommand groups not implemented")
    if len(tree) < 2:
        raise ValueError("Subcommand groups not implemented")
    base = stringcase.spinalcase(tree[0])
    name = stringcase.spinalcase(tree[1])
    if name in Register.DECLARED_COMMANDS.get(base, {}):
        raise ValueError(f"SubCommand {name} is already declared in {base}")
    Register.DECLARED_COMMANDS.setdefault(base, {})
    Register.DECLARED_COMMANDS[base][name] = SubCommandDecl(
        base=base,
        name=stringcase.spinalcase(cls.__name__),
        handler=cls,
        options=list(_parse_options(cls, config.options_kwargs)),
        config=config,
    )
    return cls


def component(cls: type[T], config: Config) -> type[T]:
    """Decorator to declare message components"""
    if "__call__" not in [m[0] for m in inspect.getmembers(cls)]:
        raise ValueError(f"Decorated command {cls.__name__} has no __call__ method")
    name = stringcase.spinalcase(cls.__qualname__.replace(".", " "))
    if name in Register.COMPONENTS:
        raise ValueError(f"Component {name} is already declared somewhere else")
    Register.COMPONENTS[name] = ComponentDecl(
        name=name,
        handler=cls,
        config=config,
        static=True,
    )
    return cls


def _get_command(cls: type[T]) -> CommandDecl:
    tree = [stringcase.spinalcase(n) for n in cls.__qualname__.split(".")]
    try:
        mapping = Register.DECLARED
        while tree:
            key = tree.pop(0)
            mapping = mapping[key]
    except KeyError:
        pass
    else:
        return mapping
    name = stringcase.spinalcase(cls.__qualname__.replace(".", ""))
    return Register.DECLARED[name]


class MetaInteraction(type):
    """Metaclass to register static interactions."""

    def __new__(cls, name, bases, dict_):
        command_name = stringcase.spinalcase(name)
        if command_name in Register.DECLARED:
            raise ValueError(f"Command {name} is already registered")
        klass = super().__new__(cls, name, bases, dict_)
        if hasattr(klass, "config"):
            config = klass.config
        else:
            config = Config()
        return command(klass, config)


@dataclass
class InteractionContext:
    has_response: bool = False
    interaction_type = hikari.InteractionType
    mode: hikari.ResponseType = hikari.ResponseType.MESSAGE_CREATE
    bot: hikari.GatewayBot
    interaction: Union[
        hikari.CommandInteraction,
        hikari.ComponentInteraction,
        hikari.ModalInteraction,
        hikari.AutocompleteInteraction,
    ]
    category_id: hikari.Snowflake
    db_connection: db.psycopg.AsyncConnection

    @property
    def channel_id(self):
        return self.interaction.channel_id

    @property
    def author_id(self):
        return self.interaction.author_id


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


def _paginate_embed(embed: hikari.Embed) -> list[hikari.Embed]:
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


@dataclass
class ModalResponse:
    title: str
    custom_id: str
    components: list[special.ModalActionRowBuilder] = field(default_factory=list)


@dataclass
class TextSelectOption:
    label: str
    value: str
    description: str | None = None
    emoji: hikari.Snowflakeish | hikari.Emoji | str | None = None
    is_default: bool = False


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
class ChannelDecl:
    type_: hikari.ChannelType
    name: str
    permissions: list[hikari.PermissionOverwrite] = field(default_factory=list)


@dataclass
class RoleDecl:
    name: str
    user_ids: list[hikari.Snowflakeish]
    channel_keys: list[Hashable]  # keys in the channel registry if any
    global_prefix: bool = True  # Use of global bot prefix over interaction prefix
    mentionable: bool = True
    color: hikari.Colorish | None = None
    hoist: bool = False
    icon: hikari.Resourceish | None = None
    unicode_emoji: str | None = None


class Interaction(hikari.RESTAware):
    def __init__(self, cfg: Config, ctx: InteractionContext):
        self.cfg: Config = cfg
        self.ctx: InteractionContext = ctx
        self.embed = hikari.Embed()
        self.components: list[special.MessageActionRowBuilder] = []
        self.attachments: list[hikari.Attachment] = []
        self.choices: list[special.AutocompleteChoiceBuilder] = []
        self.modal: Optional[ModalResponse] = None
        self._prefix: str | None = None

    @property
    def rest(self):
        return self.ctx.bot.rest

    @property
    def prefix(self):
        return self._prefix or Register.PREFIX

    async def prepare(self):
        if self.cfg.deferred and not self.ctx.has_response:
            await self.ctx.interaction.create_initial_response(
                hikari.ResponseType.DEFERRED_MESSAGE_CREATE,
                flags=hikari.MessageFlag.EPHEMERAL if self.cfg.ephemeral else None,
            )
            self.ctx.has_response = True

    async def chain(self, interaction: Union[type[Self], Self]):
        if interaction.cfg.access > db.UpdateLevel.READ_ONLY:
            await self.ctx.db_connection.set_read_only(False)
        if isinstance(interaction, type):
            handler = interaction(self.ctx)
        else:
            handler = interaction
            handler.ctx = self.ctx
        await handler()

    async def respond(self) -> None:
        if isinstance(self.ctx.interaction, hikari.AutocompleteInteraction):
            await self.rest.create_autocomplete_response(
                interaction=self.ctx.interaction,
                token=self.ctx.interaction.token,
                choices=self.choices,
            )
            return
        if self.modal:
            await self.rest.create_modal_response(
                interaction=self.ctx.interaction,
                token=self.ctx.interaction.token,
                title=self.modal.title,
                custom_id=self.modal.custom_id,
                components=self.modal.components,
            )
            return
        if self.ctx.has_response or (
            # component or modal submission
            hasattr(self.ctx.interaction, "message")
            and not self.cfg.create
        ):
            response_type = hikari.ResponseType.MESSAGE_UPDATE
            flags = None
        else:
            response_type = hikari.ResponseType.MESSAGE_CREATE
            flags = hikari.MessageFlag.EPHEMERAL if self.cfg.ephemeral else None
        self.rest.create_interaction_response(
            interaction=self.ctx.interaction,
            token=self.ctx.interaction.token,
            response_type=response_type,
            flags=flags,
            embeds=_paginate_embed(self.embed),
            components=self.components,
            attachments=self.attachments,
        )

    def add_button(
        self,
        handler: Handler,
        style: hikari.ButtonStyle,
        config: Config | None = None,
        emoji: hikari.Snowflakeish | hikari.Emoji | str | None = None,
        label: str | None = None,
        url: str | None = None,
        is_disabled: bool = False,
    ):
        row = self._auto_button_row()
        if style == hikari.ButtonStyle.LINK:
            if not url:
                raise ValueError("The url parameter is required for a link button")
            row.add_link_button(
                url=url,
                emoji=emoji,
                label=label,
                is_disabled=is_disabled,
            )
        else:
            custom_id = self._build_cutsom_id(handler)
            row.add_interactive_button(
                style=style,
                custom_id=custom_id,
                emoji=emoji,
                label=label,
                is_disabled=is_disabled,
            )
            if custom_id not in Register.COMPONENTS:
                declaration = ComponentDecl(
                    custom_id=custom_id, handler=handler, config=config or self.cfg
                )
                Register.add_component(declaration)

    def add_select(
        self,
        handler: Handler,
        type_: hikari.ComponentType,
        config: Config | None = None,
        placeholder: str | None = None,
        min_values: int = 0,
        max_values: int = 1,
        options: list[TextSelectOption] = [],
        channel_types: list[hikari.ChannelType] = [],
        is_disabled: bool = False,
    ):
        row = self.new_message_action_row()
        custom_id = self._build_cutsom_id(handler)
        if type_ == hikari.ComponentType.TEXT_SELECT_MENU:
            text_menu = row.add_text_menu(
                custom_id=custom_id,
                placeholder=placeholder,
                min_values=min_values,
                max_values=max_values,
                is_disabled=is_disabled,
            )
            for option in options:
                text_menu.add_option(option)
        elif type_ == hikari.ComponentType.CHANNEL_SELECT_MENU:
            row.add_channel_menu(
                custom_id=custom_id,
                channel_types=channel_types,
                placeholder=placeholder,
                min_values=min_values,
                max_values=max_values,
                is_disabled=is_disabled,
            )
        else:
            row.add_select_menu(
                type_=type_,
                custom_id=custom_id,
                placeholder=placeholder,
                min_values=min_values,
                max_values=max_values,
                is_disabled=is_disabled,
            )
        if custom_id not in Register.COMPONENTS:
            declaration = ComponentDecl(
                custom_id=custom_id, handler=handler, config=config or self.cfg
            )
            Register.add_component(declaration)

    def add_modal(
        self,
        handler: Handler,
        modal: ModalResponse,
        config: Config | None = None,
    ):
        custom_id = self._build_cutsom_id(handler)
        self.modal = modal
        Register.add_modal(custom_id, handler)

    def _build_cutsom_id(self, handler: Handler):
        if isinstance(handler, type):
            custom_id = stringcase.spinalcase(handler.__qualname__.replace(".", " "))
        else:
            custom_id = str(uuid.uuid4())
        return custom_id

    def _auto_button_row(self):
        if (
            not self.components
            or len(self.components[-1]) >= 5
            or isinstance(self.components[-1].components[-1], special.SelectMenuBuilder)
        ):
            self.new_message_action_row()
        return self.components[-1]

    def new_message_action_row(self):
        if len(self.components) >= 5:
            raise ValueError("5 action rows is the maximum")
        self.components.append(special.ModalActionRowBuilder())
        return self.components[-1]

    async def align_channels(
        self,
        expected: dict[Hashable, ChannelDecl],
        registry: dict[Hashable, DiscordChannel],
        silence_exceptions: bool = False,
    ) -> None:
        # delete spurious keys from registry
        to_delete = []
        logger.debug("expected channels: %s", list(expected.keys()))
        for key, channel in registry.items():
            if key not in expected:
                to_delete.append(key)
        for key in to_delete:
            logger.debug(
                "deleting unexpected channel from registry: %s: %s",
                key,
                registry[key],
            )
            del registry[key]
        # compare what exists with what is registered
        registered = {c.id for c in registry.values()}
        logger.debug("registered channels: %s", registry)
        existing = await self.rest.fetch_guild_channels(self.ctx.interaction.guild_id)
        if self.ctx.category_id:
            existing = [c for c in existing if c.parent_id == self.ctx.category_id]
        existing = [
            c for c in existing if c.name.lower().startswith(self.cfg.prefix + "-")
        ]
        logger.debug("existing channels on discord: %s", existing)
        to_delete = [c for c in existing if c.id not in registered]
        if to_delete:
            logger.debug("deleting channels on discord: %s", to_delete)
            # delete spurious from discord
            result = await asyncio.gather(
                *(self.rest.delete_channel(c.id) for c in to_delete),
                return_exceptions=silence_exceptions,
            )
            errors = [
                r for r in result if isinstance(r, hikari.ClientHTTPResponseError)
            ]
            if errors:
                logger.warning("errors closing channels: %s", errors)
        existing = {c.id for c in existing if c.id in registered}
        # delete spurious from registry
        to_delete = []
        for key, channel in registry.items():
            if channel.id not in existing:
                to_delete.append(key)
        for key in to_delete:
            logger.debug(
                "deleting unavailable channel from registry: %s: %s",
                key,
                registry[key],
            )
            del registry[key]

        # the registry now matches discord
        # create what is missing both on discord and in registry
        keys_to_create = []
        to_create = []
        channel_type_creator = {
            hikari.ChannelType.GUILD_FORUM: self.rest.create_guild_forum_channel,
            hikari.ChannelType.GUILD_NEWS: self.rest.create_guild_news_channel,
            hikari.ChannelType.GUILD_STAGE: self.rest.create_guild_stage_channel,
            hikari.ChannelType.GUILD_TEXT: self.rest.create_guild_text_channel,
            hikari.ChannelType.GUILD_VOICE: self.rest.create_guild_voice_channel,
        }
        for key, declaration in expected.items():
            if key in registry:
                continue
            keys_to_create.append(key)
            logger.debug("creating channel on discord: %s, %s", key, declaration.name)
            to_create.append(
                channel_type_creator[declaration.type_](
                    self.guild_id,
                    declaration.name,
                    category=self.category_id or hikari.UNDEFINED,
                    permission_overwrites=declaration.permissions,
                )
            )

        result = await asyncio.gather(*to_create, return_exceptions=silence_exceptions)
        errors = [r for r in result if isinstance(r, hikari.HikariError)]
        if errors:
            logger.warning("errors creating channels: %s", errors)
        for key, res in zip(keys_to_create, result):
            if isinstance(res, hikari.HikariError):
                continue
            logger.debug("add channel to registry: %s, %s", key, res)
            self.discord.channels[key] = DiscordChannel.from_hikari(res)
        logger.debug("channels aligned")

    async def _get_role_name(self, declaration: RoleDecl):
        if declaration.global_prefix:
            return f"{Register.BOT_NAME}-{declaration.name}"
        else:
            return f"{self.prefix}-{declaration.name}"

    async def align_roles(
        self,
        expected: dict[Hashable, RoleDecl],
        registry: dict[Hashable, DiscordRole],
        channels_registry: dict[Hashable, ChannelDecl] = {},
        silence_exceptions: bool = False,
    ) -> None:
        # list what is expected
        logger.debug("expected roles: %s", expected.keys())
        # delete spurious keys from registry
        to_delete = []
        for key in registry.keys():
            if key not in expected:
                to_delete.append(key)
        for key in to_delete:
            logger.debug(
                "deleting unexpected role from registry: %s: %s",
                key,
                registry[key],
            )
            del registry[key]
        # compare what exists with what is registered
        expected_names = {self._get_role_name(r) for r in expected.values()}
        existing = await self.rest.fetch_roles(self.guild_id)
        existing = [r for r in existing if r.name in expected_names]
        logger.debug("existing roles on discord: %s", existing)
        registered = {r.id for r in registry.values()}
        logger.debug("registered roles: %s", registry)
        # delete spurious from discord
        to_delete = [r.id for r in existing if r.id not in registered]
        if to_delete:
            logger.warning("deleting unexpected roles on discord: %s", to_delete)
            # delete spurious from discord
            await asyncio.gather(
                *(self.rest.delete_role(self.guild_id, r) for r in to_delete),
                return_exceptions=silence_exceptions,
            )
        existing = {r.id for r in existing if r.id in registered}
        # delete spurious from registry (do not delete the root judge)
        to_delete = []
        for key, role in registry.items():
            if role.id not in existing:
                to_delete.append(key)
        for key in to_delete:
            logger.debug(
                "deleting unavailable role from registry: %s: %s",
                key,
                registry[key],
            )
            del registry[key]
        # now discord and internal registry are aligned
        # create what is missing both on discord and in registry
        keys_to_create = []
        roles_to_create = []
        for key, declaration in expected:
            if key not in registry:
                name = self._get_role_name(declaration)
                logger.debug("creating role on discord: %s, %s", key, name)
                keys_to_create.append(key)
                roles_to_create.append(
                    self.rest.create_role(
                        self.guild_id,
                        name=name,
                        color=declaration.color,
                        hoist=declaration.hoist,
                        icon=declaration.icon,
                        unicode_emoji=declaration.unicode_emoji,
                        mentionable=declaration.mentionable,
                    )
                )
        roles = await asyncio.gather(
            *roles_to_create, return_exceptions=silence_exceptions
        )
        errors = [r for r in roles if isinstance(r, hikari.HikariError)]
        if errors:
            logger.warning("errors creating channels: %s", errors)
        # assign the newly created roles to the guild members
        id_roles = []
        for key, role in zip(keys_to_create, roles):
            logger.debug("creating role in registry: %s, %s", key, role)
            registry[key] = DiscordRole.from_hikari(role)
            declaration = expected[key]
            # if the role has private channels, we must drop them
            for key in declaration.channel_keys:
                channels_registry.pop()
            id_roles.extend((id, role) for id in declaration.user_ids)

        if id_roles:
            logger.debug("assigning roles: %s", id_roles)
            results = await asyncio.gather(
                *[
                    self.rest.add_role_to_member(
                        self.guild_id,
                        snowflake,
                        role,
                    )
                    for snowflake, role in id_roles
                ],
                return_exceptions=silence_exceptions,
            )
            errors = [r for r in results if isinstance(r, hikari.HikariError)]
            if errors:
                logger.warning("errors assigning roles to member: %s", errors)
        logger.debug("roles aligned")


class Command(Interaction):
    @classmethod
    def mention(cls):
        name = " ".join(stringcase.spinalcase(a) for a in cls.__qualname__.split("."))
        return f"</{name}:{_get_command(cls).id}>"


class Component(Interaction):
    pass


class Modal(Interaction):
    pass


class Autocomplete(Interaction):
    pass


bot = hikari.GatewayBot(
    os.getenv("DISCORD_TOKEN") or "", logs="TRACE_HIKARI" if __debug__ else "INFO"
)
UPDATE = os.getenv("UPDATE")
RESET = os.getenv("RESET")


@bot.listen()
async def on_ready(event: hikari.StartedEvent) -> None:
    """Setup app commands and connect to the database."""
    logger.info("Ready as %s", bot.get_me().username)
    Register.set_bot_name(bot.get_me().username)
    await db.POOL.open()
    if RESET:
        await db.reset()
    await db.init()
    if not Register.APPLICATION:
        Register.APPLICATION = await bot.rest.fetch_application()

    commands = Register.get_commands_to_declare(bot)
    try:
        registered = await bot.rest.fetch_application_commands(
            application=Register.APPLICATION,
        )
        if UPDATE or set(c.name for c in commands) ^ set(c.name for c in registered):
            logger.info("Updating commands: %s", commands)
            registered = await bot.rest.set_application_commands(
                application=Register.APPLICATION,
                commands=commands,
            )
    except hikari.ForbiddenError:
        logger.exception("Bot does not have commands permission")
        return
    except hikari.BadRequestError:
        logger.exception("Bot did not manage to update commands")
        return
    # align Register.DECLARED and Register.COMMANDS
    for command in registered:
        try:
            Register.DECLARED[command.name].id = command.id
            Register.COMMANDS[command.id] = Register.DECLARED[command.name]
        except KeyError:
            logger.exception("Received unknow command %s", command)


@bot.listen()
async def on_stopped(event: hikari.StoppedEvent) -> None:
    """Disconnect from the database, close all running tasks"""
    await db.POOL.close()
    pending = asyncio.all_tasks()
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending)


@bot.listen()
async def on_connected(event: hikari.GuildAvailableEvent) -> None:
    """Connected to a guild."""
    logger.info("Logged in %s as %s", event.guild.name, bot.get_me().username)


HikariInteraction = (
    hikari.CommandInteraction
    | hikari.ModalInteraction
    | hikari.ComponentInteraction
    | hikari.AutocompleteInteraction
)


async def _get_category_id(interaction: Interaction) -> hikari.Snowflake:
    channel = interaction.app.cache.get_guild_channel(interaction.channel_id)
    if not channel:
        channel = await interaction.fetch_channel()
    return channel.parent_id


class BotError(RuntimeError):
    def __init__(self, msg: str, *args: object) -> None:
        super().__init__(*args)
        self.msg = msg


def _resolve_option(
    interaction: hikari.CommandInteraction | hikari.ComponentInteraction,
    option: hikari.SelectMenuOption | hikari.CommandInteractionOption,
):
    match option.type:
        case hikari.OptionType.ATTACHMENT:
            return option.name, interaction.resolved.attachments[option.value]
        case hikari.OptionType.CHANNEL:
            return option.name, interaction.resolved.channels[option.value]
        case hikari.OptionType.ROLE:
            return option.name, interaction.resolved.roles[option.value]
        case hikari.OptionType.USER:
            return option.name, interaction.resolved.users[option.value]
        case hikari.OptionType.MENTIONABLE:
            if option.value in interaction.resolved.users:
                return option.name, interaction.resolved.users[option.value]
            else:
                return option.name, interaction.resolved.roles[option.value]
        case _:
            return option.name, option.value


async def _handle_error(
    interaction: hikari.CommandInteraction
    | hikari.ModalInteraction
    | hikari.ComponentInteraction,
    ctx: InteractionContext,
    err: BotError,
):
    if ctx.has_response:
        await interaction.update_initial_response(
            embeds=[hikari.Embed(title="Error", description=err.msg)],
            attachments=None,
            components=None,
        )
    else:
        await interaction.create_initial_response(
            embeds=[hikari.Embed(title="Error", description=err.msg)],
            flags=hikari.MessageFlag.EPHEMERAL,
            attachments=None,
            components=None,
        )


@bot.listen()
async def on_interaction(event: hikari.InteractionCreateEvent) -> None:
    """Handle interactions (slash commands)."""
    category_id = _get_category_id(event.interaction)
    match event.interaction.type:
        case hikari.InteractionType.APPLICATION_COMMAND:
            interaction: hikari.CommandInteraction = event.interaction
            command = Register.COMMANDS[interaction.id]
            async with db.connection(
                event.interaction.guild_id, category_id, command.config.update
            ) as conn:
                if isinstance(command.handler, type):
                    ctx = InteractionContext(
                        bot=bot,
                        interaction=interaction,
                        db_connection=conn,
                        category_id=category_id,
                    )
                    handler = command.handler(cfg=command.config, ctx=ctx)
                else:
                    handler.ctx.db_connection = conn
                    handler.ctx.interaction = interaction
                kwargs = dict(
                    _resolve_option(interaction, option)
                    for option in interaction.options or []
                )
                try:
                    await handler(**kwargs)
                except BotError as e:
                    await _handle_error(interaction, handler.ctx, e)
        case hikari.InteractionType.AUTOCOMPLETE:
            interaction: hikari.AutocompleteInteraction = event.interaction
            option = interaction.options[0]
            handler = Register.COMMANDS[interaction.id]
            async with db.connection(
                event.interaction.guild_id, category_id, command.config.update
            ) as conn:
                if not isinstance(command.handler, type):
                    raise ValueError(
                        "Autocompletes expect an Interaction class "
                        "with autocomplete() function"
                    )
                ctx = InteractionContext(
                    bot=bot,
                    interaction=interaction,
                    db_connection=conn,
                    category_id=category_id,
                )
                handler = command.handler(cfg=command.config, ctx=ctx)
                try:
                    await handler.autocomplete(**{option.name: option.value})
                except BotError:
                    logger.exception("Failed to autocomplete")
        case hikari.InteractionType.MESSAGE_COMPONENT:
            interaction: hikari.ComponentInteraction = event.interaction
            option = interaction.options[0]
            component = Register.COMPONENTS[interaction.custom_id]
            async with db.connection(
                interaction.guild_id, category_id, component.config.update
            ) as conn:
                if isinstance(component.handler, type):
                    ctx = InteractionContext(
                        bot=bot,
                        interaction=interaction,
                        db_connection=conn,
                        category_id=category_id,
                    )
                    handler = component.handler(cfg=component.config, ctx=ctx)
                else:
                    handler.ctx.db_connection = conn
                    handler.ctx.interaction = interaction
                try:
                    await handler(**kwargs)
                except BotError as e:
                    await _handle_error(interaction, handler.ctx, e)
        case hikari.InteractionType.MODAL_SUBMIT:
            interaction: hikari.ModalInteraction = event.interaction
            component = Register.COMPONENTS[interaction.custom_id]
            kwargs = {
                field.custom_id: field.value
                for row in interaction.components
                for field in row.components
            }
            async with db.connection(
                interaction.guild_id, category_id, component.config.update
            ) as conn:
                if isinstance(component.handler, type):
                    ctx = InteractionContext(
                        bot=bot,
                        interaction=interaction,
                        db_connection=conn,
                        category_id=category_id,
                    )
                    handler = component.handler(cfg=component.config, ctx=ctx)
                else:
                    handler.ctx.db_connection = conn
                    handler.ctx.interaction = interaction
                try:
                    await handler(**kwargs)
                except BotError as e:
                    await _handle_error(interaction, handler.ctx, e)
