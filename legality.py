import os
from krcg.deck import Deck
from krcg.vtes import VTES

VTES.load()

folder = "/Users/lpanhaleux/Downloads/ACDecks"
for f in os.listdir(folder):
    print(f)
    with open(os.path.join(folder, f)) as source:
        d = Deck.from_txt(source)
        banned = list(d.cards(lambda c: c.banned))
        if banned:
            print(f"[ILLEGAL] [      ] BANNED CARDS: {banned}")
        groups = {c.group for c in d}
        groups.discard(None)
        groups.discard("ANY")
        groups = sorted([int(g) for g in groups])
        if groups and (len(groups) > 2 or groups[-1] - groups[0] > 1):
            print("[ILLEGAL] [      ] BAD GROUPING")
