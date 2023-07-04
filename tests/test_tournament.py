import pytest

from archon_bot import serialize
from archon_bot import tournament


@pytest.mark.asyncio
async def test_tournament():
    tourney = tournament.Tournament(name="Test Tournament")
    alice = await tourney.add_player(name="Alice")
    assert alice.vekn
    assert serialize.dumpd(alice) == {
        "deck": {},
        "name": "Alice",
        "playing": False,
        "seed": 0,
        "vekn": alice.vekn,
    }
    bob = await tourney.add_player(name="Bob")
    claire = await tourney.add_player(name="Claire")
    doug = await tourney.add_player(name="Doug")
    # dropping players at registration removes them from the list
    tourney.drop(doug.vekn)
    assert serialize.dumpd(tourney) == {
        "current_round": 0,
        "dropped": {},
        "extra": {},
        "flags": 0,
        "max_rounds": 0,
        "name": "Test Tournament",
        "notes": {},
        "players": {
            alice.vekn: {
                "deck": {},
                "name": "Alice",
                "playing": False,
                "seed": 0,
                "vekn": alice.vekn,
            },
            bob.vekn: {
                "deck": {},
                "name": "Bob",
                "playing": False,
                "seed": 0,
                "vekn": bob.vekn,
            },
            claire.vekn: {
                "deck": {},
                "name": "Claire",
                "playing": False,
                "seed": 0,
                "vekn": claire.vekn,
            },
        },
        "rounds": [],
        "state": "REGISTRATION",
        "winner": "",
    }
    tourney.open_checkin()
    # players added now are ready to play by default, others need to check in
    await tourney.add_player(vekn=alice.vekn)
    await tourney.add_player(vekn=bob.vekn)
    doug2 = await tourney.add_player(name="Doug")
    emily = await tourney.add_player(name="Emily")
    # eg. claire has not checked in, she's not playing
    assert tourney.players[alice.vekn].playing is True
    assert tourney.players[bob.vekn].playing is True
    assert tourney.players[claire.vekn].playing is False
    assert doug2.vekn != doug.vekn
    assert tourney.players[doug2.vekn].name == "Doug"
    assert tourney.players[doug2.vekn].playing is True
    assert tourney.players[emily.vekn].name == "Emily"
    assert tourney.players[emily.vekn].playing is True
