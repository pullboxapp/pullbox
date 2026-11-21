"""CBT conversion preserves source bytes and publishes only complete safe copies."""

import io
import tarfile
import zipfile

import pytest

from pullbox.core.exceptions import JobCancelledError
from pullbox.utilities.executors.file_converter import convert_file


def make_cbt(path, extra=None):
    members = {
        "pages/001.jpg": b"page one" * 200000,
        "pages/002.jpg": b"page two",
        "ComicInfo.xml": b"<ComicInfo><Number>3</Number></ComicInfo>",
        "MetronInfo.xml": b"<MetronInfo><Number>3</Number></MetronInfo>",
    }
    with tarfile.open(path, "w") as archive:
        directory = tarfile.TarInfo("pages")
        directory.type = tarfile.DIRTYPE
        archive.addfile(directory)
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
        if extra is not None:
            archive.addfile(extra)
    return members


async def test_cbt_preserves_pages_xml_and_reports_progress(tmp_path):
    source = tmp_path / "source.cbt"
    members = make_cbt(source)
    original = source.read_bytes()
    progress = []
    target = await convert_file(
        source, "cbz", progress_callback=lambda *args: progress.append(args)
    )
    with zipfile.ZipFile(target) as archive:
        assert archive.testzip() is None
        assert {name: archive.read(name) for name in archive.namelist()} == members
    assert source.read_bytes() == original
    assert progress[-1] == ("packing", len(members), len(members), "entries")
    assert set(tmp_path.iterdir()) == {source, target}


@pytest.mark.parametrize(
    "name,kind",
    [
        ("../escape.jpg", tarfile.REGTYPE),
        ("/absolute.jpg", tarfile.REGTYPE),
        ("..\\escape.jpg", tarfile.REGTYPE),
        ("pages/001.jpg", tarfile.REGTYPE),
        ("pages/001.JPG", tarfile.REGTYPE),
        ("./pages/001.jpg", tarfile.REGTYPE),
        ("link.jpg", tarfile.SYMTYPE),
        ("hardlink.jpg", tarfile.LNKTYPE),
        ("pipe", tarfile.FIFOTYPE),
        ("device", tarfile.CHRTYPE),
    ],
)
async def test_cbt_rejects_unsafe_or_ambiguous_entries(tmp_path, name, kind):
    source = tmp_path / "source.cbt"
    extra = tarfile.TarInfo(name)
    extra.type = kind
    extra.linkname = "pages/001.jpg" if kind in {tarfile.SYMTYPE, tarfile.LNKTYPE} else ""
    make_cbt(source, extra)
    original = source.read_bytes()
    with pytest.raises(ValueError, match=r"unsafe|duplicate|regular"):
        await convert_file(source, "cbz")
    assert source.read_bytes() == original
    assert list(tmp_path.iterdir()) == [source]


async def test_cbt_cancel_leaves_no_partial_copy(tmp_path):
    source = tmp_path / "source.cbt"
    make_cbt(source)
    original = source.read_bytes()

    def cancel(*_args):
        raise JobCancelledError("cancel")

    with pytest.raises(JobCancelledError):
        await convert_file(source, "cbz", progress_callback=cancel)
    assert source.read_bytes() == original
    assert list(tmp_path.iterdir()) == [source]


async def test_cbt_destination_collision_during_conversion_preserves_both(tmp_path):
    source = tmp_path / "source.cbt"
    make_cbt(source)
    original = source.read_bytes()
    target = source.with_suffix(".cbz")

    def create_competing_file(*_args):
        target.write_bytes(b"existing library file")

    with pytest.raises(FileExistsError):
        await convert_file(source, "cbz", progress_callback=create_competing_file)
    assert target.read_bytes() == b"existing library file"
    assert source.read_bytes() == original
    assert set(tmp_path.iterdir()) == {source, target}


async def test_truncated_cbt_never_publishes(tmp_path):
    source = tmp_path / "source.cbt"
    make_cbt(source)
    source.write_bytes(source.read_bytes()[:4096])
    original = source.read_bytes()
    with pytest.raises((tarfile.TarError, ValueError), match=r"unexpected|truncated|incomplete"):
        await convert_file(source, "cbz")
    assert source.read_bytes() == original
    assert list(tmp_path.iterdir()) == [source]
