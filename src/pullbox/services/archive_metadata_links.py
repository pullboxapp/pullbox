"""Edit descriptive URLs without moving identity evidence between documents."""

import re
from dataclasses import dataclass
from xml.etree import ElementTree as ET

from pydantic import ValidationError

from pullbox.core.comicinfo_sanitizer import references_stale_retailer
from pullbox.core.source_metadata import import_web_ids
from pullbox.schemas.metadata_issue_links import (
    LINKS,
    IssueWebLink,
    protected_reference,
    public_reference,
)
from pullbox.services.archive_metadata_preservation import set_text


@dataclass(frozen=True)
class DocumentLinks:
    comicinfo: tuple[IssueWebLink, ...]
    metron: tuple[IssueWebLink, ...]
    pinned_ci: tuple[str, ...]
    primary_locked: bool
    original_web: str

    @classmethod
    def read(cls, ci: ET.Element, mi: ET.Element) -> "DocumentLinks":
        web = ci.findtext("Web", "")
        if len(web) > 4096 or references_stale_retailer(web):
            raise ValueError("This ComicInfo Web field contains legacy or oversized content.")
        ci_urls = web.split()
        for url in ci_urls:
            public_reference(url)
        pinned_ci = tuple(u for u in ci_urls if protected_reference(u))
        container = mi.find("URLs")
        rows: list[IssueWebLink] = []
        primary_locked = False
        try:
            for node in container if container is not None else ():
                if node.tag != "URL" or len(node) or set(node.attrib) - {"primary"}:
                    raise ValueError("The Metron URL list contains an XML annotation.")
                url = (node.text or "").strip()
                public_reference(url)
                primary = node.get("primary", "").strip() in {"true", "1"}
                if protected_reference(url):
                    primary_locked |= primary
                else:
                    rows.append(IssueWebLink(url=url, primary=primary))
            metron = LINKS.validate_python(rows) if rows else ()
            primary_url = next((r.url for r in metron if r.primary), None)
            ci_rows = [
                IssueWebLink(url=u, primary=u == primary_url) for u in ci_urls if u not in pinned_ci
            ]
            comicinfo = LINKS.validate_python(ci_rows) if ci_rows else ()
        except ValidationError:
            raise ValueError(
                "The existing web links cannot be safely separated. Leave them unchanged."
            ) from None
        return cls(comicinfo, metron, pinned_ci, primary_locked, web)

    def checked_web(self, desired: tuple[IssueWebLink, ...]) -> str:
        if self.primary_locked and any(r.primary for r in desired):
            raise ValueError(
                "The primary Metron URL is protected. Leave the reference links non-primary."
            )
        if [r.url for r in self.comicinfo] == [r.url for r in desired]:
            return self.original_web
        # Keep existing separators: a newline can delimit the legacy import reader's match.
        urls = iter(r.url for r in desired)
        existing = re.sub(
            r"\S+", lambda m: m[0] if m[0] in self.pinned_ci else next(urls, ""), self.original_web
        ).strip()
        result = " ".join(filter(None, (existing, *urls)))
        if len(result) > 4096:
            raise ValueError(
                "The references and protected URLs together exceed the ComicInfo field limit."
            )
        if import_web_ids(result) != import_web_ids(self.original_web):
            raise ValueError("Changing these links would change the importer's ComicVine match.")
        return result

    def apply(self, ci: ET.Element, mi: ET.Element, desired: tuple[IssueWebLink, ...]) -> None:
        web = self.checked_web(desired)
        if web != self.original_web:
            set_text(ci, "Web", web)
        if self.metron == desired:
            return
        parent = mi.find("URLs")
        if parent is None:
            parent = ET.SubElement(mi, "URLs")
        original = list(parent)
        existing = {
            (n.text or "").strip(): n
            for n in original
            if not protected_reference((n.text or "").strip())
        }
        replacements = []
        for row in desired:
            node = existing.get(row.url)
            if node is None:
                node = ET.Element("URL")
                node.text = row.url
            if (node.get("primary", "").strip() in {"true", "1"}) != row.primary:
                if row.primary:
                    node.set("primary", "true")
                else:
                    node.attrib.pop("primary", None)
            replacements.append(node)
        incoming = iter(replacements)
        slots = (
            next(incoming, None) if (n.text or "").strip() in existing else n for n in original
        )
        parent[:] = [n for n in (*slots, *incoming) if n is not None]
        if not len(parent):
            mi.remove(parent)
