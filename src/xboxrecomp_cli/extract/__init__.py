"""extract: a game's stages from the owner's dump to a local Godot project.

`xbr extract` (cli.py) reads the stage trees game.toml's [extract] names
straight out of the XBE (image.py), parses the Sega Ninja chunk models
(ninja.py), decodes the textures the stage's texlist names (textures.py)
and writes one glTF 2.0 file per stage (gltf.py) into a Godot 4 project
(godot.py), which `--import` imports headless with the pinned Godot.

Everything here runs on the owner's machine on their own dump. The output
is game data: it stays under the game's dist/ (gitignored) and is never
committed or shared. No game constant lives here: the dialect, the
texture source and the stage roots are [extract] keys. The tests build
their own XBE images and chunk data in code.
"""
