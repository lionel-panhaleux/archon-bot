import dataclasses
import pytest

from archon_bot import tournament


@pytest.mark.asyncio
async def test_tournament():
    tourney = tournament.Tournament(name="Test Tournament")
    alice = await tourney.add_player(name="Alice")
    assert alice.vekn.startswith("P")
    assert dataclasses.asdict(alice) == {
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
    assert dataclasses.asdict(tourney) == {
        "current_round": 0,
        "dropped": {},
        "exclude": [],
        "include": [],
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
    doug = await tourney.add_player(name="Doug")
    emily = await tourney.add_player(name="Emily")
    # eg. claire has not checked in, she's not playing
    assert tourney.players[alice.vekn].playing is True
    assert tourney.players[bob.vekn].playing is True
    assert tourney.players[claire.vekn].playing is False
    assert tourney.players[doug.vekn].name == "Doug"
    assert tourney.players[doug.vekn].playing is True
    assert tourney.players[emily.vekn].name == "Emily"
    assert tourney.players[emily.vekn].playing is True


@pytest.mark.asyncio
async def test_add_remove_player():
    tourney = tournament.Tournament(name="Test Tournament")
    tourney.open_checkin()
    await tourney.add_player(name="Alice")
    await tourney.add_player(name="Bob")
    await tourney.add_player(name="Claire")
    await tourney.add_player(name="Doug")
    await tourney.start_round()
    emily = await tourney.add_player(name="Emily")
    tourney.round_add(emily.vekn, 1)
    assert tourney.players[emily.vekn].playing is True
    info = tourney.player_info(emily.vekn)
    assert info.table == 1
    assert info.status == tournament.PlayerStatus.PLAYING
    tourney.round_remove(emily.vekn)
    assert tourney.players[emily.vekn].playing is False
    info = tourney.player_info(emily.vekn)
    assert info.table is None
    assert info.status == tournament.PlayerStatus.CHECKED_OUT


@pytest.mark.asyncio
async def test_dq():
    tourney = tournament.Tournament(name="Test Tournament")
    tourney.open_checkin()
    alice = await tourney.add_player(name="Alice")
    await tourney.add_player(name="Bob")
    await tourney.add_player(name="Claire")
    await tourney.add_player(name="Doug")
    await tourney.add_player(name="Emily")
    tourney.drop(alice.vekn, tournament.DropReason.DISQUALIFIED)
    assert tourney.players[alice.vekn].playing is False
    with pytest.raises(tournament.CommandFailed):
        await tourney.add_player(alice.vekn)
    await tourney.start_round()
    info = tourney.player_info(alice.vekn)
    assert info.table is None
    assert info.status == tournament.PlayerStatus.DISQUALIFIED


@pytest.mark.asyncio
async def test_dq_pre_checkin():
    tourney = tournament.Tournament(name="Test Tournament")
    alice = await tourney.add_player(name="Alice")
    bob = await tourney.add_player(name="Bob")
    claire = await tourney.add_player(name="Claire")
    doug = await tourney.add_player(name="Doug")
    emily = await tourney.add_player(name="Emily")
    tourney.drop(alice.vekn, tournament.DropReason.DISQUALIFIED)
    tourney.open_checkin()
    assert tourney.players[alice.vekn].playing is False
    assert (
        tourney.player_check_in(vekn=alice.vekn) == tournament.PlayerStatus.DISQUALIFIED
    )
    tourney.player_check_in(vekn=bob.vekn) == tournament.PlayerStatus.CHECKED_IN
    tourney.player_check_in(vekn=claire.vekn) == tournament.PlayerStatus.CHECKED_IN
    tourney.player_check_in(vekn=doug.vekn) == tournament.PlayerStatus.CHECKED_IN
    tourney.player_check_in(vekn=emily.vekn) == tournament.PlayerStatus.CHECKED_IN
    await tourney.start_round()
    info = tourney.player_info(alice.vekn)
    assert info.table is None
    assert info.status == tournament.PlayerStatus.DISQUALIFIED
