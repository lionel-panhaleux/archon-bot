import asyncio
import collections
import csv
import io
import itertools
import math
import logging
import os
import random

import aiohttp
import asgiref.sync
import discord
import stringcase

import krcg.seating
import krcg.utils


from . import db
from . import tournament
from . import permissions as perm

logger = logging.getLogger()

#: Iterations for the seating algorithm (higher is best but takes longer)
ITERATIONS = 20000
VEKN_LOGIN = os.getenv("VEKN_LOGIN")
VEKN_PASSWORD = os.getenv("VEKN_PASSWORD")


class CommandFailed(Exception):
    """A "normal" failure: an answer explains why the command was not performed"""


COMMANDS = {}


class MetaCommand(type):
    """Metaclass to register commands."""

    def __new__(cls, name, bases, dict_):
        command_name = stringcase.spinalcase(name)
        if command_name in COMMANDS:
            raise ValueError(f"Command {name} is already registered")
        if command_name == "command":
            command_name = ""
        COMMANDS[command_name] = super().__new__(cls, name, bases, dict_)
        return COMMANDS[command_name]


class Command(metaclass=MetaCommand):
    """Base class for all caommnds, implements the default `archon` command."""

    #: The command does not update the tournament data. Override in children as needed.
    UPDATE = False

    def __init__(self, connection, message, data=None):
        self.connection = connection
        self.message = message
        self.author = self.message.author
        self.channel = self.message.channel
        self.guild = self.channel.guild
        self.category = self.channel.category
        self.tournament = tournament.Tournament(**(data or {}))
        self.scores = collections.defaultdict(tournament.Score)
        self.winner = None

    def update(self):
        """Update tournament data."""
        assert self.UPDATE
        data = self.tournament.to_json()
        logger.debug("update %s: %s", self.guild.name, data)
        db.update_tournament(
            self.connection,
            self.guild.id,
            self.category.id if self.category else None,
            data,
        )

    async def send(self, message):
        """Send a Discord message, split it if necessary, reference request."""
        rest = ""
        while message:
            message, rest = self._split_text(message, 2000)
            await self.channel.send(message, reference=self.message)
            message = rest
            rest = ""

    async def send_embed(self, embed):
        """Send a Discord embed, paginate it if necessary."""
        messages = []
        fields = []
        base_title = embed.title
        description = ""
        page = 1
        embed = embed.to_dict()
        logger.debug("embed: %s", embed)
        while embed:
            if "description" in embed:
                embed["description"], description = self._split_text(
                    embed["description"], 2048
                )
            while embed.get("fields") and (len(embed["fields"]) > 15 or description):
                fields.append(embed["fields"][-1])
                embed["fields"] = embed["fields"][:-1]
            messages.append(
                await self.channel.send(embed=discord.Embed.from_dict(embed))
            )
            if description or fields:
                page += 1
                embed = {
                    "title": base_title + f" ({page})",
                    "description": description,
                    "fields": list(reversed(fields[:])),
                }
                description = ""
                fields = []
            else:
                embed = None
        return messages

    def _split_text(self, s, limit):
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

    def _check_tournament(self):
        """Check if there is a running tournament or raise CommandFailed."""
        if self.tournament:
            return
        raise CommandFailed("No tournament in progress")

    def _check_judge(self, message=None):
        """Check if author is a judge or raise CommandFailed."""
        self._check_tournament()
        judge_role = self.guild.get_role(self.tournament.judge_role)
        if judge_role in self.author.roles:
            return
        message = message or f"Only a {judge_role.mention} can issue this command"
        raise CommandFailed(message)

    def _check_judge_private(self):
        """Check if we are in the judges channel or raise CommandFailed."""
        self._check_judge()
        judge_channel = self.guild.get_channel(
            self.tournament.channels[self.tournament.JUDGES_TEXT]
        )
        if self.channel.id == judge_channel.id:
            return
        raise CommandFailed(
            f"This command can only be issued in the {judge_channel.mention} channel"
        )

    def _check_current_round_modifiable(self):
        """Check if the round can be modified or raise CommandFailed."""
        self._check_tournament()
        if not self.tournament.current_round:
            raise CommandFailed("No seating has been done yet")
        if self._round_results():
            raise CommandFailed(
                "Some tables have reported their result, unable to modify seating."
            )

    def _check_round(self, round=None):
        """Check if this is a valid round number or raise CommandFailed."""
        self._check_tournament()
        if not self.tournament.current_round:
            raise CommandFailed("Tournament has not begun")
        if len(self.tournament.results) < self.tournament.current_round:
            self.tournament.results.append({})
        if round and len(self.tournament.results) < round:
            raise CommandFailed("Invalid round number")

    def _round_results(self):
        """Get the current round result or an empty dict."""
        index = self.tournament.current_round - 1
        if len(self.tournament.results) > index:
            return self.tournament.results[index]
        return {}

    def _player_display(self, vekn):
        """How to display a player."""
        name = self.tournament.registered.get(vekn)
        user_id = self.tournament.players.get(vekn)
        if user_id:
            member = self.guild.get_member(user_id)
        else:
            member = None
        return (
            ("**[D]** " if vekn in self.tournament.disqualified else "")
            + (f"{name} #{vekn} " if name else f"#{vekn} ")
            + (f"{member.mention}" if member else "")
        )

    def _get_player_number(self, vekn):
        """Retrieve a player number from their VEKN."""
        if not self.tournament.seating:
            raise CommandFailed("Tournament has not started")
        number = {v: k for k, v in self.tournament.player_numbers.items()}.get(vekn)
        if not number:
            raise CommandFailed("Player has not checked in")
        return number

    def _get_vekn(self, user_id):
        """Get a player VEKN from their Discord user ID."""
        self._check_tournament()
        vekn = {v: k for k, v in self.tournament.players.items()}.get(user_id)
        if vekn:
            return vekn
        member = self.guild.get_member(user_id)
        if not member:
            raise CommandFailed("User is not in server")
        else:
            raise CommandFailed(f"{member.mention} has not checked in")

    def _get_ranking(self, toss=False):
        """Get the overall ranking. `_compute_scores()` must have been called first."""
        ranking = []
        last = tournament.Score(math.nan, math.nan, math.nan)
        rank = 1
        for j, (vekn, score) in enumerate(
            sorted(
                self.scores.items(),
                key=lambda a: (
                    a[0] not in self.tournament.disqualified,
                    self.winner == a[0],
                    a[1],
                    -self.tournament.finals_seeding.index(a[0])
                    if a[0] in self.tournament.finals_seeding
                    else 0,
                    random.random() if toss else a[0],
                ),
                reverse=True,
            ),
            1,
        ):
            if vekn in self.tournament.disqualified and last is not None:
                last = None
                rank = j
            elif vekn not in self.tournament.disqualified and last != score:
                rank = 2 if self.winner and 2 < j < 6 else j
            ranking.append((rank, vekn, score))
            last = score
        return ranking

    def _compute_scores(self, raise_on_incorrect=True):
        """Compute the overall scores. Sets `self.scores` and `self.winner`."""
        for i in range(1, len(self.tournament.seating) + 1):
            round_result, _, incorrect = self.tournament._compute_round_result(i)
            if raise_on_incorrect and incorrect:
                raise CommandFailed(
                    f"Incorrect results for round {i} tables {incorrect}"
                )
            if raise_on_incorrect and not round_result:
                raise CommandFailed(f"No result for round {i}")
            for player, score in round_result.items():
                self.scores[player].gw += score.gw
                self.scores[player].vp += score.vp
                self.scores[player].tp += score.tp
        if self.tournament.finals_seeding:
            round_result, _, incorrect = self.tournament._compute_round_result()
            if raise_on_incorrect and incorrect:
                raise CommandFailed("Incorrect results for finals")
            if raise_on_incorrect and not round_result:
                raise CommandFailed("No result for finals")
            if round_result:
                self.winner = max(
                    round_result.items(),
                    key=lambda a: (a[1], -self.tournament.finals_seeding.index(a[0])),
                )[0]
                self.scores[self.winner].gw += 1
                for player, score in round_result.items():
                    self.scores[player].vp += score.vp

    async def _close_current_round(self):
        """Close current round or raise CommandFailed if results are incorrect."""
        if self.tournament.current_round:
            round_results, _, incorrect = self.tournament._compute_round_result()
            if not round_results:
                raise CommandFailed(
                    "No table has reported their result yet, "
                    "previous round cannot be closed. "
                    "Use `archon unseat` to recompute a new seating."
                )
            if incorrect:
                plural = len(incorrect) > 1
                raise CommandFailed(
                    f"Table{'s' if plural else ''} {', '.join(map(str, incorrect))} "
                    f"{'have' if plural else 'has'} incorrect results, "
                    "previous round cannot be closed."
                )
        await self._remove_tables()

    def _from_judge(self):
        """Returns true if request is from a judge."""
        return self.tournament and (
            self.guild.get_role(self.tournament.judge_role) in self.author.roles
        )

    def _get_mentioned_members(self):
        """Get the member(s) mentioned in the request."""
        return filter(
            bool,
            (self.guild.get_member(mention.id) for mention in self.message.mentions),
        )

    def _get_mentioned_player(self, vekn=None):
        """Get a single player mentioned by Discord or by VEKN ID."""
        mention = None
        if vekn not in self.tournament.players:
            vekn = None
        if len(self.message.mentions) > 1:
            raise CommandFailed("You must mention a single player.")
        if len(self.message.mentions) > 0:
            mention = self.message.mentions[0]
            vekn = self._get_vekn(mention.id)
        elif vekn not in self.tournament.players:
            vekn = None
        if not vekn:
            raise CommandFailed(
                "You must mention a player (Discord mention or ID number)."
            )
        return mention.id if mention else None, vekn

    def _get_table_role(self, table_number):
        for role in self.guild.roles:
            if role.name == f"{self.tournament.prefix}Table-{table_number}":
                return role
        raise CommandFailed(f"Role for Table {table_number} not found")

    def _vekn_to_number(self):
        """Returns the vekn -> number dict."""
        return {v: k for k, v in self.tournament.player_numbers.items()}

    async def _drop_player(self, vekn):
        """Drop a player."""
        if self.tournament.staggered:
            raise CommandFailed(
                "This is a staggered tournament, players cannot drop out."
            )
        self.tournament.dropped.add(vekn)
        if not self.tournament.seating:
            return
        if len(self.tournament.seating[-1]) in [8, 12]:
            await self.send(
                "The number of players does not allow a round anymore: "
                "additional players or drop outs required."
            )

    def _register_player(self, vekn, name):
        vekn = vekn.strip("-")
        if not vekn:
            vekn = f"TEMP_{len(self.tournament.registered) + 1}"
        self.tournament.registered[vekn] = name
        return vekn

    def _assign_player_numbers(self):
        round_players = self.round_players
        vekn_to_number = self._vekn_to_number()
        new_players = [vekn for vekn in round_players if vekn not in vekn_to_number]
        random.shuffle(new_players)
        for vekn, number in zip(
            new_players,
            range(
                len(self.tournament.player_numbers) + 1,
                len(self.tournament.player_numbers) + len(new_players) + 1,
            ),
        ):
            self.tournament.player_numbers[number] = vekn

    async def _remove_tables(self):
        """Remove bot-created tables."""
        await asyncio.gather(
            *(
                self.guild.get_channel(channel).delete()
                for key, channel in self.tournament.channels.items()
                if self.guild.get_channel(channel)
                and (key.startswith("table-") or key.startswith("finals-"))
            )
        )
        await asyncio.gather(
            *(
                role.delete()
                for role in self.guild.roles
                if role.name.startswith(f"{self.tournament.prefix}Table-")
            )
        )

    @property
    def reason(self):
        """Reason given for Discord logs on channel/role creations."""
        return f"{self.tournament.name} Tournament"

    @property
    def judge_role(self):
        """Get the Judge Role object."""
        return self.guild.get_role(self.tournament.judge_role)

    @property
    def spectator_role(self):
        """Get the Spectator Role object."""
        return self.guild.get_role(self.tournament.spectator_role)

    @property
    def round_players(self):
        """Get the set of players for this round."""
        return set(self.tournament.players.keys()) - self.tournament.dropped

    async def __call__(self, *args):
        """Command code: this one is the default command.

        Override in inheriting classes to implement other commands.
        """
        if not self.tournament:
            raise CommandFailed(
                "No tournament in progress. Use `archon open` to start one."
            )
        if self.tournament.finals_seeding:
            raise CommandFailed("Finals in progress")
        if not self.tournament.checkin:
            if self.tournament.registered:
                await self.send_embed(
                    discord.Embed(
                        title="Archon registration",
                        description=(
                            "**Discord registration is required to play in this "
                            "tournament**\n"
                            "Use `archon register [ID#] [Name]` to register for the "
                            "tournament with your VEKN ID#.\n"
                            "For example: `archon register 10000123 John Doe`."
                        ),
                    )
                )
            else:
                await self.send("Waiting for check-in to start")
            return
        if self.tournament.registered:
            await self.send_embed(
                discord.Embed(
                    title="Archon check-in",
                    description=(
                        "**Discord check-in is required to play in this tournament**\n"
                        "Use `archon checkin [ID#]` to check in the tournament "
                        "with your VEKN ID#.\n"
                        "For example: `archon checkin 10000123`"
                    ),
                )
            )
            return
        await self.send_embed(
            discord.Embed(
                title="Archon check-in",
                description=(
                    "**Discord check-in is required to play in this tournament**\n"
                    "Use `archon checkin` to check in the tournament."
                ),
            )
        )


class Help(Command):
    async def __call__(self, *args):
        embed = discord.Embed(title="Archon help", description="")
        try:
            self._check_judge_private()
            embed.description += """**Judge commands**
- `archon appoint [@user] (...[@user])`: appoint users as judges
- `archon spectator [@user] (...[@user])`: appoint users as spectators
- `archon register [ID#] [Name]`: register a user (Use `-` for auto ID)
- `archon checkin [ID#] [@user] ([name])`: check user in, register him (requires name)
- `archon players`: display the list of players
- `archon checkin-start`: open check-in
- `archon checkin-stop`: stop check-in
- `archon checkin-reset`: reset check-in
- `archon checkin-all`: check-in all registered players
- `archon staggered [rounds#]`: run a staggered tournament (6, 7, or 11 players)
- `archon rounds-limit [#rounds]: limit the number of rounds per player`
- `archon round-start`: seat the next round
- `archon round-reset`: rollback the round seating
- `archon round-finish`: stop reporting and close the current round
- `archon round-add [@player | ID#]`: add a player (on a 4 players table)
- `archon round-remove [@player | ID#]`: remove a player (from a 5 players table)
- `archon results`: check current round results
- `archon standings`: display current standings
- `archon finals`: start the finals
- `archon caution [@player | ID#] [Reason]`: issue a caution to a player
- `archon warn [@player | ID#] [Reason]`: issue a warning to a player
- `archon disqualify [@player | ID#] [Reason]`: disqualify a player
- `archon close`: close current tournament

**Judge private commands**
- `archon upload`: upload the list of registered players (attach CSV file)
- `archon players`: display the list of players and their current score
- `archon player [@player | ID#]`: display player information, cautions and warnings
- `archon registrations`: display the list of registrations
- `archon fix [@player | ID#] [VP#] {Round}`: fix a VP report (current round by default)
- `archon fix-table [Table] [ID#] (...[ID#])`: reassign table (list players in order)
- `archon validate [Round] [Table] [Reason]`: validate an odd VP situation
"""
        except CommandFailed:
            if self.tournament:
                embed.description += """**Player commands**
- `archon help`: display this help message
- `archon status`: current tournament status
- `archon register [ID#] [Name]`: register a VEKN ID# for the tournament
- `archon checkin [ID#]`: check in for the round (with VEKN ID# if required)
- `archon report [VP#]`: report your score for the round
- `archon drop`: drop from the tournament
"""
            else:
                embed.description += (
                    "`archon open [name]`: start a new tournament or league"
                )
            judge_channel = self.guild.get_channel(
                self.tournament.channels.get(self.tournament.JUDGES_TEXT)
            )
            if judge_channel and self._from_judge():
                embed.set_footer(
                    text=f'Use "archon help" in the {judge_channel.mention} channel '
                    "to list judges commands."
                )
        await self.send_embed(embed)


class Open(Command):
    UPDATE = True

    async def __call__(self, *args):
        if self.tournament:
            raise CommandFailed("Tournament already in progress")
        self.tournament.name = " ".join(args)
        prefixes = db.get_active_prefixes(self.connection, self.guild.id)
        if self.tournament.prefix in prefixes:
            raise CommandFailed(
                "A tournament with the same initials is already running"
            )
        judge_role, spectator_role = await asyncio.gather(
            self.guild.create_role(name=f"{self.tournament.prefix}Judge"),
            self.guild.create_role(name=f"{self.tournament.prefix}Spectator"),
        )
        self.tournament.judge_role = judge_role.id
        self.tournament.spectator_role = spectator_role.id
        db.create_tournament(
            self.connection,
            self.tournament.prefix,
            self.guild.id,
            self.category.id if self.category else None,
            self.tournament.to_json(),
        )
        results = await asyncio.gather(
            self.author.add_roles(judge_role, reason=self.reason),
            self.guild.me.add_roles(judge_role, reason=self.reason),
            self.guild.create_text_channel(
                name="Judges",
                category=self.category,
                overwrites={
                    self.guild.default_role: perm.NO_TEXT,
                    judge_role: perm.TEXT,
                },
            ),
            self.guild.create_voice_channel(
                name="Judges",
                category=self.category,
                overwrites={
                    self.guild.default_role: perm.NO_VOICE,
                    judge_role: perm.VOICE,
                },
            ),
        )
        self.tournament.channels[self.tournament.JUDGES_TEXT] = results[2].id
        self.tournament.channels[self.tournament.JUDGES_VOCAL] = results[3].id
        self.update()
        await self.send(
            "Tournament open. Use:\n"
            "- `archon appoint` to appoint judges,\n"
            "- `archon register` or `archon upload` to register players (optional),\n"
            "- `archon checkin-start` to open the check-in for the first round."
        )


class Appoint(Command):
    async def __call__(self, *args):
        self._check_judge()
        judge_role = self.judge_role
        await asyncio.gather(
            *(
                member.add_roles(judge_role, reason=self.reason)
                for member in self._get_mentioned_members()
            )
        )
        await self.send("Judge(s) appointed")


class Spectator(Command):
    async def __call__(self, *args):
        self._check_judge()
        spectator_role = self.guild.get_role(self.tournament.spectator_role)
        await asyncio.gather(
            *(
                member.add_roles(spectator_role, reason=self.reason)
                for member in self._get_mentioned_members()
            )
        )
        await self.send("Spectator(s) appointed")


class Register(Command):
    UPDATE = True

    async def __call__(self, vekn=None, *name_args):
        judge_role = self.judge_role
        judge = self._from_judge()
        # self._check_judge()
        vekn = vekn.strip("#").strip("-")
        name = " ".join(name_args)
        if vekn:
            await self._check_vekn(vekn)
        elif not judge:
            raise CommandFailed(
                f"Only a {judge_role.mention} " "can register a user with no VEKN ID"
            )
        vekn = self._register_player(vekn, name)
        self.update()
        await self.send(f"{name} registered with ID# {vekn}")

    async def _check_vekn(self, vekn):
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://www.vekn.net/api/vekn/login",
                data={"username": VEKN_LOGIN, "password": VEKN_PASSWORD},
            ) as response:
                result = await response.json()
                try:
                    token = result["data"]["auth"]
                except:  # noqa: E722
                    token = None
            if not token:
                logger.error("Authentication failed: %s", result)
                raise CommandFailed("Unable to authentify to VEKN")

            async with session.get(
                f"https://www.vekn.net/api/vekn/registry?filter={vekn}",
                headers={"Authorization": f"Bearer {token}"},
            ) as response:
                result = await response.json()
                result = result["data"]
                if isinstance(result, str):
                    logger.error("API error: %s", result)
                    raise CommandFailed("VEKN returned an error")
                result = result["players"]
                if len(result) > 1:
                    raise CommandFailed("Incomplete VEKN ID#")
                if len(result) < 1:
                    raise CommandFailed("VEKN ID# not found")
                result = result[0]
                if result["veknid"] != str(vekn):
                    raise CommandFailed("VEKN ID# not found")
            # TODO: Not checking names for now. Future versions might
            # vekn_name = result["firstname"] + " " + result["lastname"]


class Status(Command):
    async def __call__(self):
        self._check_tournament()
        message = f"**{self.tournament.name}**"
        if self.tournament.registered:
            message += f"\n{len(self.tournament.registered)} players registered"
        if self.tournament.players:
            message += f"\n{len(self.round_players)} players checked in"
        if self.tournament.current_round:
            if self.tournament.finals_seeding:
                if len(self.tournament.results) == self.tournament.current_round:
                    try:
                        self._compute_scores()
                        message += (
                            f"\n{self._player_display(self.winner)} is the winner!"
                        )
                    except CommandFailed:
                        message += "\nFinals in progress"
                else:
                    message += "\nFinals in progress"
            else:
                message += f"\nRound {self.tournament.current_round} in progress"
        await self.channel.send(message)


class Upload(Command):
    UPDATE = True

    async def __call__(self, *args):
        self._check_judge_private()
        data = await self.message.attachments[0].read()
        data = io.StringIO(data.decode("utf-8"))
        data.seek(0)
        try:
            data = [
                (i, line[0].strip("#"), line[1])
                for i, line in enumerate(csv.reader(data), 1)
            ]
        except IndexError:
            data.seek(0)
            data = [
                (i, line[0].strip("#"), line[1])
                for i, line in enumerate(csv.reader(data, delimiter=";"), 1)
            ]
        data = [(line, vekn, name) for line, vekn, name in data if vekn]
        issues = collections.defaultdict(list)
        for line, vekn, _ in data:
            issues[vekn].append(line)
        issues = {k: v for k, v in issues.items() if len(v) > 1}
        if issues:
            await self.send(
                "Some VEKN numbers are duplicated:\n"
                + "\n".join(f"{vekn}: lines {lines}" for vekn, lines in issues.items())
            )
            return
        results = {vekn: name for _line, vekn, name in data}
        self.tournament.registered = results
        self.update()
        await self.send(f"{len(self.tournament.registered)} players registered")


class RoundsLimit(Command):
    UPDATE = True

    async def __call__(self, rounds_limit: int):
        self._check_judge()
        rounds_limit = int(rounds_limit)
        self.tournament.rounds_limit = rounds_limit
        self.update()
        await self.send(
            f"Rounds limited to {rounds_limit} "
            f"round{'s' if rounds_limit > 1 else ''} per player"
        )


class Checkin(Command):
    UPDATE = True

    async def __call__(self, vekn=None, mention=None, *name_args):
        self._check_tournament()
        vekn = (vekn or "").strip("#")
        judge_role = self.judge_role
        judge = self._from_judge()
        if mention:
            if not judge:
                raise CommandFailed(
                    f"Unexpected name: only a {judge_role.mention} "
                    "can check in another user"
                )
            if len(self.message.mentions) > 1:
                raise CommandFailed("You must mention a single player")
            member = self.message.mentions[0] if self.message.mentions else None
        else:
            member = self.author
        if not judge and not self.tournament.checkin:
            raise CommandFailed(
                "Check-in is closed. Use `archon checkin-start` to open it"
            )
        id_to_vekn = {v: k for k, v in self.tournament.players.items()}
        if member and member.id in id_to_vekn:
            previous_vekn = id_to_vekn[member.id]
            del self.tournament.players[previous_vekn]
            vekn = vekn or previous_vekn
        if self.tournament.registered:
            if not vekn:
                raise CommandFailed(
                    "This tournament requires registration, "
                    "please provide your VEKN ID."
                )
            if vekn not in self.tournament.registered:
                if not judge:
                    raise CommandFailed(
                        "User not registered for that tournament.\n"
                        f"A {judge_role.mention} can fix this."
                    )
                if not name_args:
                    raise CommandFailed(
                        "User is not registered for that tournament.\n"
                        "Add the user's name to the command to register him."
                    )
                vekn = self._register_player(vekn, " ".join(name_args))
        if not vekn:
            vekn = len(self.tournament.players) + 1
        if (
            member
            and self.tournament.players.get(vekn, member.id) != member.id
            and vekn not in self.tournament.dropped
        ):
            other_member = self.guild.get_member(self.tournament.players[vekn])
            if other_member:
                if judge:
                    await self.send(
                        f"ID# was used by {other_member.mention},\n"
                        "they will need to check in again."
                    )
                else:
                    raise CommandFailed(
                        f"ID# already used by {other_member.mention},\n"
                        "they can `archon drop` so you can use this ID instead."
                    )
        if judge:
            self.tournament.disqualified.discard(vekn)
        if vekn in self.tournament.disqualified:
            raise CommandFailed("You've been disqualified, you cannot check in again.")
        self.tournament.dropped.discard(vekn)
        number = {v: k for k, v in self.tournament.player_numbers.items()}.get(vekn)
        rounds_played = sum(number in r for r in self.tournament.seating)
        if (
            not judge
            and self.tournament.rounds_limit
            and rounds_played >= int(self.tournament.rounds_limit)
        ):
            raise CommandFailed(
                f"You played {rounds_played} round{'s' if rounds_played > 1 else ''} "
                "already, you cannot check in for this round."
            )
        self.tournament.players[vekn] = member.id if member else None
        # late checkin
        if self.tournament.staggered:
            raise CommandFailed(
                "This is a staggered tournament, it cannot accept more players."
            )
        if not self.tournament.checkin:
            self._assign_player_numbers()
        self.update()
        name = self.tournament.registered.get(vekn, "")
        await self.send(
            f"{member.mention if member else 'player'} checked in as "
            f"{name}{' ' if name else ''}#{vekn}"
        )


class CheckinReset(Command):
    UPDATE = True

    async def __call__(self, vekn=None, mention=None):
        self._check_judge()
        for vekn in self.tournament.players.keys():
            await self._drop_player(vekn)
        self.tournament.checkin = False
        self.update()
        await self.send("Check-in reset")


class CheckinStart(Command):
    UPDATE = True

    async def __call__(self):
        self._check_judge()
        self.tournament.checkin = True
        self.update()
        await self.send("Check-in is open")


class CheckinStop(Command):
    UPDATE = True

    async def __call__(self):
        self._check_judge()
        self.tournament.checkin = False
        self.update()
        await self.send("Check-in is closed")


class CheckinAll(Command):
    UPDATE = True

    async def __call__(self):
        self._check_judge()
        if not self.tournament.registered:
            raise CommandFailed(
                "If you do not use checkin, "
                "you need to provide a registrations list by using `archon upload` "
                "or `archon register`."
            )
        self.tournament.players.update(
            {vekn: None for vekn in self.tournament.registered.keys()}
        )
        self.tournament.checkin = False
        self.update()
        await self.send("All registered players will play")


class Drop(Command):
    UPDATE = True

    async def __call__(self, *args):
        self._check_tournament()
        author = self.message.author
        vekn = self._get_vekn(author.id)
        await self._drop_player(vekn)
        self.update()
        await self.send(f"{author.mention} dropped out")


class Caution(Command):
    UPDATE = True

    async def __call__(self, *args):
        self._check_judge()
        _, vekn = self._get_mentioned_player(*args[:1])
        self.tournament.cautions.setdefault(vekn, [])
        if len(self.tournament.cautions[vekn]) > 0:
            await self.send(
                "Player has been cautioned before:\n"
                + "\n".join(
                    f"- R{round}: {caution}"
                    for round, caution in self.tournament.cautions[vekn]
                )
            )
        self.tournament.cautions[vekn].append(
            [self.tournament.current_round, " ".join(args[1:])]
        )
        self.update()
        await self.send("Player cautioned")


class Warn(Command):
    UPDATE = True

    async def __call__(self, *args):
        self._check_judge()
        _, vekn = self._get_mentioned_player(*args[:1])
        self.tournament.warnings.setdefault(vekn, [])
        if len(self.tournament.warnings[vekn]) > 0:
            await self.send(
                "Player has been warned before:\n"
                + "\n".join(
                    f"- R{round}: {warning}"
                    for round, warning in self.tournament.warnings[vekn]
                )
            )
        self.tournament.warnings[vekn].append(
            [self.tournament.current_round, " ".join(args[1:])]
        )
        self.update()
        await self.send("Player warned")


class Disqualify(Command):
    UPDATE = True

    async def __call__(self, *args):
        self._check_judge()
        _, vekn = self._get_mentioned_player(*args[:1])
        self.tournament.warnings.setdefault(vekn, [])
        self.tournament.warnings[vekn].append(
            [self.tournament.current_round, " ".join(args[1:])]
        )
        await self._drop_player(vekn)
        self.tournament.disqualified.add(vekn)
        self.update()
        await self.send("Player disqualifed")


class Player(Command):
    async def __call__(self, *args):
        self._check_judge_private()
        _user_id, vekn = self._get_mentioned_player(*args[:1])
        self._compute_scores(raise_on_incorrect=False)
        score = self.scores[vekn]
        embed = discord.Embed(title="Player Information", description="")
        embed.description = f"** {self._player_display(vekn)} **\n"
        if vekn in self.tournament.disqualified:
            embed.description += "Disqualified\n"
        elif vekn in self.tournament.dropped:
            embed.description += "Absent\n"
        embed.description += f"{score}\n"
        if vekn in self.tournament.cautions:
            cautions = self.tournament.cautions[vekn]
            embed.add_field(
                name="Cautions",
                value="\n".join(f"- R{r}: {c}" for r, c in cautions),
                inline=False,
            )
        if vekn in self.tournament.warnings:
            warnings = self.tournament.warnings[vekn]
            embed.add_field(
                name="Warnings",
                value="\n".join(f"- R{r}: {c}" for r, c in warnings),
                inline=False,
            )
        await self.send_embed(embed)


class Players(Command):
    async def __call__(self):
        self._check_judge()
        players = [
            vekn
            for vekn in sorted(self.tournament.players.keys())
            if vekn not in self.tournament.dropped
        ]
        await self.send_embed(
            discord.Embed(
                title=f"Players list ({len(players)})",
                description="\n".join(
                    f"- {self._player_display(vekn)}" for vekn in players
                ),
            )
        )


class Registrations(Command):
    async def __call__(self):
        self._check_judge_private()
        embed = discord.Embed(
            title=f"Registrations ({len(self.tournament.registered)})", description=""
        )
        for vekn in sorted(self.tournament.registered.keys()):
            s = f"- {self._player_display(vekn)}"
            embed.description += s + "\n"
        await self.send_embed(embed)


class _ProgressUpdate:
    def __init__(self, processes, message, embed):
        self.processes = processes
        self.message = message
        self.embed = embed
        self.progress = [0] * self.processes

    def __call__(self, i):
        async def progression(step, **kwargs):
            self.progress[i] = (step / (ITERATIONS * self.processes)) * 100
            progress = sum(self.progress)
            if not progress % 5 and progress < 100:
                progress = "▇" * int(progress // 5) + "▁" * (20 - int(progress // 5))
                self.embed.description = progress
                await self.message.edit(embed=self.embed)

        return progression


class RoundStart(Command):
    UPDATE = True

    async def __call__(self):
        self._check_judge()
        await self._close_current_round()
        self.tournament.current_round += 1
        self.tournament.reporting = True
        self._assign_player_numbers()
        if not self.tournament.staggered:
            self._init_seating()
        if self.tournament.current_round > 1:
            await self._optimise_seating()
        round = krcg.seating.Round(
            self.tournament.seating[self.tournament.current_round - 1]
        )
        table_roles = await asyncio.gather(
            *(
                self.guild.create_role(name=f"{self.tournament.prefix}Table-{i + 1}")
                for i in range(len(round))
            )
        )
        judge_role = self.judge_role
        spectator_role = self.spectator_role
        text_channels = await asyncio.gather(
            *(
                self.guild.create_text_channel(
                    name=f"Table {i + 1}",
                    category=self.category,
                    overwrites={
                        self.guild.default_role: perm.NO_TEXT,
                        spectator_role: perm.SPECTATE_TEXT,
                        table_roles[i]: perm.TEXT,
                        judge_role: perm.TEXT,
                    },
                )
                for i in range(len(round))
            )
        )
        self.tournament.channels.update(
            {
                f"table-{i}-text": channel.id
                for i, channel in enumerate(text_channels, 1)
            }
        )
        voice_channels = await asyncio.gather(
            *(
                self.guild.create_voice_channel(
                    name=f"Table {i + 1}",
                    category=self.category,
                    overwrites={
                        self.guild.default_role: perm.NO_VOICE,
                        spectator_role: perm.SPECTATE_VOICE,
                        table_roles[i]: perm.VOICE,
                        judge_role: perm.JUDGE_VOICE,
                    },
                )
                for i in range(len(round))
            )
        )
        self.tournament.channels.update(
            {
                f"table-{i}-voice": channel.id
                for i, channel in enumerate(voice_channels, 1)
            }
        )
        members = {
            n: self.guild.get_member(
                self.tournament.players[self.tournament.player_numbers[n]]
            )
            for table in round
            for n in table
        }
        await asyncio.gather(
            *(
                members[n].add_roles(table_roles[i], reason=self.reason)
                for i, table in enumerate(round)
                for n in table
                if members[n]
            )
        )
        n_to_vekn = self.tournament.player_numbers
        embed = discord.Embed(title=f"Round {self.tournament.current_round} seating")
        for i, table in enumerate(round, 1):
            embed.add_field(
                name=f"Table {i}",
                value="\n".join(
                    f"- {j}. {self._player_display(n_to_vekn[n])}"[:200]
                    for j, n in enumerate(table, 1)
                ),
                inline=False,
            )
        self.tournament.checkin = False
        self.update()
        messages = await self.send_embed(embed)
        await asyncio.gather(*(m.pin() for m in messages))
        await asyncio.gather(
            *(
                text_channels[i].send(
                    embed=discord.Embed(
                        title="Seating",
                        description="\n".join(
                            f"- {j}. {self._player_display(n_to_vekn[n])}"[:200]
                            for j, n in enumerate(table, 1)
                        ),
                    )
                )
                for i, table in enumerate(round)
            )
        )

    def _init_seating(self):
        round_players = self.round_players
        if len(round_players) in [6, 7, 11]:
            raise CommandFailed(
                "The number of players requires a staggered tournament. "
                "Add or remove players, or use the `archon staggered` command."
            )
        if not self.tournament.seating:
            self.tournament.seating = [list(range(1, len(round_players) + 1))]
        vekn_to_number = self._vekn_to_number()
        while self.tournament.current_round > len(self.tournament.seating):
            self.tournament.seating.append(
                [vekn_to_number[vekn] for vekn in round_players]
            )
            random.shuffle(self.tournament.seating[-1])
        while self.tournament.current_round > len(self.tournament.results):
            self.tournament.results.append({})

    async def _optimise_seating(self):
        embed = discord.Embed(
            title=f"Round {self.tournament.current_round} - computing seating",
            description="▁" * 20,
        )
        messages = await self.send_embed(embed)
        progression = _ProgressUpdate(4, messages[0], embed)
        results = await asyncio.gather(
            *(
                asgiref.sync.sync_to_async(krcg.seating.optimise)(
                    permutations=self.tournament.seating,
                    iterations=ITERATIONS,
                    callback=asgiref.sync.async_to_sync(progression(i)),
                    fixed=self.tournament.current_round - 1,
                    ignore=set(),
                )
                for i in range(4)
            )
        )
        rounds, score = min(results, key=lambda x: x[1].total)
        logging.info(
            "Seating – rounds: %s, score:%s=%s", rounds, score.rules, score.total
        )
        self.tournament.seating = [
            list(itertools.chain.from_iterable(r)) for r in rounds
        ]
        await messages[0].delete()


class Staggered(Command):
    UPDATE = True

    async def __call__(self, rounds):
        self._check_judge()
        if len(self.round_players) not in [6, 7, 11]:
            raise CommandFailed("Staggered tournaments are for 6, 7 or 11 players")
        if self.tournament.seating:
            raise CommandFailed(
                "Impossible: a tournament must be staggered from the start."
            )
        rounds = int(rounds)
        self.tournament.checkin = False
        self.tournament.staggered = True
        if rounds > 10:
            raise CommandFailed("Staggered tournaments must have less than 10 rounds")
        self.tournament.seating = krcg.seating.permutations(
            len(self.round_players), rounds
        )
        for i in range(1, len(self.tournament.seating)):
            random.shuffle(self.tournament.seating[i])
        self.update()
        await self.send(
            "Staggered tournament ready: "
            f"{len(self.tournament.seating)} rounds will be played, "
            f"each player will play {rounds} rounds out of those."
        )


class RoundFinish(Command):
    UPDATE = True

    async def __call__(self):
        self._check_judge
        await self._close_current_round()
        self.tournament.reporting = False
        await self.send(f"Round {self.tournament.current_round} finished")


class RoundReset(Command):
    UPDATE = True

    async def __call__(self):
        self._check_judge
        self._check_current_round_modifiable()
        await self._remove_tables()
        self.tournament.current_round -= 1
        if self.tournament.current_round <= 0:
            self.tournament.seating = []
            self.tournament.staggered = False
        self.tournament.finals_seeding = []
        self.update()
        await self.send("Seating cancelled")


class RoundAdd(Command):
    UPDATE = True

    async def __call__(self, *args):
        self._check_judge
        if self.tournament.staggered:
            raise CommandFailed("Staggered tournament rounds cannot be modified")
        user_id, vekn = self._get_mentioned_player(*args[:1])
        member = self.guild.get_member(user_id)
        if not member:
            raise CommandFailed("Player not in server")
        vekn_to_number = self._vekn_to_number()
        number = vekn_to_number.get(vekn)
        # this should not happen
        if not number:
            raise CommandFailed(
                "Player number not assigned - contact archon maintainer"
            )

        round_index = self.tournament.current_round - 1
        player_index = 0
        tables = self.tournament._get_round_tables()
        for table_index, table in enumerate(tables, 1):
            player_index += len(table)
            if len(table) > 4:
                continue
            self.tournament.seating[round_index].insert(player_index, number)
            break
        else:
            await self.send("No table available to sit this player in")
            return
        self.update()
        tables = self.tournament._get_round_tables()
        await member.add_roles(
            self._get_table_role(table_index),
            reason=self.reason,
        )
        await self.send(f"Player seated 5th on table {table_index}")
        table_channel = self.guild.get_channel(
            self.tournament.channels[f"table-{table_index}-text"]
        )
        await table_channel.send(
            embed=discord.Embed(
                title="New seating",
                description="\n".join(
                    f"- {j}. {self._player_display(vekn)}"[:200]
                    for j, vekn in enumerate(tables[table_index - 1], 1)
                ),
            )
        )


class RoundRemove(Command):
    UPDATE = True

    async def __call__(self, *args):
        raise CommandFailed("Not yet implemented.")
        # self._check_judge
        # if self.tournament.staggered:
        #     raise CommandFailed("Staggered tournament rounds cannot be modified")
        # user_id, vekn = self._get_mentioned_player(*args[:1])
        # member = self.guild.get_member(user_id)
        # if not member:
        #     raise CommandFailed("Player not in server")
        # if vekn not in self.round_players:
        #     raise CommandFailed("Player is not playing this round")
        # self.tournament.dropped.add(vekn)
        # vekn_to_number = self._vekn_to_number()
        # number = vekn_to_number.get(vekn)
        # # this should not happen
        # if not number:
        #     raise CommandFailed(
        #         "Player number bot assigned - contact archon maintainer"
        #     )
        # index = self.tournament.current_round - 1
        # for i, table in enumerate(self.tournament.seating[index], 1):
        #     if number in table:
        #         if len(table) < 5:
        #             raise CommandFailed(
        #                 f"Table {i} has only 4 players, "
        #                 "a 5th player is required before you can remove one."
        #             )
        #         table.remove(number)
        #         self.update()
        #         await member.remove_roles(self._get_table_role(i), reason=self.reason)
        #         table_channel = table_channel = self.guild.get_channel(
        #             self.tournament.channels[f"table-{i}-text"]
        #         )
        #         n_to_vekn = self.tournament.player_numbers
        #         await table_channel.send(
        #             embed=discord.Embed(
        #                 title="New seating",
        #                 description="\n".join(
        #                     f"- {j}. {self._player_display(n_to_vekn[n])}"[:200]
        #                     for j, n in enumerate(table, 1)
        #                 ),
        #             )
        #         )
        #         break
        # else:
        #     await self.send("Player not seated in this round")


class Finals(Command):
    UPDATE = True

    async def __call__(self, *args):
        self._check_judge()
        await self._close_current_round()
        self.tournament.current_round += 1
        self._compute_scores()
        table_role = await self.guild.create_role(
            name=f"{self.tournament.prefix}Finals"
        )
        top_5 = [
            (rank, vekn, score)
            for rank, vekn, score in self._get_ranking(toss=True)
            if vekn not in self.tournament.disqualified
        ][:5]
        self.tournament.finals_seeding = [vekn for _, vekn, _ in top_5]
        await asyncio.gather(
            *(
                member.add_roles(
                    table_role,
                    reason=self.reason,
                )
                for member in filter(
                    bool,
                    (
                        self.guild.get_member(self.tournament.players.get(vekn, 0))
                        for vekn in self.tournament.finals_seeding
                    ),
                )
            )
        )
        judge_role = self.judge_role
        spectator_role = self.spectator_role
        text_channel, voice_channel = await asyncio.gather(
            self.guild.create_text_channel(
                name="Finals",
                category=self.category,
                overwrites={
                    self.guild.default_role: perm.NO_TEXT,
                    spectator_role: perm.SPECTATE_TEXT,
                    judge_role: perm.TEXT,
                    table_role: perm.TEXT,
                },
            ),
            self.guild.create_voice_channel(
                name="Finals",
                category=self.category,
                overwrites={
                    self.guild.default_role: perm.NO_VOICE,
                    spectator_role: perm.SPECTATE_VOICE,
                    judge_role: perm.JUDGE_VOICE,
                    table_role: perm.VOICE,
                },
            ),
        )
        self.tournament.channels["finals-text"] = text_channel.id
        self.tournament.channels["finals-vocal"] = voice_channel.id
        self.update()
        messages = await self.send_embed(
            embed=discord.Embed(
                title="Finals",
                description="\n".join(
                    f"- {i}. {self._player_display(vekn)} " f"{score}"
                    for i, (_, vekn, score) in enumerate(top_5, 1)
                ),
            )
        )
        await messages[0].pin()
        return


class Standings(Command):
    async def __call__(self, *args):
        self._check_judge()
        embed = discord.Embed(title="Standings")
        self._compute_scores(raise_on_incorrect=False)
        ranking = self._get_ranking()
        results = []
        for rank, vekn, score in ranking:
            if vekn in self.tournament.disqualified:
                rank = ""
            elif self.winner and rank == 1:
                rank = "**WINNER** "
            else:
                rank = f"{rank}. "
            results.append(f"- {rank}{self._player_display(vekn)} " f"{score}")
        embed.description = "\n".join(results)
        await self.send_embed(embed)


class Results(Command):
    async def __call__(self, *args):
        self._check_judge()
        if not self.tournament.current_round:
            raise CommandFailed("No seating has been done yet.")
        if self.tournament.finals_seeding:
            embed = discord.Embed(title="Finals", description="")
            for i, vekn in enumerate(self.tournament.finals_seeding, 1):
                result = self.tournament.results[-1].get(vekn, 0)
                embed.description += f"{i}. {self._player_display(vekn)}: {result}VP\n"
            await self.send_embed(embed)
        else:
            embed = discord.Embed(title=f"Round {self.tournament.current_round}")
            result, tables, incorrect = self.tournament._compute_round_result()
            if not result:
                embed.description = "No table has reported their result yet."
                await self.send_embed(embed)
                return
            incorrect = set(incorrect)
            for i, table in enumerate(tables, 1):
                status = "OK"
                if sum(result[vekn].vp for vekn in table) == 0:
                    status = "NOT REPORTED"
                elif i in incorrect:
                    status = "INVALID"
                embed.add_field(
                    name=f"Table {i} {status}",
                    value="\n".join(
                        f"{i}. {self._player_display(vekn)} {result[vekn]}"
                        for i, vekn in enumerate(table, 1)
                    ),
                    inline=True,
                )
            await self.send_embed(embed)


class Report(Command):
    UPDATE = True

    async def __call__(self, vps):
        self._check_round()
        if not self.tournament.reporting:
            raise CommandFailed("No round in progress")
        vps = float(vps.replace(",", "."))
        vekn = self._get_vekn(self.message.author.id)
        index = self.tournament.current_round - 1
        if self.tournament.finals_seeding:
            if vekn not in self.tournament.finals_seeding:
                raise CommandFailed("You did not participate in the finals")
        elif vekn not in {
            self.tournament.player_numbers[n] for n in self.tournament.seating[index]
        }:
            raise CommandFailed("You did not participate in this round")
        if vps > 5:
            raise CommandFailed("That seems like too many VPs")
        if vps <= 0:
            self.tournament.results[index].pop(vekn, None)
        else:
            self.tournament.results[index][vekn] = vps
        self.update()
        await self.send("Result registered")


class Fix(Command):
    UPDATE = True

    async def __call__(self, vekn, vps, round=None):
        self._check_judge()
        _, vekn = self._get_mentioned_player(vekn)
        vps = float(vps.replace(",", "."))
        round = self.tournament.current_round if round is None else int(round)
        self._check_round(round)
        results = self.tournament.results[round - 1]
        if vps <= 0:
            results.pop(vekn, None)
        else:
            results[vekn] = vps
        self.update()
        await self.send("Fixed")


class FixTable(Command):
    UPDATE = True

    async def __call__(self, table, *vekns):
        raise CommandFailed("Not yet implemented.")
        # self._check_judge()
        # round = self.tournament.current_round
        # seating = self.tournament.seating[round - 1]
        # index = table - 1
        # if index > len(seating):
        #     raise CommandFailed("Invalid table number")
        # if index == len(seating):
        #     seating.append([])
        # if len(vekns) < 4 or len(vekns) > 5:
        #     raise CommandFailed("Invalid players count: needs to 4 or 5")
        # already_seated = set(
        #     self.tournament.player_numbers[i] for t in seating for i in t
        # ) & set(vekns)
        # if already_seated:
        #     raise CommandFailed(
        #         f"{already_seated} {'are' if len(already_seated) > 1 else 'is'} "
        #         "already seated elsewhere"
        #     )
        # vekn_to_number = self._vekn_to_number()
        # seating[index] = [vekn_to_number[vekn] for vekn in vekns]
        # self.update()
        # # TODO: Add/Remove roles, repost seating.
        # await self.send(f"Table {table} fixed")


class Validate(Command):
    UPDATE = True

    async def __call__(self, round, table, *args):
        self._check_judge()
        round = int(round)
        table = int(table)
        reason = " ".join(args)
        self._check_round(round)
        self.tournament.overrides[f"{round}-{table}"] = reason
        self.update()
        await self.send("Validated")


class Close(Command):
    UPDATE = True

    async def __call__(self, force=None):
        self._check_judge()
        force = force == "force"
        if self.channel.id in self.tournament.channels.values():
            raise CommandFailed(
                "The `close` command must be issued outside of tournament channels"
            )
        self._compute_scores(raise_on_incorrect=not force)
        if not (force or self.tournament.finals_seeding):
            raise CommandFailed(
                "Tournament is not finished. "
                "Use `archon close force` to close it nonetheless."
            )
        reports = [self._build_results_csv()]
        if self.tournament.registered and self.tournament.results:
            reports.append(self._build_methuselahs_csv())
            reports.extend(f for f in self._build_rounds_csvs())
            if (
                self.tournament.finals_seeding
                and len(self.tournament.results) >= self.tournament.current_round
            ):
                reports.append(self._build_finals_csv())
        await self.channel.send("Reports", files=reports)
        await asyncio.gather(
            *(
                self.guild.get_channel(channel).delete()
                for channel in self.tournament.channels.values()
                if self.guild.get_channel(channel)
            )
        )
        await asyncio.gather(
            *(
                role.delete()
                for role in self.guild.roles
                if role.name.startswith(self.tournament.prefix)
            )
        )
        db.close_tournament(
            self.connection, self.guild.id, self.category.id if self.category else None
        )
        logger.info("closed tournament %s in %s", self.tournament.name, self.guild.name)
        await self.send("Tournament closed")

    def _build_results_csv(self):
        data = []
        for rank, vekn, score in self._get_ranking():
            if vekn in self.tournament.disqualified:
                rank = "DQ"
            number = self._get_player_number(vekn)
            finals_position = ""
            if vekn in self.tournament.finals_seeding:
                finals_position = self.tournament.finals_seeding.index(vekn) + 1
            data.append(
                [
                    number,
                    vekn,
                    self.tournament.registered.get(vekn, ""),
                    (
                        sum(1 for s in self.tournament.seating if number in s)
                        + (1 if vekn in self.tournament.finals_seeding else 0)
                    ),
                    score.gw,
                    score.vp,
                    finals_position,
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

    def _build_methuselahs_csv(self):
        data = []
        for number, vekn in sorted(self.tournament.player_numbers.items()):
            if vekn not in self.tournament.players:
                continue
            name = self.tournament.registered.get(vekn, "UNKNOWN").split(" ", 1)
            if len(name) < 2:
                name.append("")
            data.append(
                [
                    number,
                    name[0],
                    name[1],
                    "",  # country
                    vekn,
                    (
                        sum(1 for s in self.tournament.seating if number in s)
                        + (1 if vekn in self.tournament.finals_seeding else 0)
                    ),
                    "DQ" if vekn in self.tournament.disqualified else "",
                ]
            )
        return self._build_csv("Methuselahs.csv", data)

    def _build_rounds_csvs(self):
        for i, permutation in enumerate(self.tournament.seating, 1):
            if len(self.tournament.results) < i:
                break
            data = []
            for j, table in enumerate(krcg.seating.Round(permutation), 1):
                for number in table:
                    vekn = self.tournament.player_numbers[number]
                    first_name, last_name = self._get_first_last_name(vekn)
                    data.append(
                        [
                            number,
                            first_name,
                            last_name,
                            j,
                            self.tournament.results[i - 1].get(vekn, 0),
                        ]
                    )
                if len(table) < 5:
                    data.append(["", "", "", "", ""])
            yield self._build_csv(f"Round {i}.csv", data)

    def _build_finals_csv(self):
        data = []
        vekn_to_number = self._vekn_to_number()
        for i, vekn in enumerate(self.tournament.finals_seeding, 1):
            number = vekn_to_number[vekn]
            first_name, last_name = self._get_first_last_name(vekn)
            data.append(
                [
                    number,
                    first_name,
                    last_name,
                    1,  # table
                    i,  # seat
                    self.tournament.results[-1].get(vekn, 0),
                ]
            )
        return self._build_csv("Finals.csv", data)

    def _build_csv(self, filename, it, columns=None):
        data = io.StringIO()
        writer = csv.writer(data)
        if columns:
            writer.writerow(columns)
        writer.writerows(it)
        data = io.BytesIO(data.getvalue().encode("utf-8"))
        return discord.File(data, filename=filename)

    def _get_first_last_name(self, vekn):
        name = self.tournament.registered.get(vekn, "UNKNOWN").split(" ", 1)
        if len(name) < 2:
            name.append("")
        return name[0], name[1]
