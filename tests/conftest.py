import asyncio
import contextlib
import functools
import io
import os
import pytest
import re
import unittest

from archon_bot import db


class Guild:
    def __init__(self, mock, uid=None):
        self.id = uid or 1
        self.name = "Test Guild"
        self.me = me
        self.channels = {}  # ID -> object
        self._roles = {}  # ID -> object
        self.members = {}
        self._mock = mock
        self.default_role = 0
        self._base_channel = self._create_channel("base")

    def get_role(self, uid):
        return self._roles.get(uid, None)

    def get_channel(self, uid):
        return self.channels.get(uid, None)

    def get_member(self, uid):
        return self.members.get(uid, None)

    async def create_role(self, name):
        uid = len(self._roles) + 1
        ret = Role(self, uid, name)
        self._roles[uid] = ret
        return ret

    @property
    def roles(self):
        return list(self._roles.values())

    async def create_text_channel(
        self, name, *, overwrites=None, category=None, reason=None, **options
    ):
        return self._create_channel(name, category)

    async def create_voice_channel(
        self, name, *, overwrites=None, category=None, reason=None, **options
    ):
        return self._create_channel(name, category)

    def _create_channel(self, name, category=None):
        uid = len(self.channels) + 1
        ret = Channel(self, uid, name, category)
        self.channels[uid] = ret
        return ret

    def _create_member(self, uid, name):
        ret = Member(self, uid, name)
        self.members[uid] = ret
        return ret

    def _get_role_by_name(self, name):
        return {v: k for k, v in self._roles.items()}.get(name)

    def _get_channel(self, name):
        try:
            return next(obj for obj in self.channels.values() if obj.name == "Judges")
        except StopIteration:
            return None


class Channel:
    def __init__(self, guild, uid, name, category=None):
        self.id = uid
        self.name = name
        self.mention = f"<#{name}>"
        self.guild = guild
        self.category = category
        self.deleted = False

    async def send(self, *args, **kwargs):
        self.guild._mock.send(*args, **kwargs)
        return Message(self.guild._mock)

    async def delete(self):
        try:
            del self.guild.channels[self.id]
        except KeyError:
            pass
        self.deleted = True


class Category:
    def __init__(self, guild, uid, name):
        self.id = uid
        self.name = name
        self.guild = guild


class Message:
    def __init__(self, mock):
        self._mock = mock

    async def pin(self, *args, **kwargs):
        return

    async def edit(self, *args, **kwargs):
        self._mock.edit(*args, **kwargs)

    async def delete(self, *args, **kwargs):
        self._mock.delete(*args, **kwargs)


class Role:
    def __init__(self, guild, uid, name):
        self.id = uid
        self.name = name
        self.mention = f"<@&{uid}>"
        self.guild = guild
        self.deleted = False
        self.members = set()

    def __hash__(self):
        return self.id

    def __eq__(self, rhs):
        return self.id == rhs.id

    async def delete(self):
        del self.guild._roles[self.id]
        for member in self.members:
            member.roles.discard(self)
        self.deleted = True


class Member:
    def __init__(self, guild, uid, name):
        self.id = uid
        self.display_name = name
        self.mention = f"<@{uid}>"
        self.guild = guild
        self.roles = set()

    def __hash__(self):
        return self.id

    def message(self, content, channel=None, attachment=None):
        mentions = []
        for mention in re.findall(r"<@([^>]*)>", content):
            print(mention)
            if mention.startswith("&"):
                mention = self.guild.get_role(mention[1:])
            else:
                if mention.startswith("!"):
                    mention = mention[1:]
                mention = self.guild.get_member(int(mention))
            if mention:
                mentions.append(mention)
        return unittest.mock.Mock(
            guild=self.guild,
            channel=channel or self.guild._base_channel,
            author=self,
            content=content,
            attachments=[attachment] if attachment else [],
            mentions=mentions,
        )

    async def add_roles(self, *roles, reason=None, atomic=True):
        self.roles.update(roles)
        for role in roles:
            role.members.add(self)

    @property
    def _roles_names(self):
        return {r.name for r in self.roles}


me = Member(None, 0, "archon")


def _get_content(send_call, with_params):
    if send_call[1]:
        if with_params:
            return send_call[1][0], send_call[2]
        else:
            return send_call[1][0]

    else:
        res = send_call[2]["embed"].to_dict()
        res.pop("type")
        return res


@contextlib.contextmanager
def message(client_mock, all=False, with_params=False):
    calls = client_mock.method_calls
    try:
        if all:
            yield [_get_content(c, with_params) for c in calls if c[0] == "send"]
        else:
            assert calls[-1][0] == "send"
            yield _get_content(calls[-1], with_params)
    finally:
        client_mock.reset_mock()


class File(io.BytesIO):
    async def read(self):
        return super().read()


def async_test(func):
    @functools.wraps(func)
    def wrap(*args, **kwargs):
        asyncio.run(func(*args, **kwargs))

    return wrap


@pytest.fixture()
def client_mock():
    with unittest.mock.patch("archon_bot.bot.client", user=me) as obj:
        yield obj
    try:
        os.remove(f"archon-{db.version}.db")
    except FileNotFoundError:
        pass
