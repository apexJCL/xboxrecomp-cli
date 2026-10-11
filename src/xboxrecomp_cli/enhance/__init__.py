"""enhance: owner-made enhancement assets from the game's own files.

`xbr enhance hud` (hud.py) reads the game's IPK1 archives (ipk.py), decodes
the UI textures (dds.py), keys them by the toolkit's texture hash
(texhash.py), upscales them with an integer xBR kernel (scaler.py),
rasterises the Latin font atlas from a font file the owner points at
(font.py, atlas.py) and writes a texture pack (pack.py) that the runtime's
enhancements layer loads through `textures.pack`.

Everything here runs on the owner's machine on their own dump. A pack is
derived from game data: it is never distributed and never committed. The
tests build their own archives, textures, fonts and packs; nothing in the
repository holds game bytes.
"""
