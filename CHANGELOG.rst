2.4 (2023-11-21)
----------------

- Fix decklists for /download-reports
- Bump krcg version


2.3 (2023-11-11)
----------------

- Tournament limited format management (`/define-limited`), with VDB format files


2.2 (2023-11-07)
----------------

- Bump hikari version


2.1 (2023-11-07)
----------------

- Fix DB reset


2.0 (2023-11-07)
----------------

- Refactor: use dataclasses, all discord infos in extra (including players discord ID)
- Performance: use orjson, async DB commands and connection pool
- VPs check now covers all invalid cases instead of just a few
- Removed table text channels, use the voice channels chat instead
- Displaying Seating score and add a link to official criteria
- New separate Upload Decklist command, allowing file upload and text pasting
- New separate staggered commands
- The /announce command can now be used anywhere
- Better help messages all around
- New /batch-register command to upload a CSV list of players

1.3 (2023-10-28)
----------------

- Try and fix DB connection errors when the DB server unexpectedly closes the connection
- Fix rounds number check (was off by one)
- Fixed typos in README


1.2 (2023-03-12)
----------------

- Fix major player numbering issue (ended up mixing scores and seating in LBL)
- Improved logging

1.1 (2023-02-26)
----------------

- Appointed judges get the root Archon-Judge role too
- Fix spurious table creation in DB
- Fix use of max_rounds setting

1.0 (2023-02-21)
----------------

- Improve DB access and concurrency management
- Better Discord roles and channels management
- Improved documentation
- Fix a bug preventing to download reports in niche cases (unnamed players)
- Fix a bug dropping players when registering their discord handle after the round begins

0.23 (2022-05-21)
-----------------

- Try to fix DB


0.22 (2022-05-21)
-----------------

- Fix DB


0.21 (2022-05-21)
-----------------

- Fix check-in


0.20 (2022-05-21)
-----------------

- Fix drop-player


0.19 (2022-05-21)
-----------------

- Prevent a new round to start when previous one has not been closed yet.
- Multiple fixes
- Added the reset-channels command
- Include a JSON of decklists in reports

0.18 (2022-05-21)
-----------------

- Fix player number attribution
- Improve help messages


0.17 (2022-05-20)
-----------------

- Full working version with slash commands. Beta - a few bugs might remain.


0.16 (2022-04-15)
-----------------

- Fix build


0.15 (2022-04-15)
-----------------

- Fix registering another user


0.14 (2022-04-14)
-----------------

- Fix command registration
- Check registered decks for banned cards


0.13 (2022-04-14)
-----------------

- Improve command registration


0.12 (2022-04-14)
-----------------

- Handle new VDB domain name (now vdb.im)


0.11 (2022-04-02)
-----------------

- Alpha V1
- Using PostgreSQL as backend database for easier external tooling
- Clear separation between internal logic and bot interface for future interfaces
- Using slash commands for better UX
- Tests pending
- Archon files export unavailable
- Probably quite a few bugs remaining, to be field-tested

0.10 (2022-01-03)
-----------------

- Fix KRCG version to old seating for now.


0.9 (2022-01-03)
----------------

- Fix VPs check on finals
- Indicate count in Players and Registrations
- Remove read access to finals text channels (so table password can be shared easily)

0.8 (2021-12-13)
----------------

- Improve VEKN API logging


0.7 (2021-11-22)
----------------

- Fix checkin with spurious name
- Fix rounds limit


0.6 (2021-11-06)
----------------

- Fix ``rounds-limit`` message
- Fix checkin on round limits


0.5 (2021-10-24)
----------------

- Add self-registration for players
- Add the possibility to limit the nuumber of rounds


0.4 (2021-09-28)
----------------

- Fixed judge checkin
- Improved archon help: now display judges commands in the judges channel only
- Fixed round-add


0.3 (2021-09-26)
----------------

- Players list fixed: only checked-in players are now displayed


0.2 (2021-07-07)
----------------

- Fix main


0.1 (2021-07-07)
----------------

- First public version
