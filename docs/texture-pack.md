# Texture packs

`enhance textures build` makes a pack of higher-resolution textures for a game
you own, from your own game files and from dumps of your own runs. The toolkit's
runtime swaps them in at their upload, by the hash of the texture the game made.
Stock behaviour does not change: no pack, no swap.

## What a pack is, legally

**A pack is owner-generated.** It is derived from your game files, on your
machine, for you.

- Nothing in this CLI or in a game's repository is a pack, a dump or a decoded
  texture. Never publish one or share it, and keep your dumps out of any
  repository: `layout.py`'s checks and `public export` refuse `*.xtd`, `*.pack`,
  `textures-pack.toml` and `enhance-textures.local.toml` at any depth.
- The CLI ships no model and downloads none. No model or paid service is
  bundled or a default. The default upscalers are the integer ones built in.
- A command preset is a template over a tool you installed yourself. `backend`
  shows each one's licence and homepage. A model's licence is yours to read: the
  ones known to restrict use are flagged, and using one is your decision. It may
  bear on what you can do with the pack, which is one more reason not to share it.
- `plugins/torch/` is a separate optional package, never a dependency of the
  CLI, and it has no network server.

## The pipeline

1. `mygame enhance textures inventory --dump DIR ...`: every texture of the
   sources and dumps, with the hash the runtime looks up. A dump comes from a
   run with `RECOMP_DEBUG=tex_dump=DIR`. Dump the scenes you care about: a
   texture the game changes on load has no known hash until a dump holds it.
2. `mygame enhance textures estimate --preset P`: bytes per class, VRAM per
   dumped scene and minutes, before any of it is spent.
3. `mygame enhance textures compare`: optional. The same crops through every
   upscaler you have, in a grid to judge by eye. Then `enhance textures pick
   CLASS UPSCALER` writes the choice into the config.
4. `mygame enhance textures build --preset P`: into
   `dist/enhance/textures/textures.pack`, with `textures-pack.toml`, its record.
5. `mygame package steamos --texture-pack`: the pack in your private bundle,
   enabled in its `config.default.toml`. Or copy the pack beside your build and
   point `[textures] pack` in `config.toml` at it.

Each texture goes: alpha split, edge padding by its address mode, the upscaler,
crop, recombine, mip chain, encode. The steps are integer arithmetic, so the
built-in upscalers give the same pack bytes on any machine, whatever the job count.

## Presets

| Preset | What it takes |
|---|---|
| `hud` | the HUD set of `[enhance.hud]`, with the same payloads as `enhance hud` |
| `ui` | the classes `ui`, `font` and `text` |
| `world` | those and `world` |
| `all` | every entry, whatever its class |
| `traced` | every entry, at the scale a magnification trace proposed (`inventory --propose`) |
| `custom` | as `all`: say what you want with your config's classes and rules |

A game's `[enhance.textures] presets` adds its own.

| Class | Scale | Upscaler | Alpha | Edges | Mips | Encode |
|---|---|---|---|---|---|---|
| `font` | 4 | the font fit with a face, else `xbr` | split | clamp | none | raw |
| `text` | 2 | `xbr` | split | clamp | none | bc3 |
| `ui` | your `render_scale`, 2 to 4 | `xbr` | split | clamp | none | raw |
| `world` | 2; a CNN model's up to 3, by `render_scale` | `lanczos` | split | auto | full | auto |

`encode = "auto"` looks at the texels, not at the format: BC1 when every one is
opaque, BC3 otherwise. A texture whose sides are not whole blocks stays raw, and
the build says so.

## The config

`config/enhance-textures.toml` is tracked and holds names, numbers and hashes:

```toml
schema = 1
preset = "world"
render_scale = 2

[class.world]
scale = 2
upscaler = "lanczos"

[rule."dds/radar*"]              # fnmatch over names; later rules win
upscaler = "nearest"

[rule."hash:0B060D91FB09F186"]   # an unnamed dump entry, by hash
skip = true

[rule."dds/floor_*"]
edges = "wrap"
scale = 4
upscaler = "esrgan"

[upscaler.esrgan]                # a command preset, by name
preset = "realesrgan-ncnn-vulkan"
```

The tracked file holds names, numbers and hashes, because a cloned repo's config
must not be able to run a program. `config/enhance-textures.local.toml` is this
machine's and never tracked: per upscaler its `template`, `binary`, `path`,
`model`, `model_dir`, `overrides`, `gpu` or `device`. A tracked file holding one
of those keys is refused before anything runs, with the line that says where it
goes. A preset's template ships in the CLI, so a preset stays selectable by name
from the tracked file. The pack's record keeps a tool's sha256, version and
licence, never a path.

```toml
[upscaler.esrgan]
path = "/opt/realesrgan/realesrgan-ncnn-vulkan"
model = "realesrgan-x4plus"
gpu = 0
```

A pack is rebuilt when what goes in it changes: an entry's class or treatment,
a fact the build reads of it, its dump file, the HUD table, the font's bytes, a
tool's or a model's bytes. Otherwise the build says the pack is current and keeps
it: moving a tool or a model is not a change, nor is a new scene that only raises
how often an entry was used.

The model files are what `model` names: that file or folder, at a path as written
or a bare name beside the tool; and `<model>.*` or `<model>-x<scale>.*` in
`model_dir`, or with no `model_dir` in the `models` folder beside the tool. The build sees no further than that. A wrapper
script, or a plugin that finds its own files, has bytes of its own only: change
what it runs and the build still says current, so delete the pack yourself.

A world texture no dump recorded an address mode for is padded as `wrap`, since
world textures mostly tile, and the build's summary counts them. A recorded mode
always wins. Every other class pads as `clamp` when none is recorded. When scenes
recorded different modes for a texture, per axis the one that repeats wins: `wrap`
over `mirror` over the clamps, whatever the order of the dumps.

## Upscalers

Built in: `nearest`, `box`, `xbr` (edge-directed, for HUD art and text), `epx`,
`lanczos` (for the world). Pure Python, integer, reproducible; slow on big
textures, and the estimate says how slow.

Over your own tools: `enhance textures backend` lists the presets and whether
each is on your `PATH`. `backend install torch` prints the line that installs
the torch plugin, for a model file you already have, and runs nothing. A pack
built with one of these is reproducible at most on one machine, and its record
says so (`reproducible_scope`).

Over a service: a table with a `url` upscales on another machine's GPU, or
through AUTOMATIC1111 or ComfyUI, and `enhance serve` is the server to run on
that machine. See [upscale-service.md](upscale-service.md).

## Reading a pack

The format is the toolkit's. A newer pack than a runtime reads is refused by it
at load, in its log, and the game runs stock. The class switches
(`[textures] font`, `text`, `world`) and the swap itself are the runtime's;
goldens run with no pack.
