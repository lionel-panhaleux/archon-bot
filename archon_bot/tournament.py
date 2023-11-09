import collections
import enum
import itertools
import logging
import math
import os
import random
from dataclasses import dataclass, field
from typing import Callable, Optional, Tuple, Union

import aiohttp
import asgiref.sync
import krcg.deck
import krcg.seating
import krcg.utils


logger = logging.getLogger()
ITERATIONS = 30000
VEKN_LOGIN = os.getenv("VEKN_LOGIN")
VEKN_PASSWORD = os.getenv("VEKN_PASSWORD")


class CommandFailed(Exception):
    """A "normal" failure: a message explains why the command was not performed"""


class ErrorDecklistRequired(CommandFailed):
    """Missing decklist"""


class PlayerNotRegistered(CommandFailed):
    """Player not registered"""


class ErrorMaxRoundReached(CommandFailed):
    """The player has reached the maximum amount of rounds allowed, no check in"""


class TournamentFlag(enum.IntFlag):
    VEKN_REQUIRED = enum.auto()  # whether a VEKN ID# is required for this tournament
    DECKLIST_REQUIRED = enum.auto()  # whether a decklist must be submitted
    CHECKIN_EACH_ROUND = enum.auto()  # whether players must check-in at every round
    MULTIDECK = enum.auto()  # whether players can change deck between rounds
    REGISTER_BETWEEN = enum.auto()  # whether players can register between rounds
    STAGGERED = enum.auto()  # whether this is a staggered (6, 7, 11) tournament
    SINGLE_CLAN = enum.auto()  # limited: 75% of the crypt must be a single clan
    SINGLE_VAMPIRE = enum.auto()  # limited: single vampire in crypt


class TournamentState(str, enum.Enum):
    REGISTRATION = "REGISTRATION"  # tournament has not begun, registration is open
    CHECKIN = "CHECKIN"  # check-in is open for next round
    PLAYING = "PLAYING"  # round in progress
    WAITING_FOR_CHECKIN = "WAITING_FOR_CHECKIN"  # waiting for next round check-in
    WAITING_FOR_START = "WAITING_FOR_START"  # waiting for next round to start
    FINISHED = "FINISHED"  # tournament is finished, finals have been played


class DropReason(str, enum.Enum):
    DROP = "DROP"
    DISQUALIFIED = "DISQUALIFIED"


class NoteLevel(str, enum.Enum):
    NOTE = "NOTE"
    OVERRIDE = "OVERRIDE"
    CAUTION = "CAUTION"
    WARNING = "WARNING"


class PlayerStatus(str, enum.Enum):
    NOT_REGISTERED = "NOT_REGISTERED"
    CHECKED_IN = "CHECKED_IN"
    CHECKIN_REQUIRED = "CHECKIN_REQUIRED"
    DROPPED_OUT = "DROPPED_OUT"
    DISQUALIFIED = "DISQUALIFIED"
    PLAYING = "PLAYING"
    MAX_ROUNDS_PLAYED = "MAX_ROUNDS_PLAYED"
    MISSING_DECK = "MISSING_DECK"
    WAITING = "WAITING"
    CHECKED_OUT = "CHECKED_OUT"


class DeckIssue:
    @dataclass
    class ShortLibrary:
        missing: int

        def __str__(self) -> str:
            return f"The library is too short: it misses {self.missing} cards"

    @dataclass
    class BigLibrary:
        extra: int

        def __str__(self) -> str:
            return f"The library is too big: it has {self.missing} cards too many"

    @dataclass
    class ShortCrypt:
        missing: int

        def __str__(self) -> str:
            return f"The crypt is too short: it misses {self.missing} cards"

    @dataclass
    class InvalidGrouping:
        groups: list[int]

        def __str__(self) -> str:
            return f"Invalid grouping in crypt: {self.groups}"

    @dataclass
    class BannedCards:
        cards: list[str]

        def __str__(self) -> str:
            return f"Banned cards: {self.cards}"

    @dataclass
    class ExcludedCards:
        cards: list[str]

        def __str__(self) -> str:
            return f"Excluded cards: {self.cards}"

    class SingleClanViolation:
        def __str__(self) -> str:
            return f"The crypt must have a 75% majority clan (9 out of 12)"

    class SingleVampireViolation:
        def __str__(self) -> str:
            return f"The crypt must contain a single vampire"


DeckIssueType = Union[
    DeckIssue.ShortLibrary,
    DeckIssue.BigLibrary,
    DeckIssue.ShortCrypt,
    DeckIssue.InvalidGrouping,
    DeckIssue.BannedCards,
    DeckIssue.ExcludedCards,
    DeckIssue.SingleClanViolation,
    DeckIssue.SingleVampireViolation,
]


@dataclass(order=True)
class Score:
    gw: int = 0
    vp: float = 0.0
    tp: int = 0

    def __str__(self):
        if self.gw:
            return f"({self.gw}GW{self.vp:.2g}, {self.tp}TP)"
        else:
            return f"({self.vp:.2g}VP, {self.tp}TP)"

    def __add__(self, rhs):
        return self.__class__(
            gw=self.gw + rhs.gw, vp=self.vp + rhs.vp, tp=self.tp + rhs.tp
        )

    def __iadd__(self, rhs):
        self.gw += rhs.gw
        self.vp += rhs.vp
        self.tp += rhs.tp
        return self


@dataclass
class Note:
    judge: str
    level: NoteLevel = NoteLevel.NOTE
    text: str = ""


@dataclass(unsafe_hash=True)
class Player:
    vekn: str = field(compare=True)
    name: str = field(default="", compare=False)
    deck: dict = field(default_factory=dict, compare=False)
    playing: bool = field(default=False, compare=False)
    seed: int = field(default=0, compare=False)

    def __str__(self):
        s = f"#{self.vekn}"
        if self.name:
            s = f"{self.name} " + s
        return s


@dataclass
class Round:
    seating: krcg.seating.Round = field(default_factory=krcg.seating.Round)
    results: dict[str, Score] = field(default_factory=dict)
    overrides: dict[int, Note] = field(default_factory=dict)
    finals: bool = False

    def score(self) -> set[int]:
        """Returns the list of incorrect tables"""
        incorrect = set()
        if not self.seating:
            return
        for table_num in range(1, self.seating.tables_count() + 1):
            if not self.score_table(table_num):
                incorrect.add(table_num)
        return incorrect

    def score_player(self, player: Player) -> bool:
        """Returns True if the player's table score is correct/complete"""
        if not self.seating:
            return
        for table_num, table in enumerate(self.seating.iter_tables(), 1):
            if player.vekn in table:
                return self.score_table(table_num)

    def score_table(self, table_num: int) -> bool:
        """Returns True if the table score is correct/complete"""
        table = self.seating[table_num - 1]
        tps = [12, 24, 36, 48, 60]
        if len(table) == 4:
            tps.pop(2)
        vps = sorted([self.results.get(vekn, Score()).vp, vekn] for vekn in table)
        for vp, players in itertools.groupby(vps, lambda a: a[0]):
            players = list(players)
            tp = sum(tps.pop(0) for _ in range(len(players))) // len(players)
            gw = 1 if tp == 60 and vp >= 2 else 0
            for _, vekn in players:
                self.results[vekn] = Score(gw=gw, vp=vp, tp=tp)
        if table_num not in self.overrides:
            if sum(math.ceil(a[0]) for a in vps) != len(table):
                return False
            if not self.finals:
                vps = [self.results.get(vekn, Score()).vp for vekn in table]
                # remove successive ousts
                while len(vps) > 1:
                    for j, score in enumerate(vps):
                        if score <= 0:
                            # player had only a half point left, but prey is ousted
                            if score % 1:
                                return False
                            # the score can be -X already, X more points for the pred
                            vps[(j - 1) % len(vps)] += score - 1
                            break
                    # no more oust, those who remain should all have 0.5 left
                    else:
                        return all(x == 0.5 for x in vps)
                    vps.pop(j)
                # only one left, one point left for last player standing
                # (no need to check, it follows from previous checks)
        return True


Rank = Tuple[int, str, Score]


async def _check_vekn(vekn: str) -> str:
    logger.info("Checking VEKN# %s", vekn)
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
            raise CommandFailed("Unable to authentify to VEKN")

        async with session.get(
            f"https://www.vekn.net/api/vekn/registry?filter={vekn}",
            headers={"Authorization": f"Bearer {token}"},
        ) as response:
            result = await response.json()
            result = result["data"]
            if isinstance(result, str):
                raise CommandFailed(f"VEKN returned an error: {result}")
            result = result["players"]
            if len(result) > 1:
                raise CommandFailed("Invalid VEKN ID# (it should be 7 digits)")
            if len(result) < 1:
                raise CommandFailed("VEKN ID# not found")
            result = result[0]
            if result["veknid"] != str(vekn):
                raise CommandFailed("VEKN ID# not found")
            return result["firstname"] + " " + result["lastname"]


@dataclass
class PlayerInfo:
    """Comprehensive player information"""

    player: Player
    status: PlayerStatus
    rounds: int = 0
    score: Score = field(default_factory=Score)
    notes: list[Note] = field(default_factory=list)
    table: Optional[int] = None
    position: Optional[int] = None


@dataclass
class Tournament:
    """Tournament data and base methods"""

    name: str = ""
    flags: TournamentFlag = 0
    max_rounds: int = 0
    current_round: int = 0
    include: list[int] = field(default_factory=list)
    exclude: list[int] = field(default_factory=list)
    state: TournamentState = TournamentState.REGISTRATION
    players: dict[str, Player] = field(default_factory=dict)
    dropped: dict[str, DropReason] = field(default_factory=dict)
    rounds: list[Round] = field(default_factory=list)
    notes: dict[str, list[Note]] = field(default_factory=dict)
    winner: str = ""
    extra: dict = field(default_factory=dict)

    def __bool__(self):
        return bool(self.name)

    def is_limited(self):
        return (
            self.include
            or self.exclude
            or self.flags & TournamentFlag.SINGLE_CLAN
            or self.flags & TournamentFlag.SINGLE_VAMPIRE
        )

    async def add_player(
        self,
        vekn: Optional[str] = None,
        prev_vekn: Optional[str] = None,
        name: Optional[str] = None,
        judge: bool = False,
    ) -> Player:
        """Used for both check-in and registration.

        It can be called multiple times to fill in the player info piece by piece
        """
        temp_vekn = False
        # figure out the VEKN if not provided
        # a temporary ID prefied with "P-" can be assigned if a judge calls the command
        # or if a VEKN is not required for this tournament (ie. unsanctioned)
        # remove spurious prefix if any
        vekn = self._safe_vekn(vekn)
        # handle vekn absence
        if not vekn:
            if self.flags & TournamentFlag.VEKN_REQUIRED and not judge:
                raise CommandFailed(
                    "Only a judge can register a player without VEKN ID#"
                )
            else:
                # use next number, not count
                # droping before first removes a player from the list and could collide
                vekn = f"P{random.randint(10000, 99999)}"
                temp_vekn = True
        # make sure not to overwrite a previous registration with the same VEKN ID
        # only a judge can override a previous vekn use
        # except if the previous player has dropped
        if vekn in self.players:
            if vekn in self.dropped:
                if not judge and self.dropped[vekn] == DropReason.DISQUALIFIED:
                    raise CommandFailed(
                        "Player was disqualified: only a judge can reinstate them"
                    )
                del self.dropped[vekn]
            # OK to call the method again with a temp_vekn generated by it
            if vekn.startswith("P"):
                temp_vekn = True
        else:
            if self.flags & TournamentFlag.STAGGERED:
                raise CommandFailed(
                    "Tournament is staggered, no more registration allowed."
                )
        # checks for change of vekn
        if prev_vekn and prev_vekn not in self.players:
            raise ValueError("prev_vekn is not listed in players")
        if prev_vekn and prev_vekn in self.dropped:
            if not judge and self.dropped[prev_vekn] == DropReason.DISQUALIFIED:
                raise CommandFailed(
                    "Player was disqualified: only a judge can change their VEKN#"
                )
        # check VEKN# against the VEKN registry, get registered name
        if vekn and not temp_vekn:
            vekn_name = await _check_vekn(vekn)
            if name:
                name = f"{name} ({vekn_name})"
            else:
                name = vekn_name
        elif prev_vekn:
            raise ValueError("Can only provide a prev_vekn with a new vekn")
        # handle change of VEKN (replace everywhere)
        if prev_vekn:
            dict_replace(self.players, prev_vekn, vekn)
            dict_replace(self.dropped, prev_vekn, vekn)
            dict_replace(self.notes, prev_vekn, vekn)
            for round in self.rounds:
                for table in round.seating:
                    for i, v in enumerate(table):
                        if v == prev_vekn:
                            table[i] = vekn
                dict_replace(round.results, prev_vekn, vekn)
        # upsert player information (name, deck)
        if vekn in self.players:
            player = self.players[vekn]
            player.vekn = vekn  # needs to change if it was replaced from prev_vekn
            if name:
                player.name = name
        else:
            player = Player(
                vekn=vekn,
                name=name or "",
            )
            self.players[player.vekn] = player
        self.player_check_in(player=player)
        return player

    def add_player_deck(
        self,
        vekn: str,
        deck: krcg.deck.Deck,
        judge: bool = False,
    ) -> Player:
        vekn = self._safe_vekn(vekn)
        if not deck:
            raise CommandFailed("No deck provided")
        if vekn not in self.players:
            raise PlayerNotRegistered("Player not registered")
        if (
            not judge
            and self.current_round
            and not self.flags & TournamentFlag.MULTIDECK
        ):
            raise CommandFailed("The tournament has started: too late to change deck")
        player = self.players[vekn]
        if not judge and self.state == TournamentState.PLAYING and player.playing:
            raise CommandFailed(
                "Round in progress: wait for it to end to change your deck"
            )
        if self.check_deck(deck):
            raise CommandFailed("Invalid deck")
        player = self.players[vekn]
        player.deck = deck.to_minimal_json()
        self.player_check_in(player=player)
        return player

    def check_deck(
        self,
        deck: krcg.deck.Deck,
    ) -> [DeckIssueType]:
        """Check if the deck is tournament legal, return all deck issues"""
        issues = []
        library_count = deck.cards_count(lambda c: c.library)
        crypt_count = deck.cards_count(lambda c: c.crypt)
        if library_count < 60:
            issues.append(DeckIssue.ShortLibrary(60 - library_count))
        if library_count > 90:
            issues.append(DeckIssue.BigLibrary(library_count - 90))
        if crypt_count < 12:
            issues.append(DeckIssue.ShortCrypt(12 - crypt_count))
        groups = set(c.group for c, _ in deck.cards(lambda c: c.crypt))
        groups.discard("ANY")
        groups = list(groups)
        if len(groups) > 2 or abs(int(groups[0]) - int(groups[-1])) > 1:
            issues.append(DeckIssue.InvalidGrouping(groups))
        banned = [c.name for c, _ in deck.cards(lambda c: c.banned)]
        if any(banned):
            issues.append(DeckIssue.BannedCards(banned))
        if self.exclude:
            excluded = [c.name for c, _ in deck.cards(lambda c: c.id in self.exclude)]
            if any(excluded):
                issues.append(DeckIssue.ExcludedCards(excluded))
        if self.include:
            excluded = [
                c.name for c, _ in deck.cards(lambda c: c.id not in self.include)
            ]
            if any(excluded):
                issues.append(DeckIssue.ExcludedCards(excluded))
        if self.flags & TournamentFlag.SINGLE_CLAN:
            clans = collections.Counter(
                [c.clan for c, _ in deck.cards(lambda c: c.crypt)]
            )
            _, count = clans.most_common()
            if count < crypt_count * 0.75:
                issues.append(DeckIssue.SingleClanViolation())
        if self.flags & TournamentFlag.SINGLE_VAMPIRE:
            vampires = set([c.name for c, _ in deck.cards(lambda c: c.crypt)])
            if len(vampires) > 1:
                issues.append(DeckIssue.SingleVampireViolation())
        return issues

    def player_check_in(
        self,
        player: Player = None,
        vekn: str = None,
        judge: bool = False,
    ) -> PlayerStatus:
        """Check the player in. Call with the player object, or just their vekn."""
        if not player:
            vekn = self._safe_vekn(vekn)
            if vekn not in self.players:
                raise PlayerNotRegistered("Player not registered")
            player = self.players[vekn]
        # do not change playing status while playing
        if player.playing and self.state == TournamentState.PLAYING:
            return PlayerStatus.PLAYING
        if self.max_rounds and self.player_rounds_played(player) >= self.max_rounds:
            player.playing = False
            return PlayerStatus.MAX_ROUNDS_PLAYED
        # decklist requirement on check-in
        if self.flags & TournamentFlag.DECKLIST_REQUIRED and not player.deck:
            player.playing = False
            return PlayerStatus.MISSING_DECK
        if self.state == TournamentState.CHECKIN or (
            self.state == TournamentState.WAITING_FOR_START
            and (judge or self.flags & TournamentFlag.REGISTER_BETWEEN)
        ):
            player.playing = True
            return PlayerStatus.CHECKED_IN
        return PlayerStatus.CHECKED_OUT

    def _safe_vekn(self, vekn: str) -> str:
        if not vekn:
            return ""
        vekn = str(vekn).strip("#")
        return vekn

    def _check_player(self, vekn: str) -> str:
        if vekn not in self.players:
            raise CommandFailed("Player is not registered")
        return self.players[vekn]

    def drop(
        self,
        vekn: str,
        reason: DropReason = DropReason.DROP,
    ) -> None:
        """Remove a player from the tournament.

        It's either recorded as DropReason.DROP or DropReason.DISQUALIFIED.
        Voluntary drops can always come back in a later round.
        Disqualified players cannot only be reinstated by a judge.
        """
        self._check_player(vekn)
        if self.dropped.get(vekn, None) == DropReason.DISQUALIFIED:
            raise CommandFailed("Player is already disqualified")
        elif reason == DropReason.DROP and not self.rounds:
            self.players.pop(vekn)
            self.dropped.pop(vekn, None)
        else:
            self.dropped[vekn] = reason
            self.players[vekn].playing = False

    def _reset_checkin(self) -> None:
        for player in self.players.values():
            player.playing = False

    def open_checkin(self) -> None:
        if self.flags & TournamentFlag.STAGGERED:
            raise CommandFailed("No check-in for staggered tournaments")
        if self.state == TournamentState.PLAYING:
            raise CommandFailed("The current round must be finished first")
        if self.state not in [
            TournamentState.CHECKIN,
            TournamentState.WAITING_FOR_START,
        ]:
            # REGISTRATION, WAITING_FOR_CHECKIN
            self._reset_checkin()
        # REGISTRATION, WAITING_FOR_* and CHECKIN
        self.state = TournamentState.CHECKIN

    def close_checkin(self) -> None:
        if self.state == TournamentState.CHECKIN:
            self.state = TournamentState.WAITING_FOR_START
        # REGISTRATION, WAITING_FOR_START, WAITING_FOR_CHECKIN and PLAYING stay as is

    async def start_round(self, progression_callback: Callable) -> Round:
        if self.state == TournamentState.REGISTRATION:
            raise CommandFailed("Check players in before starting the round")
        if self.state == TournamentState.PLAYING:
            raise CommandFailed("Finish the previous round before starting a new one")
        if self.state == TournamentState.FINISHED:
            raise CommandFailed("Tournament is finished")
        self.current_round += 1
        self.state = TournamentState.PLAYING
        if self.flags & TournamentFlag.STAGGERED:
            round = self.rounds[self.current_round - 1].seating
            playing = set(round.iter_players())
            for player in self.players.values():
                player.playing = player.vekn in playing
            return self.rounds[self.current_round - 1]
        # non-staggered
        players = [p.vekn for p in self.players.values() if p.playing]
        if len(players) < 4:
            raise CommandFailed("More players are required")
        if len(players) in [6, 7, 11] and not (self.flags & TournamentFlag.STAGGERED):
            raise CommandFailed(
                "A staggered tournament structure is required for 6, 7 or 11 players"
            )
        round = krcg.seating.Round.from_players(players)
        round.shuffle()
        self.rounds.append(Round(seating=round))
        score = None
        if self.current_round > 1:
            optimised_rounds, score = await asgiref.sync.sync_to_async(
                krcg.seating.optimise
            )(
                rounds=[r.seating for r in self.rounds],
                iterations=ITERATIONS,
                fixed=self.current_round - 1,
                callback=asgiref.sync.async_to_sync(progression_callback),
            )
            logger.info(
                "%s: optimised seating for round %s with score %s",
                self.name,
                self.current_round,
                score,
            )
            self.rounds[-1].seating = optimised_rounds[-1]
        return self.rounds[-1], score

    async def make_staggered(
        self, rounds_count: int, progression_callback: Callable
    ) -> None:
        """Make a tournament "staggered"

        For 6, 7 or 11 players only, use more rounds with players seating out some of
        them so that in the end everyone has played the same number of rounds.

        - rounds_count: number of rounds each player gets to play
        - callback is called every 100th of the way with arguments:
            * step
            * temperature
            * score
            * trials (since last callback call)
            * accepts (since last callback call)
            * improves (since last callback call)
        """
        if self.flags & TournamentFlag.STAGGERED:
            return
        if self.rounds:
            raise CommandFailed(
                "The tournament has already started: staggering is not possible anymore"
            )
        players = [p.vekn for p in self.players.values() if p.playing]
        if len(players) not in [6, 7, 11]:
            raise CommandFailed(
                "A staggered tournament requires exactly 6, 7 or 11 players"
            )
        rounds = krcg.seating.get_rounds(players, rounds_count)
        rounds, score = await asgiref.sync.sync_to_async(krcg.seating.optimise)(
            rounds=rounds,
            iterations=ITERATIONS,
            fixed=0,
            callback=asgiref.sync.async_to_sync(progression_callback),
        )
        self.rounds = [Round(seating=r) for r in rounds]
        logger.info(
            "%s: optimised seating for %s rounds with score %s",
            self.name,
            rounds_count,
            score,
        )
        self.flags |= TournamentFlag.STAGGERED
        # Staggered tournaments cannot allow registration between rounds
        if self.flags & TournamentFlag.REGISTER_BETWEEN:
            self.tournament.flags ^= TournamentFlag.REGISTER_BETWEEN
        self.state = TournamentState.WAITING_FOR_START

    def unmake_staggered(self) -> None:
        if not (self.flags & TournamentFlag.STAGGERED):
            return
        if self.current_round:
            raise CommandFailed(
                "The tournament has started: too late to modify its structure"
            )
        self.rounds = []
        self.flags ^= TournamentFlag.STAGGERED

    def round_add(self, player_id: str, table_num: int) -> None:
        """Add a player to the given table in current round.

        A convenience feature to add a late incoming player to a tournament,
        if by chance there is a 4 players table that hasn't started playing yet.
        """
        if self.flags & TournamentFlag.STAGGERED:
            raise CommandFailed("Cannot add a player in a staggered tournament")
        if player_id not in self.players:
            raise CommandFailed("Player is not registered")
        player = self._check_player(player_id)
        if not self.rounds:
            raise CommandFailed("No round in progress")
        if table_num > len(self.rounds[-1].seating) or table_num < 1:
            raise CommandFailed("Invalid table number")
        table = self.rounds[-1].seating[table_num - 1]
        if len(table) > 4:
            raise CommandFailed("Table has 5 players already")
        if self.max_rounds and self.player_rounds_played(player) >= self.max_rounds:
            raise ErrorMaxRoundReached()
        table.append(player.vekn)
        player.playing = True
        # if this is not first round, optimise the score
        # and make sure we don't repeat a predator-prey relation
        if len(self.rounds) > 1:
            krcg.seating.optimise_table(self.rounds, table_num - 1)

    def round_remove(self, player_id: str) -> int:
        """Remove a player from current round, returns the table number.

        A convenience feature to remove a late player from a round before starting,
        if by chance they were seated on a 5 players table.
        It is especially useful for handling unexpected drops during a tournament:
        if the player simply walks away between rounds and does not present himself
        on the table, you want to reorganise the seating if possible, to avoid
        repeating predator-prey relationships.
        """
        if self.flags & TournamentFlag.STAGGERED:
            raise CommandFailed("Cannot remove a player from a staggered tournament")
        player = self._check_player(player_id)
        if not self.rounds:
            raise CommandFailed("No round in progress")
        for table_num, _, _, p in self.rounds[-1].seating.iter_table_players():
            if player.vekn == p:
                break
        else:
            raise CommandFailed("User is not playing this round")
        table = self.rounds[-1].seating[table_num - 1]
        if len(table) < 5:
            raise CommandFailed("Table has only 4 players, unable to remove one.")
        table.remove(player.vekn)
        player.playing = False
        # if this is not first round, optimise the score
        # and make sure we don't repeat a predator-prey relation
        if len(self.rounds) > 1:
            krcg.seating.optimise_table(self.rounds, table_num - 1)
        return table_num

    def finish_round(self, keep_checkin=False) -> Round:
        """Mark the round as finished. Score gets frozen."""
        if not self.rounds or self.state != TournamentState.PLAYING:
            raise CommandFailed("No round in progress")
        incorrect = self.rounds[-1].score()

        if len(incorrect) > 1:
            raise CommandFailed(f"Incorrect score for tables {incorrect}")
        if len(incorrect) > 0:
            raise CommandFailed(f"Incorrect score for table {incorrect[0]}")
        if self.rounds[-1].finals:
            self.state = TournamentState.FINISHED
            self._reset_checkin()
            self.standings()  # compute the winner
        else:
            self.state = TournamentState.WAITING_FOR_START
            if self.flags & TournamentFlag.CHECKIN_EACH_ROUND and not keep_checkin:
                self._reset_checkin()
                self.state = TournamentState.WAITING_FOR_CHECKIN
            else:
                for player in self.players.values():
                    if (
                        self.max_rounds
                        and self.player_rounds_played(player) >= self.max_rounds
                    ):
                        player.playing = False
        return self.rounds[-1]

    def reset_round(self) -> Round:
        """Reset the current round. You can then start it anew using `start_round`."""
        if not self.rounds:
            raise CommandFailed("No round in progress")
        if self.rounds[-1].results:
            raise CommandFailed(
                "Some rounds results have been entered, the round cannot be reset."
            )
        round = self.rounds.pop(-1)
        self.current_round -= 1
        if round.finals:
            self.state = TournamentState.WAITING_FOR_START
        else:
            self.state = TournamentState.CHECKIN
        return round

    def _check_round_number(self, round_number: Optional[int] = None) -> int:
        """Return the actual round_number.

        None defaults to last round.
        """
        if round_number is None:
            return self.current_round
        if round_number < 1:
            raise CommandFailed(f"Invalid round number {round_number}")
        if round_number > len(self.rounds):
            raise CommandFailed(f"Round {round_number} has yet to be played")
        return round_number

    def tables_count(self, round_number: Optional[int] = None):
        round_number = self._check_round_number(round_number)
        return len(self.rounds[round_number - 1].seating)

    def report(
        self,
        player_id: str,
        vps: float = 0,
        round_number: Optional[int] = None,
    ) -> bool:
        """Report the number of VPs scored. Returns True if the table score is complete.

        round_number defaults to the current round.
        """
        round_number = self._check_round_number(round_number)
        player = self._check_player(player_id)
        round = self.rounds[round_number - 1]
        if player.vekn not in set(round.seating.iter_players()):
            raise CommandFailed("Player was not playing in that round")
        # do not let disqualified players enter VPs even if they were playing the round
        if self.dropped.get(player.vekn, None) == DropReason.DISQUALIFIED:
            raise CommandFailed("Player has been disqualified")
        if vps not in {0, 0.5, 1, 1.5, 2, 2.5, 3, 3.5, 4, 4.5, 5}:
            raise CommandFailed("VPs must be between 0 and 5")
        round.results[player.vekn] = Score(vp=vps)
        return round.score_player(player)

    def standings(self, toss=False) -> Tuple[Optional[str], list[Rank]]:
        """Return the winner (if any) and a full ranking [(rank, vekn, score)]

        If checking standings for finals list, toss should be True so that equal ranks
        get order randomly. In that case, record the seed order in Player.seed so that
        anyone winning a toss keeps their rank on subsequent calls, typically
        if the finals seating gets rollbacked because a finalist is missing.
        """
        winner = None
        for i, round in enumerate(
            self.rounds[: -1 if self.state == TournamentState.PLAYING else None], 1
        ):
            # check scores again, some VPs fixes might have happened
            incorrect = round.score()
            if incorrect:
                if len(incorrect) > 1:
                    raise CommandFailed(
                        f"Incorrect score for tables {incorrect} in round {i}"
                    )
                if len(incorrect) > 0:
                    raise CommandFailed(
                        f"Incorrect score for table {incorrect[0]} in round {i}"
                    )
            if round.finals and round.results:
                winner = max(
                    round.results.items(),
                    key=lambda a: (a[1], -self.players[a[0]].seed),
                )[0]
                # winning the finals counts as a GW even with less than 2 VPs
                # cf. VEKN Ratings system
                round.results[winner].gw = 1
        totals = collections.defaultdict(Score)
        for round in self.rounds:
            for vekn, score in round.results.items():
                totals[vekn] += score
        ranking = []
        last = Score()
        rank = 1
        for j, (vekn, score) in enumerate(
            sorted(
                totals.items(),
                key=lambda a: (
                    a[0] not in self.dropped,
                    winner == a[0],
                    a[1],
                    # put the seed here, so if you win the toss once, you keep your win
                    # 0 would go before negative seed numbers, math.inf always goes last
                    -self.players[a[0]].seed or -math.inf,
                    # toss
                    random.random() if toss else a[0],
                ),
                reverse=True,
            ),
            1,
        ):
            if vekn not in self.dropped:
                if winner and 1 < j < 6:
                    rank = 2
                elif last != score:
                    rank = j
                last = score
            if vekn in self.dropped and last is not None:
                last = None
                rank = j
            ranking.append((rank, vekn, score))
        self.winner = self.players[winner].vekn if winner else ""
        return winner, ranking

    def start_finals(self) -> Round:
        _, ranking = self.standings(toss=True)  # toss for finals seats if necessary
        top_5 = [vekn for (_rank, vekn, _score) in ranking[:5]]
        for p in self.players.values():
            p.seed = 0
            p.playing = False
        for i, vekn in enumerate(top_5, 1):
            self.players[vekn].seed = i
            self.players[vekn].playing = True
        # note register "seating" for finals is in fact seeding order
        # actual seating is not (yet) recorded
        self.current_round += 1
        self.rounds.append(
            Round(finals=True, seating=krcg.seating.Round.from_players(top_5))
        )
        self.state = TournamentState.PLAYING
        return self.rounds[-1]

    def rollback_round(self) -> None:
        if not self.rounds:
            raise CommandFailed("No round")
        if self.rounds[-1].results:
            raise CommandFailed("Round has been played")
        self.rounds.pop(-1)
        self.current_round -= 1
        self.state = TournamentState.WAITING_FOR_START

    def note(
        self,
        player_id: str,
        judge: str,
        level: NoteLevel,
        comment: str,
    ) -> None:
        """Take a note concerning a given player (judge command).

        Repeated NoteLevel.CAUTION should lead to a WARNING,
        Repeated NoteLevel.WARNING should lead to a disqualification (cf. drop())
        """
        vekn = self._check_player(player_id).vekn
        self.notes.setdefault(vekn, [])
        self.notes[vekn].append(
            Note(
                judge=str(judge),  # enforce str
                level=level,
                text=comment,
            )
        )

    def validate_score(
        self,
        table_number: int,
        judge: str,
        comment: str,
        round_number: Optional[int] = None,
    ) -> None:
        """Validate an odd score situation on a given table.

        This typically happens when a player drops or is disqualified and the expected
        VP is not or only partially attributed by the judge.
        """
        round_number = self._check_round_number(round_number)
        round = self.rounds[round_number - 1]
        if table_number < 1 or table_number > len(round.seating):
            raise CommandFailed("Invalid table number")
        round.overrides[table_number] = Note(
            level=NoteLevel.OVERRIDE, judge=judge, text=comment
        )

    def player_status(self, vekn: str):
        if vekn not in self.players:
            return PlayerStatus.NOT_REGISTERED
        player = self.players[vekn]
        drop = self.dropped.get(vekn, None)
        if drop == DropReason.DROP:
            return PlayerStatus.DROPPED_OUT
        elif drop == DropReason.DISQUALIFIED:
            return PlayerStatus.DISQUALIFIED
        elif drop:
            raise RuntimeError("unknown drop reason")
        if not player.deck and self.flags & TournamentFlag.DECKLIST_REQUIRED:
            return PlayerStatus.MISSING_DECK
        if self.state in [
            TournamentState.REGISTRATION,
            TournamentState.WAITING_FOR_CHECKIN,
        ]:
            return PlayerStatus.WAITING
        if self.state == TournamentState.CHECKIN:
            if player.playing:
                return PlayerStatus.CHECKED_IN
            elif (
                self.max_rounds and self.player_rounds_played(player) >= self.max_rounds
            ):
                return PlayerStatus.MAX_ROUNDS_PLAYED
            else:
                return PlayerStatus.CHECKIN_REQUIRED
        if self.state == TournamentState.WAITING_FOR_START:
            if player.playing:
                return PlayerStatus.CHECKED_IN
            elif (
                self.max_rounds and self.player_rounds_played(player) >= self.max_rounds
            ):
                return PlayerStatus.MAX_ROUNDS_PLAYED
            else:
                return PlayerStatus.CHECKED_OUT
        if self.state == TournamentState.FINISHED:
            return PlayerStatus.WAITING
        if self.state == TournamentState.PLAYING:
            if player.playing:
                return PlayerStatus.PLAYING
            else:
                return PlayerStatus.CHECKED_OUT

    def player_rounds_played(self, vekn: str):
        ret = 0
        for round in self.rounds:
            if vekn in round.seating.iter_players():
                ret += 1
        return ret

    def player_score(self, vekn: str):
        ret = Score()
        for round in self.rounds:
            ret += round.results.get(vekn, Score())
        return ret

    def player_info(self, vekn: str) -> PlayerInfo:
        """Returns a player information"""
        player = self._check_player(vekn)
        ret = PlayerInfo(
            player,
            status=self.player_status(vekn),
            rounds=self.player_rounds_played(vekn),
            score=self.player_score(vekn),
            notes=self.notes.get(vekn, []),
        )
        if self.rounds:
            round = self.rounds[-1]
            for table, position, _size, pid in round.seating.iter_table_players():
                if pid == vekn:
                    ret.table = table
                    ret.position = position
                    break
        return ret


def dict_replace(dic, k1, k2):
    """Replace k1 by k2 in dict, if it exists, and return True. Otherwise False"""
    v = dic.pop(k1, None)
    if v is not None:
        dic[k2] = v
