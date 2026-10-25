"""Ctrl+K / Ctrl+T: fuzzy host switcher on top of Textual's command palette."""

from __future__ import annotations

from typing import Callable

from textual.command import DiscoveryHit, Hit, Hits, Provider

from ..config import HostSpec


def hosts_provider(
    specs: list[HostSpec],
    on_pick: Callable[[str], None],
    hint: Callable[[str], str],
    titles: dict[str, str] | None = None,
) -> type[Provider]:
    """Provider class matching host name, tags and SSH target."""

    titles = titles or {}

    class HostsProvider(Provider):
        async def discover(self) -> Hits:
            for spec in specs:
                yield DiscoveryHit(
                    titles.get(spec.name, spec.name), lambda name=spec.name: on_pick(name), help=hint(spec.name)
                )

        async def search(self, query: str) -> Hits:
            matcher = self.matcher(query)
            for spec in specs:
                shown = titles.get(spec.name, spec.name)
                haystack = " ".join((shown, spec.name, *spec.tags, spec.target))
                score = max(matcher.match(shown), matcher.match(spec.name), matcher.match(haystack) * 0.8)
                if score > 0:
                    yield Hit(
                        score,
                        matcher.highlight(shown),
                        lambda name=spec.name: on_pick(name),
                        text=spec.name,
                        help=hint(spec.name),
                    )

    return HostsProvider
