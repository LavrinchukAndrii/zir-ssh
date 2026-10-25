"""Set up read access to MySQL / MariaDB: write ~/.my.cnf on the server.

Only after the user typed the database user and password in zir. The password
travels inside the SSH session (never on a command line, never stored locally),
is checked with `SELECT 1` first, and lands in ~/.my.cnf with mode 0600. An
existing ~/.my.cnf is kept as ~/.my.cnf.zir-backup-<time>.
"""

from __future__ import annotations

from .i18n import _


def _sh(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


def setup_script(user: str, password: str) -> str:
    return f"""
umask 077
M=$(command -v mariadb || command -v mysql)
[ -n "$M" ] || {{ echo "__noclient"; exit 0; }}
DB_USER={_sh(user)}
DB_PASS={_sh(password)}
if ! MYSQL_PWD="$DB_PASS" "$M" --no-defaults -u"$DB_USER" --connect-timeout=5 -N -B -e "SELECT 1" >/dev/null 2>&1; then
    echo "__bad"; exit 0
fi
backup=""
if [ -e "$HOME/.my.cnf" ]; then
    backup="$HOME/.my.cnf.zir-backup-$(date +%Y%m%d-%H%M%S)"
    cp -p "$HOME/.my.cnf" "$backup"
fi
esc=$(printf '%s' "$DB_PASS" | sed 's/\\\\/\\\\\\\\/g; s/"/\\\\"/g')
printf '[client]\\nuser=%s\\npassword="%s"\\n' "$DB_USER" "$esc" > "$HOME/.my.cnf"
chmod 600 "$HOME/.my.cnf"
if env -u MYSQL_PWD "$M" --connect-timeout=5 -N -B -e "SELECT 1" >/dev/null 2>&1; then
    echo "__ok $backup"
else
    echo "__written_but_failing $backup"
fi
"""


class DbAccessError(Exception):
    pass


async def setup_access(collector, user: str, password: str) -> str:
    """Returns a message for the user; raises DbAccessError when nothing was changed."""
    output = (await collector.run_script(setup_script(user, password))).strip().splitlines()
    status = output[-1] if output else ""
    if status == "__noclient":
        raise DbAccessError(_("The mysql client is not installed on the server."))
    if status == "__bad":
        raise DbAccessError(_("Wrong user or password: the database refused them, nothing was changed."))
    word, _sep, backup = status.partition(" ")
    if word == "__ok":
        message = _("Database access saved to ~/.my.cnf on the server.")
    elif word == "__written_but_failing":
        message = _("~/.my.cnf was written, but the client still cannot log in with it; check it on the server.")
    else:
        raise DbAccessError(_("Unexpected answer from the server: {text}", text=" ".join(output)[-200:]))
    if backup:
        message += " " + _("The old file is kept as {path}.", path=backup)
    return message
