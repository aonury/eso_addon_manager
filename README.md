# ESO Addon Manager

A small, single-file addon manager for **The Elder Scrolls Online**. It reads your AddOns folder, checks every addon against [ESOUI](https://www.esoui.com/), and installs, updates or removes addons together with their dependencies.

You can use it from the command line or from a local web page that runs in your browser.

- One Python file, no dependencies to install (standard library only)
- Works on Windows, macOS and Linux, including Steam + Proton and Flatpak Steam
- Old versions are never deleted: they move to a backup folder first
- Missing dependencies are installed automatically

> Not affiliated with ZeniMax Online Studios, Bethesda or ESOUI. Addon data and downloads come from the public ESOUI (MMOUI) API.

---

## Contents

- [Requirements](#requirements)
- [Quick start](#quick-start)
- [Finding your AddOns folder](#finding-your-addons-folder)
- [The web page](#the-web-page)
  - [Installed tab](#installed-tab)
  - [Find on ESOUI tab](#find-on-esoui-tab)
- [Command line](#command-line)
- [How it works](#how-it-works)
- [Safety](#safety)
- [Files it creates](#files-it-creates)
- [Known limitations](#known-limitations)
- [Running the tests](#running-the-tests)
- [License](#license)

---

## Requirements

- **Python 3.8 or newer**: check with `python3 --version` (on Windows: `py --version`)
- An internet connection to reach ESOUI

Nothing else. There is no `pip install` step.

## Quick start

```sh
git clone https://github.com/aonury/eso_addon_manager.git
cd eso_addon_manager
python3 eso_addons.py web
```

It prints an address like `http://127.0.0.1:23176/`. Open it in your browser. Press `Ctrl+C` in the terminal to stop the server.

On Windows, use `py` instead of `python3`:

```sh
py eso_addons.py web
```

Close the game before installing or updating addons. ESO only reads addons when it loads, and a running game may hold files open.

## Finding your AddOns folder

The manager looks for the AddOns folder in this order:

1. The `ESO_ADDONS_DIR` environment variable, if set
2. A folder you saved with `config --dir`
3. The usual install locations:
   - `~/Documents/Elder Scrolls Online/live/AddOns` (Windows and macOS)
   - `~/OneDrive/Documents/Elder Scrolls Online/live/AddOns` (Windows with OneDrive)
   - Steam + Proton on Linux, for native Steam (`~/.steam/steam`, `~/.local/share/Steam`) and Flatpak Steam (`~/.var/app/com.valvesoftware.Steam/...`)

See which folder it found:

```sh
python3 eso_addons.py config
```

If it found the wrong one, or none at all, set it once and it is remembered:

```sh
python3 eso_addons.py config --dir "/path/to/Elder Scrolls Online/live/AddOns"
```

For a one-off run against a different folder (the PTS, for example):

```sh
ESO_ADDONS_DIR="/path/to/Elder Scrolls Online/pts/AddOns" python3 eso_addons.py list
```

## The web page

```sh
python3 eso_addons.py web              # http://127.0.0.1:23176/
python3 eso_addons.py web --port 8123  # a port of your choice
```

The default port is 23176. If something else already uses it, the server picks a free port and prints that address instead. A port you choose with `--port` is never swapped: if it's taken, you get an error.

The server only listens on your own computer (`127.0.0.1`). Nobody else on your network can reach it.

### Installed tab

Lists every addon in your AddOns folder, with the version you have and the version on ESOUI.

- **Check now**: downloads the latest ESOUI catalog right away and refreshes the list. Next to it you see when the last check happened ("Checked 12 min ago"). Without it, the catalog refreshes on its own once an hour, when you load the page.
- **Update all**: the bar at the top shows how many updates are available. One click updates them all, with a progress bar.
- **Update** / **Remove**: per addon. Remove asks first and warns you when other addons depend on the one you're removing.
- **Filter**: type to narrow the list by name, folder or included sub-addon.
- **All / Updates**: show everything, or only addons with an update.
- **Show libraries**: libraries (LibAddonMenu, LibAsync, …) are hidden by default to keep the list short. Libraries with an update are always shown.
- **Dependencies**: addons that need other addons have an arrow (▸) in front of their name. Click the row or the arrow to see their dependencies underneath, each marked **Installed** or **Not installed**.
- **Includes**: some addons ship several addons in one folder (HarvestMap ships its zone modules, RaidNotifier ships LibCSA). These are listed under the addon's name and count as part of it.
- **Names are links**: click an addon's name to open its ESOUI page.

### Find on ESOUI tab

Search the whole ESOUI catalog and install with one click.

- **Search box**: matches the addon name and its folder names. Every word must match, in any order, so `lib async` finds *LibAsync*. Results are sorted by downloads this month.
- **Empty search**: shows the 30 most downloaded addons this month, a good place to start browsing.
- **Categories**: the drop-down lists every ESOUI category (Action Bar Mods, Bags/Bank/Inventory, Combat Mods, Libraries, Map/Coords/Compasses, and so on), with the number of addons in each. Pick one to list **every** addon in it, most downloaded first. Type in the search box to narrow the results within the category.
- **Install**: downloads the addon and any missing dependencies. Addons you already have show **Installed** instead.

## Command line

Everything the web page does is also available as a command:

```
python3 eso_addons.py <command> [options]
```

| Command | What it does |
|---|---|
| `config [--dir PATH]` | Show the AddOns folder, or set it |
| `list [--libs-only]` | List installed addons: folder, version, title |
| `search TERMS... [--limit N]` | Search the ESOUI catalog (default 20 results) |
| `outdated [--unmatched]` | List addons with a newer version on ESOUI |
| `install TARGET...` | Install or update by ESOUI ID or folder name |
| `remove FOLDER... [--force]` | Move addon folders to the backup folder |
| `web [--port N]` | Start the local web page (default port 23176) |

Run `python3 eso_addons.py <command> --help` for details on any command.

### Examples

Search for an addon. The first column is the ESOUI ID, then the latest version, the name and its monthly downloads:

```sh
$ python3 eso_addons.py search harvest map --limit 2
   57  3.16.12        HarvestMap  (monthly 15809)
 3034  3.15.3         HarvestMap-Data  (monthly 4093)

6 results
```

Install it, by ID or by folder name. Missing dependencies come along automatically:

```sh
python3 eso_addons.py install 57
python3 eso_addons.py install HarvestMap
python3 eso_addons.py install 57 2651 1360     # several at once
```

See what's out of date, then update it:

```sh
$ python3 eso_addons.py outdated
 2651  displayleads                 42.0             -> 42.1

1 outdated, 56 matched, 0 not on ESOUI

$ python3 eso_addons.py install 2651
```

Update everything that is out of date (Linux/macOS shell):

```sh
python3 eso_addons.py outdated 2>/dev/null | awk '{print $1}' | xargs -r python3 eso_addons.py install
```

`outdated --unmatched` also lists folders that aren't on ESOUI (addons you installed by hand from elsewhere, or your own). The manager can't update those.

Remove an addon. It refuses if another installed addon depends on it, unless you add `--force`:

```sh
$ python3 eso_addons.py remove LibAsync
LibAsync is required by Inventory Insight, Potion Maker (for Alchemy Crafting), Wizard's Wardrobe; use --force to remove it anyway

$ python3 eso_addons.py remove MapPins
removed MapPins (backup: .eso-addons-backup/20261011-101500/MapPins)
```

## How it works

1. **Reading installed addons.** Each folder in AddOns is one addon. Its manifest (`<Folder>/<Folder>.txt` or `.addon`) gives the title, version, library flag and `DependsOn` list. ESO also loads addons nested inside a folder (for example `HarvestMap/Modules/HarvestMap`), so those are read too and folded into their parent.
2. **Matching with ESOUI.** The ESOUI catalog lists the folders each addon ships. Installed folders are matched to catalog entries by folder name.
3. **Comparing versions.** Versions are compared by their numbers only, so `1.0 r47` and `1.0.47` are equal and `1.99` is older than `1.100`.
4. **Installing.** The manager downloads the zip, checks it, moves any folder it would replace into the backup folder, extracts the new one, then installs any dependency that isn't there yet.
5. **Remembering what was installed.** Some authors forget to update the version inside their zip. ESOUI says `42.1`, but the file still says `42.0`, so the addon would look outdated forever. The manager writes the ESOUI version to a small `.eso-addons-version` file in each folder it installs and trusts whichever version is newer.

The ESOUI catalog is several MB, so it is cached for one hour: reloading the page re-reads your AddOns folder right away, but new releases on ESOUI show up within the hour. **Check now** on the web page skips the wait. A catalog less than a minute old is reused, so repeated clicks don't re-download it. If ESOUI can't be reached, the last cached copy is used and the page tells you so. There is no background check while the page is open.

## Safety

Addons are code that runs inside your game, downloaded from third parties. The manager tries to keep that safe:

- **Nothing is deleted.** Updated and removed folders move to `AddOns/.eso-addons-backup/<date-time>/`. To undo, move a folder back. Delete old backups yourself when you no longer need them.
- **Downloads are checked.** HTTPS only, and the file's MD5 checksum must match what ESOUI reports.
- **Zips can't escape.** A zip that tries to write outside the AddOns folder, or into the backup folder, is refused before anything is changed.
- **The web server is local only.** It listens on `127.0.0.1`, accepts requests only when addressed to `localhost`, and every change needs a random token that is new on each start. This stops other websites you visit from installing or removing addons through it. The page also refuses to be embedded in other sites.

## Files it creates

| Path | What |
|---|---|
| `~/.config/eso-addons/config.json` | Saved AddOns folder (only after `config --dir`) |
| `~/.cache/eso-addons/filelist.json` | ESOUI catalog cache, refreshed hourly |
| `AddOns/.eso-addons-backup/` | Previous versions of updated and removed addons |
| `AddOns/<Addon>/.eso-addons-version` | ESOUI version of what was installed; ESO ignores it |

## Known limitations

- **Multi-folder addons**: when one ESOUI download ships several top-level folders, the version is read from the first one, and `remove` needs each folder named.
- **Same folder, several ESOUI entries**: when more than one ESOUI download ships the same folder name, the most downloaded one is assumed.
- **Version words are ignored**: `beta`, `rc` and similar are not compared, only the numbers.
- **Addons not on ESOUI** are listed but can't be updated.
- **Unreadable versions**: an addon whose manifest has no version, and that wasn't installed by this tool, shows "Version not readable". Install it once through the manager and its version is known from then on.

## Running the tests

```sh
python3 test_eso_addons.py
```

It prints a few install and remove lines. A traceback means a test failed. The tests use a fake network and temporary folders, and never touch your real AddOns folder.

## License

[MIT](LICENSE)
