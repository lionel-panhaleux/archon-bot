# Archon Discord Bot

[![PyPI version](https://badge.fury.io/py/archon-bot.svg)](https://badge.fury.io/py/archon-bot)
[![Validation](https://github.com/lionel-panhaleux/archon-bot/workflows/Validation/badge.svg)](https://github.com/lionel-panhaleux/archon-bot/actions)
[![Python version](https://img.shields.io/badge/python-3.8-blue)](https://www.python.org/downloads/)
[![License](https://img.shields.io/badge/License-MIT-blue)](https://opensource.org/licenses/MIT)
[![Code Style](https://img.shields.io/badge/code%20style-black-black)](https://github.com/psf/black)

Discord bot for V:TES tournament management.
[Add it to your server](https://discordapp.com/oauth2/authorize?client_id=862326826193518622&scope=bot&permissions=401730896)

The bot requires quite a few permissions to create roles and channels properly.
Please do not tinker with the list of required permissions and grant them all
if you want the bot to run properly on your server.

## Cheatsheet

Players:

-   Check in: `archon checkin`
-   Check in with VEKN ID: `archon checkin 1000123`
-   Report your result: `archon report 2`
-   Drop out: `archon drop`

Judges:

-   Open the tournament: `archon open My Tournament`
-   Appoint judges: `archon appoint @someone`
-   Allow bots in players tables: `archon appoint @timer @krcg`
-   Registration: `archon register 1000123 Alice Allister`
-   List registered players: `archon registrations`
-   Upload registration file: `archon upload`
-   Open check-in: `archon checkin-start`
-   List checked in players: `archon players`
-   Close check-in: `archon checkin-stop`
-   Start a round: `archon round-start`
-   Issue caution: `archon caution text explaining why`
-   Issue warning: `archon warning text explaining why`
-   Disqualify: `archon disqualify @someone text explaining why`
-   Check the round results: `archon results`
-   Fix a result: `archon fix @someone 2`
-   Finish a round: `archon round-finish`
-   Display standings: `archon standings`
-   Run the finals: `archon finals`
-   Close the tournament: `archon close`

For commands mentioning a player, you can use either a Discord `@someone` mention
or the ID of the listed player.

## Usage

The archon bot can be used in a variety of tournament settings.
The archon bot can only run **one tournament per category** in a Discord server.

### Basic tournament handling

This is the most basic case, and it is easy. Just open a tournament:

```
archon open My Tournament
```

As organiser, you automatically get the Judge role and access to the `#Judges` channels.
You can appoint some additional judges to help you:

```
archon appoint @your_friendly_judge @another_one
```

Do not forget to **give the judge status to the bots** you want to make available
to the players in the table channels:

```
archon appoint @timer @krcg
```

You can optionally add some spectators:

```
archon spectator @some_guest
```

When you're about to start the first round, open the check-in:

```
archon checkin-start
```

Players can now check-in to the tournament by issuing simply:

```
archon checkin
```

You can display the list of checked-in players at any time

```
archon players
```

And display a reminder on how the check-in works for your players:

```
archon
```

You can close the check-in when you like (this is optional):

```
archon checkin-stop
```

Once everyone has checked in, you can start the first round:

```
archon round-start
```

This command is the heart of the archon bot, it does multiple thing:

-   Check if the previous round is finished and all results reported and consistent
-   Randomise and optimise the seating according to the
    [official guidelines [LSJ 20020323]](https://groups.google.com/g/rec.games.trading-cards.jyhad/c/4YivYLDVYQc/m/CCH-ZBU5UiUJ)
-   Display and pin the seating in the channel you issued the command
-   Create text and voice channels for each table
-   Display the seating in each table channel
-   Assign roles to the players so they get access to their respective table
-   The archon bot and the judges have access to all channels
-   Spectators have access to all channels but cannot read or write in them

Once the round is finished, players should report their result (VPs):

```
archon report 3
```

The bot computes game wins automatically.
You can check the results in the `#Judges` channel:

```
archon results
```

If some results are not correct, any judge can fix them:

```
archon fix @mistaken_player 2
```

Once everything is OK, you can close the round, no more VP report will be accepted.
This step is optional: you can also proceed to the next round directly.

```
archon round-finish
```

A judge can display the standings at any time,
in the `#Judges` channel or in a public one:

```
archon standings
```

When all rounds have been played and reported, you can launch the finals:

```
archon finals
```

The bot will do a "toss" to choose between runner-ups if necessary: it is random.
Channels are created and the seeding order will be displayed.
On a finals table, last seed chooses their seat first.
Once the finals is finished, have the players report their results as usual
or do it yourself with the `archon fix` command.
Once the report is done, you can close the tournament:

```
archon close
```

This command must be run outside of bot-created channels,
it will provide you a full tournament report as a CSV file.
Bot-created channels and roles will be deleted.

### Corner cases

There are a few corner cases you might hit when handling a tournament.

#### Late check-in

Some players might want to check in after the first round has already begun.
They can always just check in for the next round, provided you kept the check-in open.
Alternatively, if the other players haven't started to play yet,
you can add the late comer to a 4 players table after he checked in:

```
archon round-add @late_player
```

#### Player dropping out

Players can easily drop out of the tournament between rounds:

```
archon drop
```

They can check in again later to participate in future rounds.
The archon bot will take their absence into account when optimising the seating.

#### Reset a round

If players are missing or new players are arriving late, it might be better to
cancel the round you just started and start a new one:

```
archon round-reset
archon round-start
```

Note you can also use `round-reset` to reset the finals if you have a missing finalist.

#### Cautions, warnings, disqualification

Judges can issue cautions, warnings and disqualify players:

```
archon caution drew an additional card
archon warning additional cards again
archon warning misrepresented the rules
archon disqualify misrepresented the rules again
```

If any caution or warning has been previously issued, the bot will display
the previous issues so you can issue an additional penalty if you like.
A judge can see the issued cautions and warnings for a given player:

```
archon player @problematic_player
```

#### Fix a previous round result

You cannot close a round and start a new one if the score is incorrect,
but this does not mean mistakes cannot happen. To fix a mistake in a previous round,
a judge can use the `fix` command and indicate a previous round number.
For example, to remove player_1 VP and give it to player_2 in round 1:

```
archon fix @player_1 0 1
archon fix @player_2 1 1
```

#### Validate odd VP situations

In some situations, a judge might decide to change the available VP total on a table.
For example if a player drops out of a round at the very beginning, he might decide
to award only 0.5 VP to its predator, or no VP at all. In that case, the archon bot
will see the total result as invalid because the total does not match.
A judge can force an odd VP result to be accepted.
For example, validate Table 1 on Round 2:

```
archon validate 1 2 Alice dropped out on turn two
```

### Sanctioned tournament

Sanctionned tournaments require some additional steps.
You want to make sure the players have a valid VEKN ID number before letting them
check in. You can use the registration feature:

```
archon register 1000123 Alice Allister
```

Only a judge can issue the `register` command, they have to check the provided VEKN ID
on the [VEKN registry](https://www.vekn.net/player-registry) manually.
When using the registration feature, the archon bot will **deny check-in** to players
who are not registered.

If a player does not have a VEKN ID yet, you can still register them on a temporary ID
provide them a VEKN ID number after the tournament:

```
archon register - Alice Allister
```

Finally, you can check the list of all registered players
in the `#Judges` private channel:

```
archon registrations
```

Note if you want to check VEKN IDs programatically, this package includes a small
script for this. You'll need the python `aiohttp` package installed:

```bash
VEKN_LOGIN='your_login' VEKN_PASSWORD='your_password' ./check-vekn.py < vekn_ids.txt
```

This assumes you can feed a simple `vekn_ids.txt` listing one VEKN ID# per line.

#### Upload a registration file

You can also handle your registrations separately and just upload a file listing
the registered players:

```
archon upload
```

Just provide the registration file as an attachment. It must be a simple
[CSV file](https://en.wikipedia.org/wiki/Comma-separated_values)
with no header and 2 columns:

1. the player name
2. their VEKN ID

For example:

```csv
Alice Allister, 1000123
Bob Beril, 1000234
```

Note that this _updates_ the registrations list: you can upload multiple files,
each new input will override the previous ones based on the VEKN ID.
Previously registered VEKN IDs that do not appear in the new file will be kept.

#### Uploading an archon file to VEKN

Once you close the tournament, you will get CSV files matching the
[VEKN Archon file](http://www.vekn.net/downloads) structure:

```
archon close
```

If you used the registration feature, this will give you, in addition to the normal
`Results.csv` file, a `Methuselahs.csv` file, a `Round_N.csv` file for each round and
a `Finals.csv` file for the finals. You can import those files in a standard
[VEKN archon file](http://www.vekn.net/downloads)
in the relevant tabs using your favorite software import feature.

Fill up the `Tournament Info` tab manually and make sure to fill the "Coin Ranking"
in case there was a tie for a spot on the finals table.
Your archon file is ready for upload.

### League

You can use the archon bot to run a league, ie. a tournament spanning multiple weeks.
You can simply reset the check-in every tournament day:

```
archon checkin-reset
archon checkin-start
```

You can issue the `archon checkin-reset` command after the end of each round or day:
it will empty the players list and close the check-in until you open it again using
the `archon checkin-start` command. This way, every time a judge is present and wants
to run a league round, they just have to:

```
archon checkin-start
archon round-start
```

And once the round is finished:

```
archon round-finish
archon checkin-reset
```

This can also be used to run a normal tournament over multiple days.

The archon bot has no limit in the number of rounds and will optimise seating
on each round, taking into account the players attending or not.
Once all your league rounds are finished, you can check the standings as usual:

```
archon standings
```

For the finals, make sure you disqualify any absent top seed before launching:

```
archon disqualify @top_5_player absent for finals
archon finals
```

### Staggered tournament (6, 7 & 11 players)

A V:TES seating can accomodate any number of players,
except if you have 6, 7 or 11 players. In this situation, the `archon round-start`
command will yield an error because it cannot get a table for everyone.
You should either have some players drop out or additional players check in.

In case you want to run a tournament with 6, 7, or 11 players, you can setup a staggered
tournament by indicating the number of rounds played for each players:

```
archon staggered 2
```

The archon bot will devise a staggered round structure where everyone plays
exactly that number of rounds. For example, for everyone to play 2 rounds,
the bot will prepare 3 rounds where some players are left out of each round.
To play 3 rounds, it will prepare 4 or 5 rounds depending on the number of players.
You cannot setup a staggered tournament with more than 10 rounds played by player.

Beware that once you go for a staggered tournament,
no player can be added or removed between rounds. This means **check-in, drop out
and disqualifications are disabled** once you're in a staggered tournament.

### Offline tournament

Although the archon bot is primarily intended for online play, you can
use it to run an offline tournament too.

You can register the player as they come as usual:

```
archon register 1000123 Alice Allister
```

Once registrations are closed, you can just check all players in yourself:

```
archon checkin-all
```

Then run your rounds normally:

```
archon round-start
```

Any judge can register the results using the `fix` command over VEKN IDs:

```
archon fix 1000123 2
```

If a player checks in between rounds, you can register them and check them in:

```
archon register 1000234 Bob Beril
archon checkin 1000234 -
```

## Contribute

This is an Open Source software under MIT license. Contributions are welcome,
feel free to [open an issue](https://github.com/lionel-panhaleux/archon-bot/issues)
if you encounter a bug or have a feature request, and we will try to accomodate.
If you're up to it, pull requests are welcome and will be merged if they pass the tests.
