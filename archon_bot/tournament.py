import functools
import itertools
import math

import krcg.seating


@functools.total_ordering
class Score:
    def __init__(self, gw=0, vp=0, tp=0):
        self.gw = gw
        self.vp = vp
        self.tp = tp

    def __eq__(self, rhs):
        return (self.gw, self.vp, self.tp) == (rhs.gw, rhs.vp, rhs.tp)

    def __lt__(self, rhs):
        return (self.gw, self.vp, self.tp) < (rhs.gw, rhs.vp, rhs.tp)

    def __str__(self):
        return f"({self.gw}GW{self.vp}, {self.tp}TP)"


class Tournament:
    """Mostly POD tournament data."""

    JUDGES_TEXT = "judges-text"
    JUDGES_VOCAL = "judges-vocal"

    def __init__(self, **kwargs):
        self.name = kwargs.get("name", "")
        self.checkin = kwargs.get("checkin", False)
        self.staggered = kwargs.get("staggered", False)
        self.judge_role = kwargs.get("judge_role", 0)
        self.spectator_role = kwargs.get("spectator_role", 0)
        self.channels = kwargs.get("channels", {})
        self.rounds_limit = kwargs.get("rounds_limit", 0)
        self.current_round = kwargs.get("current_round", 0)
        self.reporting = kwargs.get("reporting", False)
        self.registered = kwargs.get("registered", {})
        self.players = kwargs.get("players", {})
        self.dropped = set(kwargs.get("dropped", []))
        self.disqualified = set(kwargs.get("disqualified", []))
        self.seating = kwargs.get("seating", [])
        self.finals_seeding = kwargs.get("finals_seeding", [])
        self.results = kwargs.get("results", [])
        self.overrides = kwargs.get("overrides", {})
        self.player_numbers = {
            int(k): v for k, v in kwargs.get("player_numbers", {}).items()
        }
        self.cautions = kwargs.get("cautions", {})
        self.warnings = kwargs.get("warnings", {})

    def __bool__(self):
        return bool(self.name)

    @property
    def prefix(self):
        return "".join([w[0] for w in self.name.split()][:3]) + "-"

    def to_json(self):
        return {
            "name": self.name,
            "checkin": self.checkin,
            "staggered": self.staggered,
            "judge_role": self.judge_role,
            "spectator_role": self.spectator_role,
            "channels": self.channels,
            "rounds_limit": self.rounds_limit,
            "current_round": self.current_round,
            "reporting": self.reporting,
            "registered": self.registered,  # ID -> name
            "players": self.players,  # ID -> discord user_id
            "dropped": list(self.dropped),  # ID
            "disqualified": list(self.disqualified),  # ID
            "player_numbers": self.player_numbers,  # seating number -> ID
            "seating": self.seating,  # [permutation]
            "finals_seeding": self.finals_seeding,  # [permutation]
            "results": self.results,  # ID -> VPs
            "overrides": self.overrides,  # (round, table) -> reason
            "cautions": self.cautions,  # ID -> [(Round#, caution reason)]
            "warnings": self.warnings,  # ID -> [(Round#, warning reason)]
        }

    def _get_round_tables(self, round=None):
        round = round or self.current_round
        return [
            [self.player_numbers[n] for n in table]
            for table in krcg.seating.Round(self.seating[round - 1])
        ]

    def _compute_round_result(self, round=None):
        """Compute actual round results.

        Return the results tables and incorrect tables for given round.
        """
        round = round or self.current_round
        round_result = {}
        if len(self.results) < round:
            return round_result, [[]], []
        vp_result = self.results[round - 1]
        if not vp_result or len(self.seating) + bool(self.finals_seeding) < round:
            return round_result, [[]], []
        if round > len(self.seating):
            finals = True
            tables = [self.finals_seeding]
        else:
            finals = False
            tables = self._get_round_tables(round)
        incorrect = []
        for i, table in enumerate(tables, 1):
            tps = [12, 24, 36, 48, 60]
            if len(table) == 4:
                tps.pop(2)
            scores = sorted([vp_result.get(vekn, 0), vekn] for vekn in table)
            for vp, players in itertools.groupby(scores, lambda a: a[0]):
                players = list(players)
                tp = sum(tps.pop(0) for _ in range(len(players))) // len(players)
                gw = 1 if tp == 60 and vp >= 2 else 0
                for _, vekn in players:
                    round_result[vekn] = Score(gw, vp, tp)
            if f"{round}-{i}" not in self.overrides and sum(
                math.ceil(a[0]) for a in scores
            ) != len(table):
                incorrect.append(i)
            scores = [vp_result.get(vekn, 0) for vekn in table]
            # if someone has a VP, their prey does not get 0.5 from timeout
            # don't check that on finals, since actual seating is unknown
            if not finals:
                for j, score in enumerate(scores):
                    if score % 1 and scores[j - 1] >= 1:
                        incorrect.append(i)
        return round_result, tables, incorrect
