# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

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

[Unreleased]: https://github.com/LavrinchukAndrii/zir-ssh/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/LavrinchukAndrii/zir-ssh/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/LavrinchukAndrii/zir-ssh/releases/tag/v0.1.0
