# AirDC++ Integration

AirDC++ support is experimental and disabled by default. It adds Direct
Connect as an independent acquisition protocol without changing the existing
Usenet, torrent, or direct-download providers.

Enable it with `PULLBOX_AIRDCPP_ENABLED=true`, restart Pullbox, and then add one
or more AirDC++ clients under **Settings > Download Clients**.

## AirDC++ API user

Create a dedicated, non-administrator AirDC++ Web API user for Pullbox. Grant
only these permissions:

- `search`
- `download`
- `queue_view`
- `queue_edit`
- `hubs_view`
- `settings_view`

Do not grant `admin`, filesystem, share, chat, event-edit, web-user management,
or `transfers`. Pullbox uses the queue API for whole-file progress and
completion, so `transfers` is neither needed nor supported.

The supported compatibility floor is Web API version 1 and feature level 10.
Set AirDC++'s **Minimum search interval** to at least 45 seconds before testing
the connection. Pullbox checks the API version, permissions, WebSocket setup,
and minimum interval without changing AirDC++ settings or starting a search.

## Client settings

For each AirDC++ client, configure:

- **URL**: the AirDC++ Web API base URL, without credentials, a query string,
  or a fragment.
- **Username and password**: the dedicated API account. Pullbox encrypts the
  password at rest and never returns it from the API.
- **Remote path**: the absolute completion root reported by AirDC++, commonly
  `/Downloads`.
- **Download directory**: the absolute path for that same directory inside the
  Pullbox container, such as `/downloads/airdcpp`.
- **Hub allowlist**: optional normalized `adc://` or `adcs://` hub URLs, one per
  line. Leave it empty to search every currently connected hub.

The remote and local roots must identify the same bounded directory through
the two containers' mounts. Completed files outside the exact remote root,
paths containing traversal, symlinks, non-regular files, and unsupported
extensions are rejected. Do not configure `/` or another broad filesystem
root.

Multiple AirDC++ clients may be enabled. A manual search fans out once to every
enabled and ready AirDC++ client; each client then searches all of its connected
hubs unless its allowlist narrows the scope. Failure or cooldown on one client
does not hide usable results from another client or acquisition source.

## Search cooldown and manual searches

Pullbox enforces one durable search gate per configured AirDC++ client. Manual
searches, automatic searches, different query text, and queue alternate-source
searches all share the same 45-second-or-longer interval. The gate survives a
Pullbox restart.

If a manual search reaches a client during its cooldown, Pullbox keeps the
search spinner active and displays:

> Direct Connect search will resume in {seconds} seconds to respect the
> 45-second hub cooldown.

The search resumes automatically when the client becomes eligible. The live
region announces the wait when it begins and the resume transition, not every
countdown tick.

Manual results are grouped and ranked using normalized file identity, size,
online source availability, and route freshness. The browser receives only a
short-lived opaque route token; user identities, hub identities, raw result
IDs, TTH values, and remote paths are not exposed in the page.

## Queue, progress, and import

Choose **Grab** on an AirDC++ result to create the durable Pullbox acquisition
record before the remote queue is mutated. Pullbox then reconciles the exact
AirDC++ bundle from both WebSocket events and bounded REST snapshots. The
Downloads page shows durable progress, speed, ETA, and terminal state without
polling AirDC++ separately for every row.

Cancel removes the exact known bundle and records the cancellation locally.
Retry creates a fresh queue attempt from retained provenance. Pullbox does not
guess when a bundle identity is ambiguous.

After AirDC++ reports a completed/shared bundle, Pullbox maps the completed path
through the configured roots and sends the archive through the normal comic
validation, naming, ComicInfo, library registration, and cleanup pipeline. A
database lease prevents duplicate imports after overlapping scheduler runs or
restarts. The raw AirDC++ completion path is redacted from diagnostic exports.

Automatic wanted searches may include AirDC++ results only when **Automatic
search** is enabled for that client. During the experimental soak period those
results are evaluation-only: Pullbox records selection diagnostics but does not
automatically mutate the AirDC++ queue. Manual Grab is the supported test path.

## Troubleshooting

Use **Test** on the client card and **System > Health > Download Clients**.
Common states are:

- **Authentication failed**: verify the dedicated username and password.
- **Permissions incomplete**: grant exactly the six required permissions.
- **API incompatible**: upgrade AirDC++ Web Client to an API version 1,
  feature-level-10-or-newer release.
- **Minimum interval below 45 seconds**: change the AirDC++ setting, then test
  again.
- **Reconnecting or unavailable**: verify the API URL and container/network
  reachability; the supervisor retries with bounded backoff.
- **Completed path unavailable or unsafe**: verify the remote/local root pair
  and the shared volume mount. Do not bypass the path safety checks.

When diagnosing a missing result, first confirm the hub is connected in
AirDC++, the client's search toggle is enabled, its cooldown has elapsed, and
the optional hub allowlist exactly matches the connected hub URL.
