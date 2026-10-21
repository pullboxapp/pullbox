"""The credit-role overlap between ComicInfo and richer canonical metadata."""

from xml.etree import ElementTree as ET

from pullbox.schemas.metadata_credits import MetadataCredit, parse_credits

COMICINFO_ROLES = {
    "Writer": "writer",
    "Penciller": "penciller",
    "Inker": "inker",
    "Colorist": "colorist",
    "Letterer": "letterer",
    "CoverArtist": "cover",
    "Editor": "editor",
    "Translator": "translator",
}

ARCHIVE_FORMATS = {
    "standard": "Single Issue",
    "tpb": "Trade Paperback",
    "one_shot": "One-Shot",
    "annual": "Annual",
    "hardcover": "Hardcover",
    "omnibus": "Omnibus",
    "graphic_novel": "Graphic Novel",
}
COMICINFO_ONLY_FORMATS = frozenset({"special", "compendium", "deluxe", "volume"})


def normalize_comicinfo_aliases(root: ET.Element) -> None:
    """Normalize only known aliases in a parsed copy; retain duplicates for rejection."""
    aliases = {"Issue": "Number", "writer": "Writer"}
    for node in root:
        if isinstance(node.tag, str) and node.tag in aliases:
            node.tag = aliases[node.tag]


def archive_format_label(value: str | None) -> str | None:
    if value in COMICINFO_ONLY_FORMATS:
        return value.title()
    return ARCHIVE_FORMATS.get(value, value) if value is not None else None


def canonical_archive_format(value: str) -> str:
    """Normalize only the exact reversible labels our writer emits."""
    for canonical in (*ARCHIVE_FORMATS, *COMICINFO_ONLY_FORMATS):
        if archive_format_label(canonical) == value:
            return canonical
    return value


def comicinfo_credits(credits: tuple[MetadataCredit, ...]) -> tuple[MetadataCredit, ...]:
    """Project supported roles, without losing rich roles in the canonical record."""
    supported = set(COMICINFO_ROLES.values())
    return parse_credits(
        tuple(
            {"name": credit.name, "role": ", ".join(roles)}
            for credit in credits
            if (roles := [role for role in credit.role.split(", ") if role in supported])
        )
    )
