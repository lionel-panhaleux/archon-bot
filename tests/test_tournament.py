import pytest

from archon_bot import tournament


@pytest.mark.asyncio
async def test_tournament():
    tourney = tournament.Tournament(name="Test Tournament")
    alice = await tourney.add_player(name="Alice")
    assert alice.to_json() == {
        "deck": {},
        "discord": 0,
        "name": "Alice",
        "number": 1,
        "playing": False,
        "seed": 0,
        "vekn": "P-1",
    }
    await tourney.add_player(name="Bob")
    await tourney.add_player(name="Claire")
    await tourney.add_player(name="Doug")
    # dropping players at registration removes them from the list
    tourney.drop("P-4")
    assert tourney.to_json() == {
        "current_round": 0,
        "dropped": {},
        "extra": {},
        "flags": tournament.TournamentFlag(0),
        "max_rounds": 0,
        "name": "Test Tournament",
        "next_number": 5,
        "notes": {},
        "players": [
            {
                "deck": {},
                "discord": 0,
                "name": "Alice",
                "number": 1,
                "playing": False,
                "seed": 0,
                "vekn": "P-1",
            },
            {
                "deck": {},
                "discord": 0,
                "name": "Bob",
                "number": 2,
                "playing": False,
                "seed": 0,
                "vekn": "P-2",
            },
            {
                "deck": {},
                "discord": 0,
                "name": "Claire",
                "number": 3,
                "playing": False,
                "seed": 0,
                "vekn": "P-3",
            },
        ],
        "rounds": [],
        "state": "REGISTRATION",
        "winner": "",
    }
    tourney.open_checkin()
    # players added now are ready to play by fdefault, others need to check in
    await tourney.add_player(vekn="P-1")
    await tourney.add_player(vekn="P-2")
    await tourney.add_player(name="Doug")
    await tourney.add_player(name="Emily")
    # eg. claire has not checked in, she's not playing
    assert tourney.players["P-1"].playing is True
    assert tourney.players["P-2"].playing is True
    assert tourney.players["P-3"].playing is False
    assert tourney.players["P-5"].name == "Doug"
    assert tourney.players["P-5"].playing is True
    assert tourney.players["P-6"].name == "Emily"
    assert tourney.players["P-6"].playing is True
