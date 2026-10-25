# Contributing to Zir SSH

Thanks for taking the time to help! Bug reports, ideas, translations and pull requests are all welcome.
Issues and pull requests can be written in English or Ukrainian.

## Reporting a bug

Open an [issue](https://github.com/LavrinchukAndrii/zir-ssh/issues/new/choose) and include:

- `zir --version`, your OS and terminal (macOS Terminal, iTerm2, Windows Terminal, GNOME Terminal…);
- the server side: distribution, web server, database (what the problem is about);
- what you did, what you expected and what happened; a screenshot helps a lot.

Please **remove host names, IP addresses, user names and log lines** that you do not want to make
public. Security problems go to [SECURITY.md](SECURITY.md), not to public issues.

## Development setup

```sh
git clone https://github.com/LavrinchukAndrii/zir-ssh.git
cd zir-ssh
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/zir --demo          # the demo fleet needs no servers
make test                     # or: .venv/bin/pytest -q
```

For a real Linux box with nginx, PHP-FPM and MariaDB there is a Docker test server, see
[docs/BUILD.md](docs/BUILD.md#тестовий-сервер-у-docker).

## Pull requests

1. Fork the repository and create a branch from `main`.
2. Keep the change focused: one fix or feature per pull request.
3. Add or update tests in `tests/` for the behaviour you change; `make test` must pass.
4. Match the style of the surrounding code (type hints, small functions, short comments that explain *why*).
5. Update `README.md` / `README.uk.md` and `CHANGELOG.md` (section *Unreleased*) if users will notice the change.
6. If the UI changed visibly, regenerate the screenshots: `python scripts/screenshots.py`.

### Ground rules for the code

- **Monitoring stays read-only.** Probes must not change anything on a server. Anything that writes
  (like installing a key or `~/.my.cnf`) must be an explicit user action with a confirmation.
- **No new network endpoints.** Zir SSH talks only to the user's servers over their own `ssh`.
- **Every UI string is translated.** Wrap text in `_()` / `_n()` from `zir/i18n.py` and add the
  Ukrainian translation to `zir/locale_uk.py`; `tests/test_i18n.py` fails on a missing one.

## License

Zir SSH is distributed under the [PolyForm Noncommercial License 1.0.0](LICENSE). By submitting a
contribution you agree that it is licensed under the same terms, and you grant the author
(Lavrinchuk Andrii) the right to also license your contribution under other terms, for example a
separate commercial license.
