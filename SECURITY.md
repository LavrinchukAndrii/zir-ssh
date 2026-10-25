# Security policy

Zir SSH handles SSH sessions, server output and saved passwords, so security reports are taken seriously.

## Supported versions

Only the latest release receives security fixes.

## Reporting a vulnerability

**Please do not open a public issue.** Report it privately instead:

- through [GitHub private vulnerability reporting](https://github.com/LavrinchukAndrii/zir-ssh/security/advisories/new), or
- by email to **lavrinchuk.a@gmail.com** with "Zir SSH security" in the subject.

Include the version (`zir --version`), your OS, steps to reproduce and the impact you see. You will get
an answer within a few days; once a fix is released, you will be credited in the changelog unless you
prefer otherwise.

## Scope

Things that are especially interesting:

- a probe or feature that changes a server without an explicit user action;
- leaking passwords, identity cards (`credentials.enc`) or key material;
- server output (logs, process names, file names) that can inject commands or escape sequences
  into the local terminal or the UI;
- weaknesses in how identity cards are encrypted.
