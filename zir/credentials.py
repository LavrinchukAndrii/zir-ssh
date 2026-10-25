"""Identity cards (name, login, password) for the built-in terminal.

Stored in config_dir()/credentials.enc, encrypted with AES-256-GCM. The key is
derived (HKDF-SHA256) from the bytes of this computer's private SSH key file,
so the vault opens by itself on start and cannot be read on another machine.
The file holds a short id of the key it was made with: a different key gives
a clear error instead of a silent "wrong password".

File: MAGIC | salt (16) | key id (8) | nonce (12) | AES-GCM(JSON list of cards)
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .i18n import _
from .keysetup import DEFAULT_KEYS, ssh_dir
from .paths import config_dir

# MAGIC and the HKDF labels keep the old name (sdeck) on purpose: they are part
# of the key and the file format, so changing them would lock out saved cards.
MAGIC = b"SDCK1"
SALT, KEY_ID, NONCE = 16, 8, 12


class VaultError(Exception):
    pass


@dataclass
class Credential:
    name: str
    login: str = ""
    password: str = ""
    id: str = field(default_factory=lambda: uuid.uuid4().hex)


def vault_path() -> Path:
    return config_dir() / "credentials.enc"


def private_keys() -> list[Path]:
    return [p for p in (ssh_dir() / name for name in DEFAULT_KEYS) if p.is_file()]


def _hkdf(material: bytes, salt: bytes, info: bytes) -> bytes:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    return HKDF(algorithm=hashes.SHA256(), length=32, salt=salt, info=info).derive(material)


def _key_id(material: bytes) -> bytes:
    return _hkdf(material, b"", b"sdeck-vault-key-id")[:KEY_ID]


def _read_keys(keys: list[Path] | None) -> list[bytes]:
    out = []
    for path in private_keys() if keys is None else keys:
        try:
            out.append(path.read_bytes())
        except OSError:
            continue
    return out


def load(path: Path | None = None, keys: list[Path] | None = None) -> list[Credential]:
    path = path or vault_path()
    try:
        blob = path.read_bytes()
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise VaultError(_("Cannot read {path}: {error}", path=path, error=exc.strerror)) from None
    head = len(MAGIC) + SALT + KEY_ID + NONCE
    if not blob.startswith(MAGIC) or len(blob) <= head:
        raise VaultError(_("{path} is damaged", path=path))
    salt = blob[len(MAGIC):len(MAGIC) + SALT]
    key_id = blob[len(MAGIC) + SALT:len(MAGIC) + SALT + KEY_ID]
    nonce = blob[head - NONCE:head]
    materials = _read_keys(keys)
    if not materials:
        raise VaultError(_("No private SSH key in ~/.ssh: the identity cards cannot be decrypted"))
    material = next((m for m in materials if _key_id(m) == key_id), None)
    if material is None:
        raise VaultError(_("The identity cards were encrypted with another SSH key (not on this computer)"))

    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    try:
        plain = AESGCM(_hkdf(material, salt, b"sdeck-vault")).decrypt(nonce, blob[head:], MAGIC)
        items = json.loads(plain)
    except (InvalidTag, ValueError):
        raise VaultError(_("{path} is damaged", path=path)) from None
    return [Credential(**{k: str(item.get(k, "")) for k in ("name", "login", "password", "id")})
            for item in items if isinstance(item, dict)]


def save(cards: list[Credential], path: Path | None = None, keys: list[Path] | None = None) -> None:
    path = path or vault_path()
    materials = _read_keys(keys)
    if not materials:
        raise VaultError(_("No private SSH key in ~/.ssh to encrypt the identity cards"))
    material = materials[0]

    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    salt, nonce = os.urandom(SALT), os.urandom(NONCE)
    plain = json.dumps([asdict(c) for c in cards], ensure_ascii=False).encode()
    blob = MAGIC + salt + _key_id(material) + nonce + AESGCM(_hkdf(material, salt, b"sdeck-vault")).encrypt(nonce, plain, MAGIC)
    tmp = path.with_suffix(".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(blob)
        os.replace(tmp, path)
    except OSError as exc:
        raise VaultError(_("Cannot save {path}: {error}", path=path, error=exc.strerror)) from None


class Vault:
    """The cards loaded once per run, shared by the picker and the manager."""

    def __init__(self, path: Path | None = None, keys: list[Path] | None = None) -> None:
        self.path = path
        self.keys = keys
        self.cards: list[Credential] = []
        self.error = ""
        self.loaded = False

    def ensure_loaded(self) -> None:
        if self.loaded:
            return
        try:
            self.cards = load(self.path, self.keys)
            self.error = ""
            self.loaded = True
        except VaultError as exc:
            self.error = str(exc)

    def save(self) -> None:
        if not self.loaded and (self.path or vault_path()).exists():
            raise VaultError(self.error)  # never overwrite cards we could not read
        save(self.cards, self.path, self.keys)
        self.error = ""
        self.loaded = True
