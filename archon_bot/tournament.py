import itertools
import math

import krcg.seating


class Tournament:
    """Mostly POD tournament data."""

    JUDGES_TEXT = "judges-text"
    JUDGES_VOCAL = "judges-vocal"

    def __init__(self, **kwargs):
        self.name = kwargs.get("name", "")
        self.rounds = kwargs.get("rounds", 0)
        self.judge_role = kwargs.get("judge_role", 0)
        self.spectator_role = kwargs.get("spectator_role", 0)
        self.channels = kwargs.get("channels", {})
        self.current_round = kwargs.get("current_round", 0)
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

    @property
    def finals(self):
        return self.current_round == len(self.seating) + 1

    def to_json(self):
        return {
            "name": self.name,
            "rounds": self.rounds,
            "judge_role": self.judge_role,
            "spectator_role": self.spectator_role,
            "channels": self.channels,
            "current_round": self.current_round,
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

    def _get_round_tables(self, round):
        return [
            [self.player_numbers[n] for n in table]
            for table in krcg.seating.Round(self.seating[round])
        ]

    def _compute_round_result(self, round):
        """Compute actual round results.

        Return the results tables and incorrect tables for given round.
        """
        round_result = {}
        if len(self.results) <= round:
            return round_result, [[]], []
        vp_result = self.results[round]
        if round >= len(self.seating):
            return vp_result, [[]], []
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
                    round_result[vekn] = [gw, vp, tp]
            if f"{round+1}-{i}" not in self.overrides and sum(
                math.ceil(a[0]) for a in scores
            ) != len(table):
                incorrect.append(i)
        return round_result, tables, incorrect
