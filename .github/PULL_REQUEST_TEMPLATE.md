## What and why

<!-- What does this change and which problem does it solve? Link the issue: Fixes #123 -->

## How it was tested

<!-- Tests added or updated, manual checks (zir --demo, a real server, which OS). -->

## Checklist

- [ ] `make test` passes
- [ ] Monitoring stays read-only; anything that writes to a server is an explicit user action
- [ ] New UI strings are wrapped in `_()` and translated in `zir/locale_uk.py`
- [ ] README / CHANGELOG updated if users will notice the change
- [ ] Screenshots regenerated (`python scripts/screenshots.py`) if the UI changed visibly
