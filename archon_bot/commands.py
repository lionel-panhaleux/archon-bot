import asyncio
import collections
import csv
import io
import itertools
import math
import logging
import random

import asgiref.sync
import discord

import krcg.seating
import krcg.utils


from .db import CONNECTION
from .tournament import Tournament
from . import permissions as perm

logger = logging.getLogger()

#: Iterations for the seating algorithm (higher is best but takes longer)
ITERATIONS = 20000


class CommandFailed(Exception):
    """A "normal" failure: an answer explains why the command was not performed"""


class Command:
    """Bot Commands implementations."""

    def __init__(self, connection, message, data=None):
        self.connection = connection
        self.message = message
        self.author = self.message.author
        self.channel = self.message.channel
        self.guild = self.channel.guild
        self.category = self.channel.category
        self.tournament = Tournament(**(data or {}))

    def update(self):
        data = self.tournament.to_json()
        logger.debug("update %s: %s", self.guild.name, data)
        cursor = self.connection.cursor()
        cursor.execute(
            "UPDATE tournament SET data=? WHERE active=1 AND guild=?",
            [
                data,
                str(self.guild.id),
            ],
        )

    async def send(self, message):
        rest = ""
        while message:
            message, rest = self._split_text(message, 2000)
            await self.channel.send(message, reference=self.message)
            message = rest
            rest = ""

    async def send_embed(self, embed):
        """Paginate the embed if necessary"""
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

    async def _check_tournament(self):
        if self.tournament:
            return
        await self.send("No tournament in progress")
        raise CommandFailed("Tournament required")

    async def _check_judge(self, message=None):
        await self._check_tournament()
        judge_role = self.guild.get_role(self.tournament.judge_role)
        if judge_role in self.author.roles:
            return
        await self.send(
            message or f"Only a {judge_role.mention} can issue this command"
        )
        raise CommandFailed("Judge only")

    async def _check_judge_private(self):
        await self._check_judge()
        judge_channel = self.guild.get_channel(
            self.tournament.channels[self.tournament.JUDGES_TEXT]
        )
        if self.channel.id == judge_channel.id:
            return
        await self.send(
            f"This command can only be issued in the {judge_channel.mention} channel"
        )
        raise CommandFailed("Only in the private judge channel")

    def _player_display(self, vekn):
        name = self.tournament.registered.get(vekn)
        user_id = self.tournament.players.get(vekn)
        if user_id:
            member = self.guild.get_member(user_id)
        else:
            member = None
        return (
            f"{name} #{vekn} " if name else f"#{vekn} "
        ) + f"{member.mention if member else ''}"

    def _score_display(self, score):
        return f"({score[0]}GW{score[1]}, {score[2]}TP)"

    async def help(self, *args):
        """Help message (bot manual)"""
        embed = discord.Embed(title="Archon help", description="")
        if self.tournament:
            embed.description += """**Player commands**
- `archon help`: Display this help message
- `archon status`: current tournament status
- `archon checkin [ID#]`: check in for tournament (with VEKN ID# if required)
- `archon report [VP#]`: Report your score for the round
- `archon drop`: Drop from the tournament
"""
        else:
            embed.description += (
                "`archon open [Rounds#] [tournament name]`: start a new tournament"
            )
        if self.guild.get_role(self.tournament.judge_role) in self.author.roles:
            embed.description += """
**Judge commands**
- `archon appoint [@user]`: appoint user as judge
- `archon spectator [@user]`: appoint user as spectator
- `archon register [ID#] [Full Name]`: register a user (Use `-` for auto ID)
- `archon checkin [ID#] [@user]`: check user in (even if disqualified)
- `archon uncheck`: reset check-in
- `archon allcheck`: check in all registered players
- `archon players`: display the list of players
- `archon seat`: Seat the next round
- `archon add [@player | ID#]`: Add a player to the round (on a 4 players table)
- `archon unseat`: Rollback the round seating
- `archon results`: check current round results
- `archon standings`: Display current standings
- `archon warn [@player | ID#] [Reason]`: Issue a warning to a player
- `archon disqualify [@player | ID#] [Reason]`: Disqualify a player
- `archon close`: Close current tournament

**Judge private commands**
- `archon upload`: upload the list of registered players (attach CSV file)
- `archon players`: display the list of players and their current score
- `archon registrations`: display the list of registrations
- `archon fix [Round] [ID#] [VP#]`: fix a VP report
- `archon validate [Round] [Table] [Reason]`: validate an odd VP situation
"""
        await self.send_embed(embed)

    async def default(self, *args):
        if not self.tournament:
            await self.send("No tournament in progress. `archon open` to start one.")
            return
        if self.tournament.registered and not self.tournament.finals_seeding:
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
        if not self.tournament.finals_seeding:
            await self.send_embed(
                discord.Embed(
                    title="Archon check-in",
                    description=(
                        "**Discord check-in is required to play in this tournament**\n"
                        "Use `archon checkin` to check in the tournament."
                    ),
                )
            )
            return

    async def open(self, rounds, *args):
        if self.tournament:
            await self.send("Tournament already in progress")
            return
        self.tournament.name = " ".join(args)
        self.tournament.rounds = int(rounds)
        judge_role = await self.guild.create_role(name=f"{self.tournament.prefix}Judge")
        spectator_role = await self.guild.create_role(
            name=f"{self.tournament.prefix}Spectator"
        )
        self.tournament.judge_role = judge_role.id
        self.tournament.spectator_role = spectator_role.id
        connection = await CONNECTION.get()
        cursor = connection.cursor()
        cursor.execute(
            "INSERT INTO tournament (active, guild, data) VALUES (1, ?, ?)",
            [str(self.guild.id), self.tournament.to_json()],
        )
        connection.commit()
        CONNECTION.put_nowait(connection)
        await self.author.add_roles(
            judge_role, reason=f"{self.tournament.name} Tournament opened"
        )
        await self.guild.me.add_roles(
            judge_role, reason=f"{self.tournament.name} Tournament opened"
        )
        channel = await self.guild.create_text_channel(
            name="Judges",
            category=self.category,
            overwrites={
                self.guild.default_role: perm.NO_TEXT,
                judge_role: perm.TEXT,
            },
        )
        self.tournament.channels[self.tournament.JUDGES_TEXT] = channel.id
        channel = await self.guild.create_voice_channel(
            name="Judges",
            category=self.category,
            overwrites={
                self.guild.default_role: perm.NO_VOICE,
                judge_role: perm.VOICE,
            },
        )
        self.tournament.channels[self.tournament.JUDGES_VOCAL] = channel.id
        self.update()
        await self.send("Tournament open")

    async def appoint(self, *args):
        await self._check_judge()
        judge_role = self.guild.get_role(self.tournament.judge_role)
        for mention in self.message.mentions:
            member = self.guild.get_member(mention.id)
            if not member:
                continue
            await member.add_roles(
                judge_role, reason=f"{self.tournament.name} Tournament appointment"
            )
        await self.send("Judge(s) appointed")

    async def spectator(self, *args):
        await self._check_judge()
        spectator_role = self.guild.get_role(self.tournament.spectator_role)
        for mention in self.message.mentions:
            member = self.guild.get_member(mention.id)
            if not member:
                continue
            await member.add_roles(
                spectator_role, reason=f"{self.tournament.name} Tournament appointment"
            )
        await self.send("Spectator(s) appointed")

    async def register(self, vekn, *args):
        await self._check_judge()
        vekn = vekn.strip("-").strip("#")
        name = " ".join(args)
        if not vekn:
            vekn = f"TEMP_{len(self.tournament.registered) + 1}"
        self.tournament.registered[vekn] = name
        self.update()
        await self.send(f"{name} registered with ID# {vekn}")

    async def status(self):
        await self._check_tournament()
        message = f"**{self.tournament.name}** ({self.tournament.rounds}R+F)"
        if self.tournament.registered:
            message += f"\n{len(self.tournament.registered)} players registered"
        if self.tournament.players:
            count = len(self.tournament.players) - len(self.tournament.dropped)
            message += f"\n{count} players checked in"
        if self.tournament.current_round:
            if self.tournament.finals:
                if len(self.tournament.results) == self.tournament.current_round:
                    _score, winner = await self._get_total_scores(False)
                    message += f"\n{self._player_display(winner)} is the winner!"
                else:
                    message += "\nFinals in progress"
            else:
                message += f"\nRound {self.tournament.current_round} in progress"
        await self.channel.send(message)

    async def upload(self, *args):
        await self._check_judge_private()
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

    async def _fetch_official_vekn(self, session, token, vekn):
        async with session.get(
            f"https://www.vekn.net/api/vekn/registry?filter={vekn}",
            headers={"Authorization": f"Bearer {token}"},
        ) as response:
            result = await response.json()
            logger.info("Received: %s", result)
            result = result["data"]
            if isinstance(result, str):
                return False, f"VEKN returned an error: {result}"
            result = result["players"]
            if len(result) > 1:
                return False, "Incomplete VEKN ID#"
            if len(result) < 1:
                return False, "VEKN ID# not found"
            result = result[0]
            if result["veknid"] != vekn:
                return False, "VEKN ID# not found"
            return True, result["firstname"] + " " + result["lastname"]

    async def allcheck(self):
        await self._check_judge()
        if not self.tournament.registered:
            await self.send(
                "If you do not use checkin, "
                "you need to provide a registrations list by using `archon upload` "
                "or `archon register`."
            )
            return
        self.tournament.players.update(
            {vekn: None for vekn in self.tournament.registered.keys()}
        )
        self.update()
        await self.send("All registered players will play.")

    async def uncheck(self):
        await self._check_judge()
        for vekn in self.tournament.players.keys():
            self._drop_player(vekn)
        self.update()
        await self.send("Check in reset.")

    async def checkin(self, vekn=None, mention=None):
        await self._check_tournament()
        vekn = vekn or ""
        vekn = vekn.strip("#")
        judge_role = self.guild.get_role(self.tournament.judge_role)
        if mention:
            await self._check_judge(
                f"Only a {judge_role.mention} can check in another user"
            )
            if len(self.message.mentions) > 1:
                await self.send("You must mention a single player.")
                return
            judge = True
            member = self.message.mentions[0] if self.message.mentions else None
        else:
            judge = False
            member = self.author
        id_to_vekn = {v: k for k, v in self.tournament.players.items()}
        if member and member.id in id_to_vekn:
            previous_vekn = id_to_vekn[self.author.id]
            del self.tournament.players[previous_vekn]
            vekn = vekn or previous_vekn
        if self.tournament.registered:
            if not vekn:
                await self.send(
                    "This tournament requires registration, "
                    "please provide your VEKN ID."
                )
                return
            if vekn not in self.tournament.registered:
                await self.send(
                    "User not registered for that tournament.\n"
                    f"A {judge_role.mention} can use `archon register` to fix this."
                )
                return
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
                    await self.send(
                        f"ID# already used by {other_member.mention},\n"
                        "they can `archon drop` so you can use this ID instead."
                    )
                    return
        if judge:
            self.tournament.disqualified.discard(vekn)
        if vekn in self.tournament.disqualified:
            await self.send("You've been disqualified, you cannot check in again")
            return
        self.tournament.dropped.discard(vekn)
        self.tournament.players[vekn] = member.id if member else None
        # late checkin
        if self.tournament.player_numbers:
            vekn_to_number = {v: k for k, v in self.tournament.player_numbers.items()}
            number = vekn_to_number.get(vekn)
            if not number:
                number = len(self.tournament.player_numbers) + 1
                self.tournament.player_numbers[number] = vekn
            for i, permutation in enumerate(self.tournament.seating):
                if i < self.tournament.current_round:
                    continue
                if number not in permutation:
                    permutation.append(number)
        self.update()
        name = self.tournament.registered.get(vekn, "")
        await self.send(
            f"{member.mention if member else 'player'} checked in as "
            f"{name}{' ' if name else ''}#{vekn}"
        )

    def _drop_player(self, vekn):
        self.tournament.dropped.add(vekn)
        if not self.tournament.player_numbers:
            return
        number = {v: k for k, v in self.tournament.player_numbers.items()}.get(vekn)
        for i, permutation in enumerate(self.tournament.seating):
            if i < self.tournament.current_round:
                continue
            if number in permutation:
                permutation.remove(number)

    async def drop(self, *args):
        await self._check_tournament()
        author = self.message.author
        vekn = await self._get_vekn(author.id)
        self._drop_player(vekn)
        self.update()
        await self.send(f"{author.mention} dropped out")

    async def _get_mentioned_player(self, vekn=None):
        mention = None
        if vekn not in self.tournament.players:
            vekn = None
        if len(self.message.mentions) > 1:
            await self.send("You must mention a single player.")
            raise CommandFailed("Single mention required")
        if len(self.message.mentions) > 0:
            mention = self.message.mentions[0]
            vekn = await self._get_vekn(mention.id)
        elif vekn not in self.tournament.players:
            vekn = None
        if not vekn:
            await self.send("You must mention a player (Discord mention or ID number).")
            raise CommandFailed("Player required")
        return mention.id if mention else None, vekn

    async def caution(self, *args):
        await self._check_judge()
        _, vekn = await self._get_mentioned_player(*args[:1])
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

    async def warn(self, *args):
        await self._check_judge()
        _, vekn = await self._get_mentioned_player(*args[:1])
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

    async def disqualify(self, *args):
        await self._check_judge()
        _, vekn = await self._get_mentioned_player(*args[:1])
        self.tournament.warnings.setdefault(vekn, [])
        self.tournament.warnings[vekn].append(
            [self.tournament.current_round, " ".join(args[1:])]
        )
        self._drop_player(vekn)
        self.tournament.disqualified.add(vekn)
        self.update()
        await self.send("Player disqualifed")

    async def _get_player_number(self, vekn):
        number = {v: k for k, v in self.tournament.player_numbers.items()}.get(vekn)
        if not number:
            await self.send("Player has not checked in or tournament has not started")
            raise CommandFailed("Player not in rounds")
        return number

    async def _get_vekn(self, user_id):
        await self._check_tournament()
        vekn = {v: k for k, v in self.tournament.players.items()}.get(user_id)
        if vekn:
            return vekn
        member = self.guild.get_member(user_id)
        if not member:
            await self.send("User is not in server")
        else:
            await self.send(f"{member.mention} has not checked in")
        raise CommandFailed("Player not checked in")

    async def player(self, *args):
        await self._check_judge_private()
        _user_id, vekn = await self._get_mentioned_player(*args[:1])
        score, _winner = await self._get_total_scores(raise_on_incorrect=False)
        score = score[vekn]
        embed = discord.Embed(title="Player Information", description="")
        embed.description = f"**{self._player_display(vekn)}**\n"
        if vekn in self.tournament.dropped:
            if vekn in self.tournament.warnings:
                embed.description += "Disqualified\n"
            else:
                embed.description += "Dropped\n"
        embed.description += f"{self._score_display(score)}\n"
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

    async def players(self, *args):
        await self._check_judge()
        judge_channel = self.guild.get_channel(
            self.tournament.channels[self.tournament.JUDGES_TEXT]
        )
        private = True if self.channel.id == judge_channel.id else False

        scores, _winner = await self._get_total_scores(raise_on_incorrect=False)
        embed = discord.Embed(title="Players list", description="")
        for vekn in self.tournament.players.keys():
            s = f"- {self._player_display(vekn)}"
            if private:
                s += f" {self._score_display(scores[vekn])}"
                if vekn in self.tournament.dropped:
                    s += " **[D]**"  # dropped or disqualified
                elif vekn in self.tournament.warnings:
                    s += " **[W]**"
            s += "\n"
            embed.description += s
        await self.send_embed(embed)

    async def registrations(self, *args):
        await self._check_judge_private()
        embed = discord.Embed(title="Registrations", description="")
        for vekn in sorted(self.tournament.registered.keys()):
            s = f"- {self._player_display(vekn)}"
            if vekn in self.tournament.dropped:
                s += " **[D]**"  # dropped or disqualified
            if vekn in self.tournament.warnings:
                if vekn not in self.tournament.dropped:
                    s += " **[W]**"
            embed.description += s + "\n"
        await self.send_embed(embed)

    async def _check_current_round_modifiable(self):
        await self._check_tournament()
        if not self.tournament.current_round:
            await self.send("No seating has been done yet.")
            raise CommandFailed("Seating required")
        index = self.tournament.current_round - 1
        if len(self.tournament.results) > index and self.tournament.results[index]:
            await self.send(
                "Some tables have reported their result, unable to modify seating."
            )
            raise CommandFailed("Results reported")

    async def _get_total_scores(self, raise_on_incorrect=True):
        winner = None
        scores = collections.defaultdict(lambda: [0, 0, 0, 0])
        for i in range(len(self.tournament.seating)):
            round_result, _, incorrect = self.tournament._compute_round_result(i)
            if raise_on_incorrect and incorrect:
                await self.send(
                    f"Incorrect results for round {i + 1} tables {incorrect}"
                )
                raise CommandFailed("Incorrect score")
            for player, score in round_result.items():
                scores[player][0] += score[0]
                scores[player][1] += score[1]
                scores[player][2] += score[2]
                scores[player][3] += random.random()
        if self.tournament.finals and self.tournament.finals_seeding:
            round_result, _, _ = self.tournament._compute_round_result(
                self.tournament.current_round - 1
            )
            winner = max(
                round_result.items(),
                key=lambda a: (a[1], -self.tournament.finals_seeding.index(a[0])),
            )[0]
            scores[winner][0] += 1
            for player, score in round_result.items():
                scores[player][1] += score
        return scores, winner

    async def seat(self):
        await self._check_judge()
        if self.tournament.current_round:
            index = self.tournament.current_round - 1
            if len(self.tournament.results) <= index:
                await self.send(
                    "No table has reported their result yet, "
                    "previous round cannot be closed. "
                    "Use `archon unseat` to recompute a new seating."
                )
                return
            round_result, _, incorrect = self.tournament._compute_round_result(index)
            if incorrect:
                await self.send(
                    f"Tables {', '.join(map(str, incorrect))} "
                    "have incorrect results, "
                    "previous round cannot be closed."
                )
                return
        await self._remove_tables()
        if not self.tournament.seating:
            self.tournament.seating = krcg.seating.permutations(
                len(self.tournament.players) - len(self.tournament.dropped),
                self.tournament.rounds,
            )
            for p in self.tournament.seating[1:]:
                random.shuffle(p)
        permutations = self.tournament.seating
        self.tournament.current_round += 1
        judge_role = self.guild.get_role(self.tournament.judge_role)
        spectator_role = self.guild.get_role(self.tournament.spectator_role)
        # finals
        if self.tournament.finals:
            scores, _ = await self._get_total_scores()
            table_role = await self.guild.create_role(
                name=f"{self.tournament.prefix}Finals"
            )
            await self.guild.me.add_roles(
                table_role, reason=f"{self.tournament.name} Tournament seating"
            )
            results = []
            finals = []
            last = [math.nan] * 3
            place = 1
            cut = 6
            for j, (vekn, score) in enumerate(
                sorted(
                    scores.items(),
                    key=lambda a: (a[0] not in self.tournament.dropped, a[1]),
                    reverse=True,
                ),
                1,
            ):
                member = self.guild.get_member(self.tournament.players.get(vekn, 0))
                if vekn in self.tournament.dropped:
                    place = "**[D]**"
                elif last != score[:3]:
                    place = j
                last = score[:3]
                results.append(
                    f"- {place}. {self._player_display(vekn)} "
                    f"{self._score_display(score)}"
                )
                if j < cut and vekn not in self.tournament.dropped:
                    finals.append(
                        f"- {len(finals) + 1} {self._player_display(vekn)} "
                        f"{self._score_display(score)}"
                    )
                    self.tournament.finals_seeding.append(vekn)
                    if member:
                        await member.add_roles(
                            table_role,
                            reason=f"{self.tournament.name} Tournament seating",
                        )
            channel = await self.guild.create_text_channel(
                name="Finals",
                category=self.category,
                overwrites={
                    self.guild.default_role: perm.SPECTATE_TEXT,
                    judge_role: perm.TEXT,
                    table_role: perm.TEXT,
                },
            )
            self.tournament.channels["finals-text"] = channel.id
            channel = await self.guild.create_voice_channel(
                name="Finals",
                category=self.category,
                overwrites={
                    self.guild.default_role: perm.NO_VOICE,
                    spectator_role: perm.SPECTATE_VOICE,
                    judge_role: perm.JUDGE_VOICE,
                    table_role: perm.VOICE,
                },
            )
            self.tournament.channels["finals-vocal"] = channel.id
            self.update()
            messages = await self.send_embed(
                embed=discord.Embed(
                    title="Rounds results", description="\n".join(results)
                )
            )
            messages = await self.send_embed(
                embed=discord.Embed(title="Finals", description="\n".join(finals))
            )
            await messages[0].pin()
            return
        if self.tournament.current_round > len(permutations) + 1:
            await self.send(
                "All rounds have been played, "
                "use `archon close` to finish the tournament "
                "or `archon unseat` to cancel last round seating arrangement."
            )
            return
        # normal round
        if self.tournament.current_round > 1:
            embed = discord.Embed(
                title=f"Round {self.tournament.current_round} - computing seating",
                description="▁" * 20,
            )
            messages = await self.send_embed(embed)
            progression = ProgressUpdate(4, messages[0], embed)
            results = await asyncio.gather(
                *(
                    asgiref.sync.sync_to_async(krcg.seating.optimise)(
                        permutations=permutations,
                        iterations=ITERATIONS,
                        callback=asgiref.sync.async_to_sync(progression(i)),
                        fixed=max(1, self.tournament.current_round - 1),
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
        round = krcg.seating.Round(
            self.tournament.seating[self.tournament.current_round - 1]
        )
        embed = discord.Embed(title=f"Round {self.tournament.current_round} seating")
        if not self.tournament.player_numbers:
            players = list(
                set(self.tournament.players.keys()) - self.tournament.dropped
            )
            random.shuffle(players)
            self.tournament.player_numbers = {i: v for i, v in enumerate(players, 1)}
            # add numbers for players who dropped in case they re-checkin later
            for vekn in self.tournament.dropped:
                number = len(self.tournament.player_numbers) + 1
                self.tournament.player_numbers[number] = vekn
        for i, table in enumerate(round, 1):
            players = []
            table_role = await self.guild.create_role(
                name=f"{self.tournament.prefix}Table-{i}"
            )
            await self.guild.me.add_roles(
                table_role, reason=f"{self.tournament.name} Tournament seating"
            )
            for j, n in enumerate(table, 1):
                vekn = self.tournament.player_numbers[n]
                user_id = self.tournament.players[vekn]
                member = self.guild.get_member(user_id)
                players.append(f"- {j}. {self._player_display(vekn)}"[:200])
                if member:
                    await member.add_roles(
                        table_role, reason=f"{self.tournament.name} Tournament seating"
                    )
            embed.add_field(name=f"Table {i}", value="\n".join(players), inline=False)
            channel = await self.guild.create_text_channel(
                name=f"Table {i}",
                category=self.category,
                overwrites={
                    self.guild.default_role: perm.NO_TEXT,
                    spectator_role: perm.SPECTATE_TEXT,
                    table_role: perm.TEXT,
                    judge_role: perm.TEXT,
                },
            )
            self.tournament.channels[f"table-{i}-text"] = channel.id
            channel = await self.guild.create_voice_channel(
                name=f"Table {i}",
                category=self.category,
                overwrites={
                    self.guild.default_role: perm.NO_VOICE,
                    spectator_role: perm.SPECTATE_VOICE,
                    table_role: perm.VOICE,
                    judge_role: perm.JUDGE_VOICE,
                },
            )
            self.tournament.channels[f"table-{i}-vocal"] = channel.id
        self.update()
        messages = await self.send_embed(embed)
        asyncio.gather(*(m.pin() for m in messages))

    async def standings(self):
        await self._check_judge()
        embed = discord.Embed(title="Standings")
        scores, winner = await self._get_total_scores(raise_on_incorrect=False)
        results = []
        last = [math.nan] * 3
        place = 1
        for j, (vekn, score) in enumerate(
            sorted(
                scores.items(),
                key=lambda a: (a[0] not in self.tournament.dropped, a[1]),
                reverse=True,
            ),
            1,
        ):
            if vekn in self.tournament.dropped:
                place = "**[D]**"
            elif winner and j == 1:
                place = "**WINNER**"
            elif last != score[:3]:
                place = 2 if winner and 2 < j < 6 else j
            last = score[:3]
            results.append(
                f"- {place}. {self._player_display(vekn)} "
                f"{self._score_display(score)}"
            )
        embed.description = "\n".join(results)
        await self.send_embed(embed)

    async def results(self):
        await self._check_judge()
        if not self.tournament.current_round:
            await self.send("No seating has been done yet.")
            return
        if (
            self.tournament.finals_seeding
            and len(self.tournament.results) > self.tournament.rounds
        ):
            embed = discord.Embed(title="Finals", description="")
            for i, vekn in enumerate(self.tournament.finals_seeding, 1):
                result = self.tournament.results[-1].get(vekn, 0)
                embed.description += f"{i}. {self._player_display(vekn)}: {result}VP\n"
            await self.send_embed(embed)
        else:
            index = min(self.tournament.current_round, self.tournament.rounds) - 1
            embed = discord.Embed(title=f"Round {index+1}")
            if len(self.tournament.results) <= index:
                embed.description = "No table has reported their result yet."
                await self.send_embed(embed)
            result, tables, incorrect = self.tournament._compute_round_result(index)
            incorrect = set(incorrect)
            for i, table in enumerate(tables, 1):
                status = "OK"
                if sum(result[vekn][1] for vekn in table) == 0:
                    status = "NOT REPORTED"
                elif i in incorrect:
                    status = "INVALID"
                embed.add_field(
                    name=f"Table {i} {status}",
                    value="\n".join(
                        f"{i}. {self._player_display(vekn)} "
                        f"{self._score_display(result[vekn])}"
                        for i, vekn in enumerate(table, 1)
                    ),
                    inline=True,
                )
            await self.send_embed(embed)

    async def unseat(self):
        await self._check_judge()
        await self._remove_tables()
        await self._check_current_round_modifiable()
        self.tournament.current_round -= 1
        self.tournament.finals_seeding = []
        self.update()
        await self.send("Seating cancelled.")

    async def add(self, *args):
        await self._check_judge()
        await self._check_current_round_modifiable()
        user_id, vekn = await self._get_mentioned_player(*args[:1])
        id_to_number = {v: k for k, v in self.tournament.player_numbers.items()}
        number = id_to_number.get(vekn)
        if not number:
            await self.send("Player not properly registered")
            return
        member = self.guild.get_member(user_id)
        if not member:
            await self.send("Player not in server")
            return
        if vekn in self.tournament.disqualified:
            await self.semd("Player is disqualified")
            return
        index = self.tournament.current_round - 1
        tables = self.tournament._get_round_tables(index)
        for i, table in enumerate(tables, 1):
            if len(table) > 4:
                continue
            prev = self.tournament.seating[index].index(id_to_number[table[3]])
            self.tournament.seating[index].insert(prev + 1, number)
            self.tournament.dropped.discard(vekn)
            for role in self.guild.roles:
                if role.name == f"{self.tournament.prefix}Table-{i}":
                    await member.add_roles(
                        role,
                        reason=f"{self.tournament.name} Tournament: added by judge",
                    )
                    break
            else:
                await self.send("Table role not found")
                return
            self.update()
            await self.send(f"Player seated 5th on table {i}")
            break
        else:
            await self.send("No table available to sit this player in")

    async def _remove_tables(self):
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

    async def _check_round(self, round=None):
        if not self.tournament.current_round:
            await self.send("Tournament has not begun")
            raise CommandFailed("No current round")
        if len(self.tournament.results) < self.tournament.current_round:
            self.tournament.results.append({})
        if round and len(self.tournament.results) < round:
            await self.send("Invalid round number")
            raise CommandFailed("Bad round number")

    async def report(self, vps):
        vps = float(vps.replace(",", "."))
        vekn = await self._get_vekn(self.message.author.id)
        await self._check_round()
        index = self.tournament.current_round - 1
        if self.tournament.finals:
            if vekn not in self.tournament.finals_seeding:
                await self.send("You did not participate in the finals")
                return
        elif vekn not in [
            self.tournament.player_numbers[n] for n in self.tournament.seating[index]
        ]:
            await self.send("You did not participate in this round")
            return
        if vps > 5:
            await self.send("That seems like too many VPs")
            return
        self.tournament.results[index][vekn] = vps
        self.update()
        await self.send("Result registered.")

    async def fix(self, round, vekn, vps):
        round = int(round)
        vps = float(vps)
        _, vekn = await self._get_mentioned_player(vekn)
        await self._check_judge()
        await self._check_round(round)
        results = self.tournament.results[round - 1]
        if vps <= 0:
            results.pop(vekn, None)
        else:
            results[vekn] = vps
        self.update()
        await self.send("Fixed")

    async def validate(self, round, table, *args):
        round = int(round)
        table = int(table)
        reason = " ".join(args)
        await self._check_judge()
        await self._check_round(round)
        self.tournament.overrides[f"{round}-{table}"] = reason
        self.update()
        await self.send("Validated")

    async def close(self):
        await self._check_judge()
        if self.channel.id in self.tournament.channels.values():
            await self.send(
                "The `close` command must be issued outside of tournament channels"
            )
            return
        reports = []
        scores, winner = await self._get_total_scores(raise_on_incorrect=False)
        results = []
        last = [math.nan] * 3
        rank = 1
        j = 0
        for vekn, score in sorted(
            scores.items(),
            key=lambda a: (
                a[0] == winner,
                a[0] in self.tournament.finals_seeding,
                a[1],
            ),
            reverse=True,
        ):
            if vekn in self.tournament.dropped:
                rank = "DQ"
            else:
                j += 1
                if last != score[:3] and (
                    vekn not in self.tournament.finals_seeding or j == 2
                ):
                    rank = j
            last = score[:3]
            number = await self._get_player_number(vekn)
            finals_position = ""
            if vekn in self.tournament.finals_seeding:
                finals_position = self.tournament.finals_seeding.index(vekn) + 1
            results.append(
                [
                    number,
                    vekn,
                    self.tournament.registered.get(vekn, ""),
                    (
                        sum(1 for s in self.tournament.seating if number in s)
                        + (1 if vekn in self.tournament.finals_seeding else 0)
                    ),
                    score[0],
                    score[1],
                    finals_position,
                    rank,
                ]
            )
        data = io.StringIO()
        writer = csv.writer(data)
        writer.writerow(
            [
                "Player Num",
                "V:EKN Num",
                "Name",
                "Games Played",
                "Games Won",
                "Total VPs",
                "Finals Position",
                "Rank",
            ]
        )
        writer.writerows(results)
        data = io.BytesIO(data.getvalue().encode("utf-8"))
        reports.append(discord.File(data, filename="Report.csv"))
        if self.tournament.registered and self.tournament.results:
            results = []
            for number, vekn in sorted(self.tournament.player_numbers.items()):
                if vekn not in self.tournament.players:
                    continue
                name = self.tournament.registered.get(vekn, "UNKNOWN").split(" ", 1)
                if len(name) < 2:
                    name.append("")
                results.append(
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
                        "DQ" if vekn in self.tournament.dropped else "",
                    ]
                )
            data = io.StringIO()
            writer = csv.writer(data)
            writer.writerows(results)
            data = io.BytesIO(data.getvalue().encode("utf-8"))
            reports.append(discord.File(data, filename="Methuselahs.csv"))
            for i, permutation in enumerate(self.tournament.seating, 1):
                if len(self.tournament.results) < i:
                    break
                results = []
                for j, table in enumerate(krcg.seating.Round(permutation), 1):
                    for number in table:
                        vekn = self.tournament.player_numbers[number]
                        name = self.tournament.registered.get(vekn, "UNKNOWN").split(
                            " ", 1
                        )
                        if len(name) < 2:
                            name.append("")
                        results.append(
                            [
                                number,
                                name[0],
                                name[1],
                                j,
                                self.tournament.results[i - 1].get(vekn, 0),
                            ]
                        )
                    if len(table) < 5:
                        results.append(["", "", "", "", ""])
                data = io.StringIO()
                writer = csv.writer(data)
                writer.writerows(results)
                data = io.BytesIO(data.getvalue().encode("utf-8"))
                reports.append(discord.File(data, filename=f"Round {i}.csv"))
            if (
                self.tournament.finals_seeding
                and len(self.tournament.results) > self.tournament.rounds
            ):
                results = []
                vekn_to_number = {
                    v: k for k, v in self.tournament.player_numbers.items()
                }
                for i, vekn in enumerate(self.tournament.finals_seeding, 1):
                    number = vekn_to_number[vekn]
                    name = self.tournament.registered.get(vekn, "UNKNOWN").split(" ", 1)
                    if len(name) < 2:
                        name.append("")
                    results.append(
                        [
                            number,
                            name[0],
                            name[1],
                            1,
                            i,
                            self.tournament.results[-1].get(vekn, 0),
                        ]
                    )
                data = io.StringIO()
                writer = csv.writer(data)
                writer.writerows(results)
                data = io.BytesIO(data.getvalue().encode("utf-8"))
                reports.append(discord.File(data, filename="Finals.csv"))
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
        cursor = self.connection.cursor()
        cursor.execute(
            "UPDATE tournament SET active=0 WHERE active=1 AND guild=?",
            [
                str(self.guild.id),
            ],
        )
        logger.info("closed tournament in %s", self.guild.name)
        await self.send("Tournament closed")


class ProgressUpdate:
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
