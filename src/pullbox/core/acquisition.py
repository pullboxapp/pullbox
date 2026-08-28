"""Shared acquisition protocol primitives."""

import enum


class AcquisitionProtocol(enum.StrEnum):
    """The protocol used to acquire a release."""

    USENET = "usenet"
    TORRENT = "torrent"
    DIRECT = "direct"
    DC = "dc"
