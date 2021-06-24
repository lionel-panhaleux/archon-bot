from archon_bot import bot, commands
from . import conftest


commands.ITERATIONS = 100


@conftest.async_test
async def test_tournament_sanctioned(client_mock):
    guild = conftest.Guild(client_mock)
    user_1 = guild._create_member(1, "user_1")
    user_2 = guild._create_member(2, "user_2")
    await bot.on_ready()

    await bot.on_message(user_1.message("archon help"))
    with conftest.message(client_mock) as message:
        assert message == {
            "title": "Archon help",
            "description": (
                "`archon open [Rounds#] [tournament name]`: start a new tournament"
            ),
        }

    await bot.on_message(user_1.message("archon"))
    with conftest.message(client_mock) as message:
        assert message == "No tournament in progress. Use `archon open` to start one."

    await bot.on_message(user_1.message("archon open 2 Testing It"))
    with conftest.message(client_mock) as message:
        assert message == "Tournament open"

    roles = {r.name: r for r in guild.roles}
    assert "TI-Judge" in roles
    assert roles["TI-Judge"] in conftest.me.roles
    assert roles["TI-Judge"] in user_1.roles

    await bot.on_message(user_1.message("archon help"))
    with conftest.message(client_mock) as message:
        assert message == {
            "title": "Archon help",
            "description": (
                "**Player commands**\n"
                "- `archon help`: Display this help message\n"
                "- `archon status`: current tournament status\n"
                "- `archon checkin [ID#]`: check in for tournament (with VEKN "
                "ID# if required)\n"
                "- `archon report [VP#]`: Report your score for the round\n"
                "- `archon drop`: Drop from the tournament\n"
                "\n"
                "**Judge commands**\n"
                "- `archon appoint [@user]`: appoint user as judge\n"
                "- `archon spectator [@user]`: appoint user as spectator\n"
                "- `archon register [ID#] [Full Name]`: register a user (Use "
                "`-` for auto ID)\n"
                "- `archon checkin [ID#] [@user]`: check user in (even if "
                "disqualified)\n"
                "- `archon uncheck`: reset check-in\n"
                "- `archon allcheck`: check in all registered players\n"
                "- `archon players`: display the list of players\n"
                "- `archon seat`: Seat the next round\n"
                "- `archon add [@player | ID#]`: Add a player to the round (on "
                "a 4 players table)\n"
                "- `archon unseat`: Rollback the round seating\n"
                "- `archon results`: check current round results\n"
                "- `archon standings`: Display current standings\n"
                "- `archon warn [@player | ID#] [Reason]`: Issue a warning to "
                "a player\n"
                "- `archon disqualify [@player | ID#] [Reason]`: Disqualify a "
                "player\n"
                "- `archon close`: Close current tournament\n"
                "\n"
                "**Judge private commands**\n"
                "- `archon upload`: upload the list of registered players "
                "(attach CSV file)\n"
                "- `archon players`: display the list of players and their "
                "current score\n"
                "- `archon registrations`: display the list of registrations\n"
                "- `archon fix [Round] [ID#] [VP#]`: fix a VP report\n"
                "- `archon validate [Round] [Table] [Reason]`: validate an odd "
                "VP situation\n"
            ),
        }
    await bot.on_message(user_2.message("archon help"))
    with conftest.message(client_mock) as message:
        assert message == {
            "title": "Archon help",
            "description": (
                "**Player commands**\n"
                "- `archon help`: Display this help message\n"
                "- `archon status`: current tournament status\n"
                "- `archon checkin [ID#]`: check in for tournament (with VEKN "
                "ID# if required)\n"
                "- `archon report [VP#]`: Report your score for the round\n"
                "- `archon drop`: Drop from the tournament\n"
            ),
        }
    # #################################################################### appoint judge
    await bot.on_message(user_1.message("archon appoint <@2>"))
    with conftest.message(client_mock) as message:
        assert message == "Judge(s) appointed"
    assert roles["TI-Judge"] in user_2.roles
    await bot.on_message(user_1.message("archon"))
    with conftest.message(client_mock) as message:
        assert message == {
            "title": "Archon check-in",
            "description": (
                "**Discord check-in is required to play in this tournament**\n"
                "Use `archon checkin` to check in the tournament."
            ),
        }
    registrations = conftest.File(
        ("1234567,Alice\n" "2345678,Bob\n" "3456789,Charles\n" "4567890,Doug\n").encode(
            "utf-8"
        )
    )
    registrations.seek(0)
    await bot.on_message(user_1.message("archon upload", attachment=registrations))
    with conftest.message(client_mock) as message:
        assert message == "This command can only be issued in the <#Judges> channel"

    judges_channel = guild._get_channel("Judges")

    await bot.on_message(
        user_1.message(
            "archon upload", channel=judges_channel, attachment=registrations
        )
    )
    with conftest.message(client_mock) as message:
        assert message == "4 players registered"
    await bot.on_message(user_1.message("archon registrations", channel=judges_channel))
    with conftest.message(client_mock) as message:
        assert message == {
            "title": "Registrations",
            "description": (
                "- Alice #1234567 \n"
                "- Bob #2345678 \n"
                "- Charles #3456789 \n"
                "- Doug #4567890 \n"
            ),
        }
    # ######################################################################### check-in
    alice = guild._create_member(123, "Alice")
    bob = guild._create_member(234, "Bob")
    charles = guild._create_member(345, "Charles")
    doug = guild._create_member(456, "Doug")
    emili = guild._create_member(567, "Emili")
    await bot.on_message(alice.message("archon checkin 1234567"))
    with conftest.message(client_mock) as message:
        assert message == "<@123> checked in as Alice #1234567"
    await bot.on_message(bob.message("archon checkin 666"))
    with conftest.message(client_mock) as message:
        assert message == (
            "User not registered for that tournament.\n"
            "A <@&1> can use `archon register` to fix this."
        )
    await bot.on_message(bob.message("archon checkin 1234567"))
    with conftest.message(client_mock) as message:
        assert message == (
            "ID# already used by <@123>,\n"
            "they can `archon drop` so you can use this ID instead."
        )
    await bot.on_message(bob.message("archon checkin #2345678"))
    with conftest.message(client_mock) as message:
        assert message == "<@234> checked in as Bob #2345678"
    await bot.on_message(charles.message("archon checkin 3456789"))
    with conftest.message(client_mock) as message:
        assert message == "<@345> checked in as Charles #3456789"
    await bot.on_message(doug.message("archon checkin 4567890"))
    with conftest.message(client_mock) as message:
        assert message == "<@456> checked in as Doug #4567890"
    # ################################################################ late registration
    await bot.on_message(user_1.message("archon register 5678901 Emili"))
    with conftest.message(client_mock) as message:
        assert message == "Emili registered with ID# 5678901"
    await bot.on_message(user_1.message("archon checkin 5678901 <@567>"))
    with conftest.message(client_mock) as message:
        assert message == "<@567> checked in as Emili #5678901"
    await bot.on_message(user_1.message("archon status"))
    with conftest.message(client_mock) as message:
        assert message == (
            "**Testing It** (2R+F)\n" "5 players registered\n" "5 players checked in"
        )
    # ################################################################## seating round 1
    await bot.on_message(user_1.message("archon seat"))
    with conftest.message(client_mock) as message:
        assert message["title"] == "Round 1 seating"
        assert len(message["fields"]) == 1
        assert message["fields"][0]["name"] == "Table 1"
        assert "Alice #1234567 <@123>" in message["fields"][0]["value"]
        assert "Emili #5678901 <@567>" in message["fields"][0]["value"]
        assert alice._roles_names == {"TI-Table-1"}
        assert bob._roles_names == {"TI-Table-1"}
        assert charles._roles_names == {"TI-Table-1"}
        assert doug._roles_names == {"TI-Table-1"}
        assert emili._roles_names == {"TI-Table-1"}
    # ############################################################### warnings, drop, DQ
    await bot.on_message(user_1.message("archon warn 2345678 slow play"))
    with conftest.message(client_mock) as message:
        assert message == "Player warned"
    await bot.on_message(
        user_1.message(
            "archon warn <@234> rules misrepresentation",
            channel=guild._get_channel("table-1"),
        )
    )
    with conftest.message(client_mock, all=True) as messages:
        assert messages[0] == ("Player has been warned before:\n" "- R1: slow play")
        assert messages[1] == "Player warned"
    await bot.on_message(bob.message("archon drop"))
    with conftest.message(client_mock) as message:
        assert message == "<@234> dropped out"
    await bot.on_message(bob.message("archon checkin"))
    with conftest.message(client_mock) as message:
        assert message == "<@234> checked in as Bob #2345678"
    await bot.on_message(user_1.message("archon disqualify 2345678 inconsistent"))
    with conftest.message(client_mock) as message:
        assert message == "Player disqualifed"
    await bot.on_message(bob.message("archon checkin"))
    with conftest.message(client_mock) as message:
        assert message == "You've been disqualified, you cannot check in again."
    # #################################################################### round results
    await bot.on_message(user_1.message("archon seat"))
    with conftest.message(client_mock) as message:
        assert message == (
            "No table has reported their result yet, previous round cannot be closed. "
            "Use `archon unseat` to recompute a new seating."
        )
    await bot.on_message(user_1.message("archon results"))
    with conftest.message(client_mock) as message:
        assert message["title"] == "Round 1"
        assert message["description"] == "No table has reported their result yet."
    await bot.on_message(alice.message("archon report 4"))
    with conftest.message(client_mock) as message:
        assert message == "Result registered"
    await bot.on_message(user_1.message("archon results"))
    with conftest.message(client_mock) as message:
        assert message["title"] == "Round 1"
        assert message["fields"][0]["name"] == "Table 1 INVALID"
        assert "Alice #1234567 <@123> (1GW4.0, 60TP)" in message["fields"][0]["value"]
    await bot.on_message(user_1.message("archon fix 1 3456789 1"))
    with conftest.message(client_mock) as message:
        assert message == "Fixed"
    await bot.on_message(user_1.message("archon results"))
    with conftest.message(client_mock) as message:
        assert message["title"] == "Round 1"
        assert message["fields"][0]["name"] == "Table 1 OK"
        assert "Alice #1234567 <@123> (1GW4.0, 60TP)" in message["fields"][0]["value"]
        assert "Charles #3456789 <@345> (0GW1.0, 48TP)" in message["fields"][0]["value"]
    # ################################################################### check-in reset
    await bot.on_message(user_1.message("archon uncheck"))
    with conftest.message(client_mock) as message:
        assert message == "Check in reset."
    await bot.on_message(user_1.message("archon status"))
    with conftest.message(client_mock) as message:
        assert message == (
            "**Testing It** (2R+F)\n"
            "5 players registered\n"
            "0 players checked in\n"
            "Round 1 in progress"
        )
    await bot.on_message(alice.message("archon checkin"))
    with conftest.message(client_mock) as message:
        assert message == "<@123> checked in as Alice #1234567"
    await bot.on_message(bob.message("archon checkin"))
    with conftest.message(client_mock) as message:
        assert message == "You've been disqualified, you cannot check in again."
    await bot.on_message(charles.message("archon checkin 3456789"))
    with conftest.message(client_mock) as message:
        assert message == "<@345> checked in as Charles #3456789"
    await bot.on_message(doug.message("archon checkin"))
    with conftest.message(client_mock) as message:
        assert message == "<@456> checked in as Doug #4567890"
    emili2 = guild._create_member(678, "Emili")
    await bot.on_message(emili2.message("archon checkin"))
    with conftest.message(client_mock) as message:
        assert message == (
            "This tournament requires registration, please provide your VEKN ID."
        )
    await bot.on_message(emili2.message("archon checkin 5678901"))
    with conftest.message(client_mock) as message:
        assert message == "<@678> checked in as Emili #5678901"
    # ################################################################## seating round 2
    await bot.on_message(user_1.message("archon seat"))
    with conftest.message(client_mock) as message:
        assert message["title"] == "Round 2 seating"
        assert len(message["fields"]) == 1
        assert message["fields"][0]["name"] == "Table 1"
        assert "Alice #1234567 <@123>" in message["fields"][0]["value"]
        assert "Emili #5678901 <@678>" in message["fields"][0]["value"]
        assert "Bob #2345678 <@234>" not in message["fields"][0]["value"]
        assert alice._roles_names == {"TI-Table-1"}
        assert bob._roles_names == set()
        assert charles._roles_names == {"TI-Table-1"}
        assert doug._roles_names == {"TI-Table-1"}
        assert emili._roles_names == set()
        assert emili2._roles_names == {"TI-Table-1"}
    await bot.on_message(emili2.message("archon report 4"))
    with conftest.message(client_mock) as message:
        assert message == "Result registered"
    await bot.on_message(user_1.message("archon results"))
    with conftest.message(client_mock) as message:
        assert message["title"] == "Round 2"
        assert message["fields"][0]["name"] == "Table 1 OK"
        assert "Alice #1234567 <@123> (0GW0, 28TP)" in message["fields"][0]["value"]
        assert "Emili #5678901 <@678> (1GW4.0, 60TP)" in message["fields"][0]["value"]
    # ######################################################################## standings
    await bot.on_message(user_1.message("archon standings"))
    with conftest.message(client_mock) as message:
        assert message == {
            "title": "Standings",
            "description": (
                "- 1. Alice #1234567 <@123> (1GW4.0, 88TP)\n"
                "- 2. Emili #5678901 <@678> (1GW4.0, 84TP)\n"
                "- 3. Charles #3456789 <@345> (0GW1.0, 76TP)\n"
                "- 4. Doug #4567890 <@456> (0GW0, 52TP)\n"
                "- **[D]**. Bob #2345678 <@234> (0GW0, 24TP)"
            ),
        }
    # ########################################################################### finals
    await bot.on_message(user_1.message("archon seat"))
    with conftest.message(client_mock) as message:
        assert message == {
            "title": "Finals",
            "description": (
                "- 1 Alice #1234567 <@123> (1GW4.0, 88TP)\n"
                "- 2 Emili #5678901 <@678> (1GW4.0, 84TP)\n"
                "- 3 Charles #3456789 <@345> (0GW1.0, 76TP)\n"
                "- 4 Doug #4567890 <@456> (0GW0, 52TP)"
            ),
        }
    await bot.on_message(user_1.message("archon fix 3 4567890 1"))
    with conftest.message(client_mock) as message:
        assert message == "Fixed"
    await bot.on_message(user_1.message("archon results"))
    with conftest.message(client_mock) as message:
        assert message == {
            "title": "Finals",
            "description": (
                "1. Alice #1234567 <@123>: 0VP\n"
                "2. Emili #5678901 <@678>: 0VP\n"
                "3. Charles #3456789 <@345>: 0VP\n"
                "4. Doug #4567890 <@456>: 1.0VP\n"
            ),
        }
    await bot.on_message(user_1.message("archon fix 3 <@678> 3"))
    with conftest.message(client_mock) as message:
        assert message == "Fixed"
    await bot.on_message(user_1.message("archon results"))
    with conftest.message(client_mock) as message:
        assert message == {
            "title": "Finals",
            "description": (
                "1. Alice #1234567 <@123>: 0VP\n"
                "2. Emili #5678901 <@678>: 3.0VP\n"
                "3. Charles #3456789 <@345>: 0VP\n"
                "4. Doug #4567890 <@456>: 1.0VP\n"
            ),
        }
    await bot.on_message(user_1.message("archon standings"))
    with conftest.message(client_mock) as message:
        assert message == {
            "title": "Standings",
            "description": (
                "- **WINNER**. Emili #5678901 <@678> (2GW7.0, 84TP)\n"
                "- 2. Alice #1234567 <@123> (1GW4.0, 88TP)\n"
                "- 2. Charles #3456789 <@345> (0GW1.0, 76TP)\n"
                "- 2. Doug #4567890 <@456> (0GW1.0, 52TP)\n"
                "- **[D]**. Bob #2345678 <@234> (0GW0, 24TP)"
            ),
        }
    # ########################################################################### report
    await bot.on_message(user_1.message("archon close"))
    with conftest.message(client_mock, all=True, with_params=True) as message:
        assert message[0][0] == "Reports"
        assert "files" in message[0][1]
        assert len(message[0][1]["files"]) == 5  # Report, Methuselahs, 2 Rounds, Finals
        assert message[0][1]["files"][0].filename == "Report.csv"
        lines = message[0][1]["files"][0].fp.read().split(b"\r\n")
        assert lines[0] == (
            b"Player Num,V:EKN Num,Name,Games Played,Games Won,Total VPs,"
            b"Finals Position,Rank"
        )
        assert lines[1][1:] == b",5678901,Emili,3,2,7.0,2,1"
        assert lines[2][1:] == b",1234567,Alice,3,1,4.0,1,2"
        assert lines[3][1:] == b",3456789,Charles,3,0,1.0,3,2"
        assert lines[4][1:] == b",4567890,Doug,3,0,1.0,4,2"
        assert lines[5][1:] == b",2345678,Bob,1,0,0,,DQ"
        assert message[1][0] == "Tournament closed"
        assert message[0][1]["files"][1].filename == "Methuselahs.csv"
        lines = message[0][1]["files"][1].fp.read().split(b"\r\n")
        lines = [a[1:] for a in lines]
        assert b",Alice,,,1234567,3," in lines
        assert b",Emili,,,5678901,3," in lines
        assert b",Doug,,,4567890,3," in lines
        assert b",Charles,,,3456789,3," in lines
        assert b",Bob,,,2345678,1,DQ" in lines
        assert message[0][1]["files"][2].filename == "Round 1.csv"
        lines = message[0][1]["files"][2].fp.read().split(b"\r\n")
        lines = [a[1:] for a in lines]
        assert b",Doug,,1,0" in lines
        assert b",Bob,,1,0" in lines
        assert b",Alice,,1,4.0" in lines
        assert b",Charles,,1,1.0" in lines
        assert b",Emili,,1,0" in lines
        assert message[0][1]["files"][3].filename == "Round 2.csv"
        lines = message[0][1]["files"][3].fp.read().split(b"\r\n")
        lines = [a[1:] for a in lines]
        assert b",Alice,,1,0" in lines
        assert b",Emili,,1,4.0" in lines
        assert b",Charles,,1,0" in lines
        assert b",Doug,,1,0" in lines
        assert message[0][1]["files"][4].filename == "Finals.csv"
        lines = message[0][1]["files"][4].fp.read().split(b"\r\n")
        lines = [a[1:] for a in lines]
        assert b",Alice,,1,1,0" in lines
        assert b",Emili,,1,2,3.0" in lines
        assert b",Charles,,1,3,0" in lines
        assert b",Doug,,1,4,1.0" in lines


@conftest.async_test
async def test_tournament_casual(client_mock):
    guild = conftest.Guild(client_mock)
    user_1 = guild._create_member(1, "user_1")
    await bot.on_ready()

    await bot.on_message(user_1.message("archon help"))
    with conftest.message(client_mock) as message:
        assert message == {
            "title": "Archon help",
            "description": (
                "`archon open [Rounds#] [tournament name]`: start a new tournament"
            ),
        }

    await bot.on_message(user_1.message("archon"))
    with conftest.message(client_mock) as message:
        assert message == "No tournament in progress. Use `archon open` to start one."

    await bot.on_message(user_1.message("archon open 2 Testing It"))
    with conftest.message(client_mock) as message:
        assert message == "Tournament open"

    alice = guild._create_member(123, "Alice")
    bob = guild._create_member(234, "Bob")
    charles = guild._create_member(345, "Charles")
    doug = guild._create_member(456, "Doug")
    emili = guild._create_member(567, "Emili")
    await bot.on_message(alice.message("archon checkin"))
    with conftest.message(client_mock) as message:
        assert message == "<@123> checked in as #1"
    await bot.on_message(bob.message("archon checkin"))
    with conftest.message(client_mock) as message:
        assert message == "<@234> checked in as #2"
    await bot.on_message(charles.message("archon checkin"))
    with conftest.message(client_mock) as message:
        assert message == "<@345> checked in as #3"
    await bot.on_message(doug.message("archon checkin"))
    with conftest.message(client_mock) as message:
        assert message == "<@456> checked in as #4"
    await bot.on_message(emili.message("archon checkin"))
    with conftest.message(client_mock) as message:
        assert message == "<@567> checked in as #5"
    await bot.on_message(user_1.message("archon status"))
    with conftest.message(client_mock) as message:
        assert message == ("**Testing It** (2R+F)\n5 players checked in")
    # ################################################################## seating round 1
    await bot.on_message(user_1.message("archon seat"))
    with conftest.message(client_mock) as message:
        assert message["title"] == "Round 1 seating"
        assert len(message["fields"]) == 1
        assert message["fields"][0]["name"] == "Table 1"
        assert "#1 <@123>" in message["fields"][0]["value"]
        assert "#5 <@567>" in message["fields"][0]["value"]
        assert alice._roles_names == {"TI-Table-1"}
        assert bob._roles_names == {"TI-Table-1"}
        assert charles._roles_names == {"TI-Table-1"}
        assert doug._roles_names == {"TI-Table-1"}
        assert emili._roles_names == {"TI-Table-1"}
    await bot.on_message(alice.message("archon report 4"))
    with conftest.message(client_mock) as message:
        assert message == "Result registered"
    await bot.on_message(charles.message("archon report 1"))
    with conftest.message(client_mock) as message:
        assert message == "Result registered"
    # ################################################################## seating round 2
    await bot.on_message(user_1.message("archon seat"))
    with conftest.message(client_mock) as message:
        assert message["title"] == "Round 2 seating"
        assert len(message["fields"]) == 1
        assert message["fields"][0]["name"] == "Table 1"
        assert "#1 <@123>" in message["fields"][0]["value"]
        assert "#5 <@567>" in message["fields"][0]["value"]
    await bot.on_message(bob.message("archon report 3"))
    with conftest.message(client_mock) as message:
        assert message == "Result registered"
    await bot.on_message(doug.message("archon report 2"))
    with conftest.message(client_mock) as message:
        assert message == "Result registered"
    # ################################################################### seating finals
    await bot.on_message(user_1.message("archon seat"))
    with conftest.message(client_mock) as message:
        assert message["title"] == "Finals"
    await bot.on_message(alice.message("archon report 2.5"))
    with conftest.message(client_mock) as message:
        assert message == "Result registered"
    await bot.on_message(emili.message("archon report 1.5"))
    with conftest.message(client_mock) as message:
        assert message == "Result registered"
    await bot.on_message(user_1.message("archon close"))
    with conftest.message(client_mock, all=True, with_params=True) as message:
        assert message[0][0] == "Reports"
        assert "files" in message[0][1]
        assert len(message[0][1]["files"]) == 1  # Only Report
        assert message[0][1]["files"][0].filename == "Report.csv"
        lines = message[0][1]["files"][0].fp.read().split(b"\r\n")
        assert lines[0] == (
            b"Player Num,V:EKN Num,Name,Games Played,Games Won,Total VPs,"
            b"Finals Position,Rank"
        )
        assert lines[1][1:] == b",1,,3,2,6.5,1,1"
        assert lines[2][1:] == b",2,,3,1,3.0,2,2"
        assert lines[3][1:] == b",4,,3,0,2.0,3,2"
        assert lines[4][1:] == b",5,,3,0,1.5,5,2"
        assert lines[5][1:] == b",3,,3,0,1.0,4,2"
        assert message[1][0] == "Tournament closed"
