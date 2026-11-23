"""Generated library and two operator actions for the disposable cutover rehearsal."""

import hashlib
import io
import json
import time
import zipfile
from pathlib import Path

from PIL import Image


def _comic(path, number):
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        for index, color in enumerate(("red", "blue"), 1):
            with io.BytesIO() as page:
                Image.new("RGB", (32, 48), color).save(page, "PNG")
                archive.writestr(f"page-{index}.png", page.getvalue())
        archive.writestr(
            "ComicInfo.xml",
            f"<ComicInfo><Series>Harbor Lights</Series><Number>{number}</Number></ComicInfo>",
        )
    return hashlib.sha256(path.read_bytes()).digest()


OWNED = Path("Harbor Lights (2024)/Harbor Lights 001 (2024).cbz")
INCOMING = "Harbor Lights 002 (2024).cbz"


def seed(db, comics, imports):
    db.execute("UPDATE system_config SET value='copy' WHERE key='post_processing_method'")
    db.executemany(
        "INSERT INTO series(id,title,sort_title,year_start,status,issue_count,monitored,"
        "library_root_id) VALUES(?,?,?,2024,'ENDED',16,false,77)",
        (
            (1000 + n, title, title.lower())
            for n in range(640)
            for title in ["Harbor Lights" if not n else f"Cutover Shelf {n}"]
        ),
    )
    db.executemany(
        "INSERT INTO issues(id,series_id,issue_number,issue_number_text,status,release_date) "
        "VALUES(?,?,?,?,?,'2024-01-01')",
        (
            (10000 + n, 1000 + n // 16, n % 16 + 1, str(n % 16 + 1), "SKIPPED" if n else "OWNED")
            for n in range(640 * 16)
        ),
    )
    db.execute("UPDATE series SET comicvine_id=91000 WHERE id=1000")
    db.execute("UPDATE issues SET comicvine_id=91001 WHERE id=10000")
    db.execute("UPDATE issues SET status='WANTED' WHERE id=10001")
    original = _comic(comics / OWNED, 1)
    incoming = _comic(imports / INCOMING, 2)
    stat = (comics / OWNED).stat()
    signature = json.dumps({"size_bytes": stat.st_size, "mtime_ns": stat.st_mtime_ns})
    path = f"/comics/{OWNED.as_posix()}"
    db.execute(
        "INSERT INTO library_files(id,library_root_id,issue_id,file_path,file_name,file_size,"
        "file_format,file_modified_at,match_confidence,has_comicinfo,"
        "source_signature,storage_mode) "
        "VALUES(102,77,10000,?,?,?,'CBZ',CURRENT_TIMESTAMP,'HIGH',true,?,'referenced')",
        (path, OWNED.name, stat.st_size, signature),
    )
    db.execute(
        "INSERT INTO import_jobs(id,source_path,source_type,status,scan_total_files,"
        "scan_total_dirs,"
        "series_found,series_duplicate,series_matched,series_no_match,series_new,series_imported,"
        "series_failed,search_on_add,cv_match_threshold,auto_accept_high_confidence,skip_no_match,"
        "source_layout_snapshot,future_root_policy_snapshot) "
        "VALUES(7,'/comics','MYLAR3','COMPLETED',2,1,1,0,1,0,1,1,0,false,0.8,false,false,?,?)",
        ('{"mode":"auto","preset":null}', "null"),
    )
    db.execute(
        "INSERT INTO import_series(id,import_job_id,status,raw_series_name,raw_year,series_id,"
        "cv_id,file_count,has_files) VALUES(19,7,'IMPORTED','Harbor Lights',2024,1000,91000,2,true)"
    )
    db.executemany(
        "INSERT INTO import_files(id,import_job_id,import_series_id,file_path,file_name,file_size,"
        "file_format,has_comicinfo,status,is_preferred,comicvine_issue_id,parsed_issue_number,"
        "parsed_series,source_signature) VALUES(?,7,19,?,?,?,'cbz',true,'NO_MATCH',false,?,?,"
        "'Harbor Lights',?)",
        [
            (200, path, OWNED.name, stat.st_size, 91001, 1, signature),
            (201, "/comics/unknown.cbz", "unknown.cbz", 32, None, None, "{}"),
        ],
    )
    # Prior decisions need no current filesystem access and must not be reopened by recheck.
    db.execute(
        "UPDATE import_jobs SET scan_total_files=6540, scan_total_dirs=641,series_found=641 "
        "WHERE id=3"
    )
    db.executemany(
        "INSERT INTO import_series(id,import_job_id,status,raw_series_name,series_id,"
        "file_count,has_files) VALUES(?,3,'SKIPPED',?,?,10,true)",
        ((3000 + n, f"Historical group {n}", 1000 + n) for n in range(640)),
    )
    db.executemany(
        "INSERT INTO import_files(id,import_job_id,import_series_id,file_path,file_name,file_size,"
        "file_format,has_comicinfo,status,is_preferred) VALUES(?,3,?,?,?,32,'cbz',false,"
        "'SKIPPED',false)",
        (
            (3000 + n, 3000 + n // 10, f"/fixture/{n // 10}/{n % 10 + 1}.cbz", f"{n % 10 + 1}.cbz")
            for n in range(6400)
        ),
    )
    return original, incoming, stat.st_mtime_ns


# Run inside the packaged app using the session established by _NAVIGATE.
OPERATOR_ACTIONS = """
from html.parser import HTMLParser
class Page(HTMLParser):
    def handle_starttag(self, tag, attrs):
        if tag == 'body':
            csrf.update(json.loads(dict(attrs)['hx-headers']))
status, issue_page = request('/issues/10001')
assert status == 200 and 'Harbor Lights' in issue_page, ('wanted issue', status)
Page().feed(issue_page)
assert csrf.get('X-CSRF-Token'), 'No form token on signed-in page'
from pathlib import Path
def set_transfer(method):
    status, body = request('/api/v1/config', method='PUT',
                           payload={'values':{'post_processing_method':method}})
    assert status == 200, ('transfer settings', status, body[:500])
source = {'file_path':'/imports/Harbor Lights 002 (2024).cbz'}
import_url = '/api/v1/issues/10001/import-file'
before = set(Path('/comics').rglob('*.cbz'))
set_transfer('move')
status, body = request(import_url, payload=source)
assert status == 409 and 'read-only' in body and 'Copy' in body, ('read-only move', status, body)
status, body = request(import_url + '/start', payload=source)
assert status == 202, ('start', status)
for _ in range(50):
    status, body = request(import_url + '/progress')
    assert status == 200, ('progress', status)
    progress = json.loads(body)
    if progress['state'] == 'failed':
        break
    time.sleep(.1)
assert progress['state'] == 'failed' and 'Copy' in progress['error_message'], progress['state']
assert set(Path('/comics').rglob('*.cbz')) == before
status, body = request('/api/v1/issues/10001')
assert status == 200 and json.loads(body)['status'] == 'wanted'
set_transfer('copy')
status, body = request(import_url, payload=source)
assert status == 201, ('import', status, body[:500])
artifact = json.loads(body)
assert artifact['issue_id'] == 10001 and artifact['file_path'].startswith('/comics/')
path = '/api/v1/import/7/cleanup/recheck_deferred_files'
status, body = request(path + '/preview')
assert status == 200, ('preview', status, body[:500])
preview = json.loads(body)
assert preview['affected_count'] > 0
status, body = request(path, payload={'preview_token':preview['preview_token'],
                                      'confirmation':preview['confirmation_text']})
assert status == 200, ('apply', status, body[:500])
assert json.loads(body)['requires_import_retry']
"""


def verify(sql, db, library, imports, expected, *, mylar_neighbor=False):
    for _ in range(50):
        state = sql(db, "SELECT status FROM import_jobs WHERE id=7")
        if state == "COMPLETED":
            break
        time.sleep(0.4)
    assert state == "COMPLETED", state
    assert sql(db, "SELECT status FROM import_files WHERE id=200") == "ALREADY_OWNED"
    assert sql(db, "SELECT matched_issue_id FROM import_files WHERE id=200") == "10000"
    assert sql(db, "SELECT status FROM import_files WHERE id=201") == "NO_MATCH"
    assert (
        sql(db, "SELECT count(*) FROM import_files WHERE status='SKIPPED' AND import_job_id=3")
        == "6400"
    )
    assert sql(db, "SELECT status FROM import_files WHERE id=2") == "FAILED"
    assert sql(db, "SELECT value FROM system_config WHERE key='post_processing_method'") == "copy"
    assert sql(db, "SELECT count(*) FROM series") == "642"
    assert sql(db, "SELECT count(*) FROM issues") == "10241"
    assert (
        sql(db, "SELECT count(*) FROM issues WHERE id IN (10000,10001) AND status='OWNED'") == "2"
    )
    assert sql(db, "SELECT count(*) FROM library_files WHERE issue_id IN (10000,10001)") == "2"
    assert sql(db, "SELECT storage_mode FROM library_files WHERE id=102") == "referenced"
    imported = Path(sql(db, "SELECT file_path FROM library_files WHERE issue_id=10001"))
    relative = imported.relative_to("/comics")
    assert set(p.relative_to(library) for p in library.rglob("*.cbz")) == {
        OWNED,
        relative,
        Path("one.cbz"),
        *(
            [Path("Cutover Shelf 1 (2024)/Cutover Shelf 1 (2024) #002.cbz")]
            if mylar_neighbor
            else []
        ),
    }
    # Embedded metadata can legitimately change during ingest; the actual pages must survive.
    with (
        zipfile.ZipFile(library / relative) as artifact,
        zipfile.ZipFile(imports / INCOMING) as source,
    ):
        for name in ("page-1.png", "page-2.png"):
            assert artifact.read(name) == source.read(name)
    assert hashlib.sha256((library / OWNED).read_bytes()).digest() == expected[0]
    assert hashlib.sha256((imports / INCOMING).read_bytes()).digest() == expected[1]
    assert (library / OWNED).stat().st_mtime_ns == expected[2]
