import collections
import os
from krcg.deck import Deck
from krcg.vtes import VTES

VTES.load()
decks = []
folder = "/Users/lpanhaleux/Downloads/ACDecks"
for f in os.listdir(folder):
    print(f)
    with open(os.path.join(folder, f)) as source:
        d = Deck.from_txt(source, id=f[:-4])
        decks.append(d)

crypt = collections.Counter()
library = collections.Counter()
for d in decks:
    for c, count in d.cards(lambda c: c.crypt):
        crypt.update({c: 1})
    for c, count in d.cards(lambda c: c.library):
        library.update({c: 1})

print("Top 10 crypt")
for c in crypt.most_common(10):
    print(c)

print("Top 10 library")
for c in library.most_common(100):
    print(c)

archetypes = collections.Counter()
clans = collections.Counter()
for d in decks:
    print(d.to_txt())
    clan = input("Clan: ")
    clans.update({clan: 1})
    arch = input("Archetype: ")
    archetypes.update({arch: 1})

print("Top clans")
for c in clans.most_common():
    print(c)

print("Top Archetypes")
for c in archetypes.most_common():
    print(c)

techs = collections.Counter()
for d in decks:
    if VTES["Anarch Convert"] not in d:
        continue
    print(d.to_txt())
    t = input("Techs: ")
    techs.update({a: 1 for a in t.split(",")})


print("Techs")
for t in techs.most_common():
    print(t)


for d in decks:
    clanset = {a for c, _ in d.cards(lambda c: c.crypt) for a in c.clans}
    clans.update(clanset)
