"""Descriptive edition references, never provider IDs or URLs to fetch."""

import re
from typing import Annotated
from urllib.parse import parse_qsl, urlsplit

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, TypeAdapter, field_validator

from pullbox.core.comicinfo_sanitizer import references_stale_retailer
from pullbox.core.comicinfo_text import descriptive_text

_PROVIDER_HOSTS = (
    "comicvine.gamespot.com",
    "comicvine.com",
    "metron.cloud",
    "comics.org",
    "leagueofcomicgeeks.com",
    "leagueofcomicgeeks.co.uk",
)


def public_reference(url: str) -> None:
    if (
        not 0 < len(url) <= 2048
        or not url.isprintable()
        or re.search(r'[\s\\<>"{}|^`]', url)
        or re.search(r"%(?![A-Fa-f0-9]{2})", url)
    ):
        raise ValueError("Enter an HTTP or HTTPS URL; encode spaces as %20.")
    try:
        parts = urlsplit(url)
        valid = (
            parts.scheme in ("http", "https")
            and parts.hostname
            and parts.username is None
            and parts.password is None
            and parts.port != 0
        )
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("Enter a web URL without credentials.")
    for component in (parts.query, parts.fragment):
        for key, _ in parse_qsl(component, keep_blank_values=True):
            if re.search(
                r"token|passw|secret|signature|credential|authoriz|api[-_]?key|^sig$", key, re.I
            ):
                raise ValueError("A URL appears to contain private access information.")


def protected_reference(url: str) -> bool:
    try:
        host = (urlsplit(url).hostname or "").rstrip(".").lower()
        descriptive_text(url)
    except ValueError:
        return True
    return any(host == h or host.endswith("." + h) for h in _PROVIDER_HOSTS)


class IssueWebLink(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    url: str = Field(strict=True, min_length=1, max_length=2048)
    primary: bool = Field(default=False, strict=True)

    @field_validator("url")
    @classmethod
    def descriptive_reference(cls, url: str) -> str:
        public_reference(url)
        if protected_reference(url) or references_stale_retailer(url):
            raise ValueError(
                "Provider identity and legacy retailer URLs are not descriptive links."
            )
        return url

    def label(self) -> str:
        return self.url + (" (primary)" if self.primary else "")


def _distinct(rows: tuple[IssueWebLink, ...]) -> tuple[IssueWebLink, ...]:
    if len({r.url for r in rows}) != len(rows) or sum(r.primary for r in rows) > 1:
        raise ValueError("Enter each reference once and choose at most one primary link.")
    if len(" ".join(r.url for r in rows)) > 4096:
        raise ValueError("The combined reference links exceed the ComicInfo field limit.")
    return rows


type IssueWebLinks = Annotated[
    tuple[IssueWebLink, ...], Field(min_length=1, max_length=128), AfterValidator(_distinct)
]
LINKS: TypeAdapter[tuple[IssueWebLink, ...]] = TypeAdapter(IssueWebLinks)
