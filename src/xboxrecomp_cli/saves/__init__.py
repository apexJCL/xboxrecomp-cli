"""`xbr saves`: bring a player's own Xbox saves into a recompiled game's
save root (openspec change save-import). Standard library only: the
SteamOS bundle carries this package as it is.

  sources.py   folder and zip sources, the save root, the name rules
  procs.py     is the game (or an emulator) running
  importer.py  classify, plan, back up, stage, re-sign, rename into place
  cli.py       `saves list` and `saves import`
"""
