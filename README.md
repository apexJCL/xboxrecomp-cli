# xboxrecomp-cli

Turn your own copy of an original Xbox game into a program that runs on
your PC or Mac. The game's code is translated to C and compiled with the
[xboxrecomp](https://github.com/apexJCL/xboxrecomp) toolkit. It is not an
emulator.

- You need your own, legally dumped copy of the game (a disc image, or the
  folder with its files).
- No game files are included or downloaded. What is built from your copy
  stays on your own computers.
- Each game has its own repository. This one holds the tool they share and
  the **build wizard**, a small program with a window that does the build
  for you. On Windows and macOS you never need a terminal.

Two words used below: your **game dump** is the copy you made from your
own disc (a disc image, or a folder with `default.xbe` in it); the
**bundle** is the installable game the wizard makes from it.

## Getting started

1. Pick your game in the table below and open its **Releases** page.
2. Download the wizard for your computer: the file whose name ends in
   `windows-x64.zip`, `macos.zip` or `linux-x64.tar.gz` (`SHA256SUMS` on
   the same page is optional: it lets you verify the download).
3. Open it: see [Opening the wizard](#opening-the-wizard) for your
   computer.
4. The wizard says what it will do, then checks your computer and fetches
   or offers to install what is missing: the build tools (each download
   checked against a known checksum) and, on a Mac, Apple's Command Line
   Tools and Homebrew (its installer asks for your password). It sends
   nothing of yours; if you leave the box ticked, it asks the releases
   page once whether a newer wizard exists.
5. Point it at your game dump.
6. Choose the target (usually the computer you are on) and the game's
   options (render scale, fullscreen and the like).
7. Build. This takes a while: BLiNX 2 takes about 20 to 40 minutes on the
   computers it was timed on. Closing the window stops the build; a later
   build reuses most of the work.
8. Press **Install now**, or open the output folder and copy the bundle to
   the computer you play on: on Windows a setup program; on a Mac a `.dmg`
   (drag the app to Applications); on a Steam Deck or Linux PC a `.tar` to
   unpack, then `./install.sh --steam` in a terminal opened in the
   unpacked folder, with Steam open (the bundle's README explains the
   rest).

You need about 15 GB free for the build folder, plus room for the bundle
(your game files and about 1 GB more). The wizard shows both folders on
its first page and lets you change them; the bundle lands in a `dist`
folder beside the wizard.

### Opening the wizard

**Windows.** Unzip and run `<game>-wizard.exe`. SmartScreen warns because
the program is not signed: click *More info*, then *Run anyway*. The setup
program the build makes warns the same way.

**macOS.** Unzip, then move `<game>-wizard.app` out of Downloads (into your
home folder, say) before opening it; opened in place, macOS runs it from a
temporary location and the bundle goes to the build folder instead. The
first time, macOS refuses to open it because it is not signed: close the
message, open *System Settings > Privacy & Security*, click *Open Anyway*
near the bottom and confirm. Do the same once for the game's app after
installing it.

**Linux and Steam Deck.** On the Deck, use Desktop Mode. Extract the
archive into a folder of its own, open a terminal there (Dolphin:
right-click > *Open Terminal Here*) and run `./<game>-wizard`
([docs/wizard.md](docs/wizard.md#the-icon) has the lines for a menu
entry). After the install the game is in your Steam library; in Game Mode,
pick the Gamepad template in its controller settings.

## Supported games

| Game | Releases | Status |
|---|---|---|
| BLiNX 2: Masters of Time & Space | [apexJCL/blinx2-recomp](https://github.com/apexJCL/blinx2-recomp/releases) | Plays on Windows 11, on Steam Deck and Linux (Proton) and on macOS (Apple silicon) |
| Burnout 3: Takedown | [apexJCL/burnout3-recomp](https://github.com/apexJCL/burnout3-recomp/releases) | Coming soon |

Built on the [xboxrecomp](https://github.com/sp00nznet/xboxrecomp) toolkit
by sp00nznet, which [apexJCL/xboxrecomp](https://github.com/apexJCL/xboxrecomp)
extends for these ports; its runtime carries code from
[xemu](https://github.com/xemu-project/xemu). The wizard's credits list
every project a build relies on.

## Help

- [docs/troubleshooting.md](docs/troubleshooting.md): the messages and
  their fixes.
- Logs: `wizard.log` in the wizard's settings folder (`%APPDATA%\<game>-wizard`,
  `~/Library/Application Support/<game>-wizard` or `~/.config/<game>-wizard`),
  and the build's own in `src/build-logs/` under the build folder, which a
  failed build names, with a button to open it.
- Report a game problem on the game's repository, and a wizard or command
  line problem at
  [apexJCL/xboxrecomp-cli/issues](https://github.com/apexJCL/xboxrecomp-cli/issues).
  Attach the log, never the bundle or your game files.

## Developers

`xbr` is the command line the wizard runs. A game is a folder with a
`game.toml`; the CLI holds no game's values, and nobody installs this
repository: a game's `./<game>` bootstrap fetches the CLI at the release
the game pins ([docs/bootstrap.md](docs/bootstrap.md)). The host needs uv,
git and Python ([what the host needs](docs/bootstrap.md#what-the-host-needs));
`setup` fetches the rest. To build an existing game, clone it and run
`./<game> /path/to/game.iso`. To start a new one:

```sh
uvx --from git+https://github.com/apexJCL/xboxrecomp-cli xbr new mygame --xbe /path/to/game.iso
cd mygame
./mygame import /path/to/game.iso
./mygame setup     # the toolchain into the game's tree
./mygame all       # analyze, recomp, build: build-win/mygame_recomp.exe
```

The first run of a new game crashes; the port starts there.

| Document | Contents |
|---|---|
| [docs/commands.md](docs/commands.md) | every command, option and tool |
| [docs/manifest.md](docs/manifest.md) | `game.toml`, key by key |
| [docs/porting.md](docs/porting.md) | the porting loop, the keys a port meets first, macOS |
| [docs/wizard.md](docs/wizard.md) | the build wizard: the screens, the folders, the config, releases |
| [docs/bootstrap.md](docs/bootstrap.md) | how a game finds the CLI, the pins, what the host needs |
| [docs/troubleshooting.md](docs/troubleshooting.md) | the messages and their fixes |
| [docs/maintainers.md](docs/maintainers.md) | working on the CLI, releases, the audit before a public push |
| [docs/origin.md](docs/origin.md) | where each file came from |

## Licence

MIT, see [LICENSE](LICENSE). Third-party notices: `NOTICES.txt` in each
wizard archive, and [LICENSES/](LICENSES) here. The games are not included
and never will be.
