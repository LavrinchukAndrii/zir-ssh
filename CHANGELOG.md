# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.3.1] - 2026-11-18

### Fixed

- The 0.3.0 downloads did not start ("No module named 'tarfile'"): the single-file build left out
  modules the new Files section needs (tar for copying, the text area of the viewer). Every build now
  starts the UI without a screen (`ZIR_SELF_TEST=1`) before it is published.

## [0.3.0] - 2026-11-18 [YANKED]

Withdrawn: the downloads did not start (fixed in 0.3.1, which has everything listed here).

### Added

- **Files** section next to Terminal: a two-panel file manager like mc, this computer on the left and
  the server on the right. Copy (F5) and move (F6) files and folders with everything inside, both
  ways; for files that already exist it asks: overwrite, skip, overwrite all, skip all or overwrite
  only older ones. A progress window shows the current file, speed and remaining time and can be
  cancelled. View (F3), edit (F4, Ctrl+S saves), rename (F2), new folder (F7), delete (F8, asks
  first), mark with Ins / Space, `+` / `-` by mask, `*` to invert, type to jump to a name, sort by a
  column click, Ctrl+G to go to a path. Windows shows the drives above `C:\`.
- **F9 Run as** in the Files section: the server panel works as another user through `su -`, with the
  password of an identity card (for example root); its login is the user name. The panel header
  shows who you are, in red for root.

## [0.2.1] - 2026-11-17

### Fixed

- Windows: the built-in terminal (SSH sessions and the Local tab) now works, through ConPTY; Local opens PowerShell.
- Windows classic console: buttons, inputs, lists and icons no longer show "?" boxes; symbols the console
  font lacks are replaced with ones it has (Windows Terminal keeps the originals).

## [0.2.0] - 2026-11-17

### Added

- **My snippets**: your own commands for all servers or for one server; create, edit, delete and reorder them.
- **Ctrl+]** in the terminal opens the Zir SSH menu: insert a snippet or a password, new / close session,
  switch sections and server tabs.

### Changed

- While the terminal has focus, every key goes to the shell, mc or vim: F3/F4/F6, Alt+letters, Alt+digits,
  Ctrl+N, Ctrl+Q, Ctrl+PgUp/PgDn no longer switch tabs or sections there.

### Fixed

- The terminal now sends F3, F4, Shift+F1..F12, Ctrl+\\ and modified cursor keys, so mc's View, Edit and
  other commands work.

## [0.1.0] - 2026-10-25

First public release.

### Added

- Tabs per server, a **Fleet** table of all servers and a **Local** terminal tab.
- Sections: Overview, Web, Database, PHP, Attacks, Processes, Disks, Network, Services, Logs, Terminal, Snippets.
- Built-in SSH terminal sessions that reuse the monitoring connection.
- Snippets prepared for the software found on the server.
- Encrypted identity cards (logins and passwords) for terminal password prompts.
- Adding your SSH key to password-only servers; optional database access via `~/.my.cnf`.
- Ukrainian and English interface.
- `--report` / `--report --json` one-shot reports; `--demo` mode without any servers.
- Builds for macOS on Apple Silicon (`.app` in a `.dmg`), Linux (x86_64, arm64) and Windows.

[Unreleased]: https://github.com/LavrinchukAndrii/zir-ssh/compare/v0.2.1...HEAD
[0.2.1]: https://github.com/LavrinchukAndrii/zir-ssh/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/LavrinchukAndrii/zir-ssh/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/LavrinchukAndrii/zir-ssh/releases/tag/v0.1.0
