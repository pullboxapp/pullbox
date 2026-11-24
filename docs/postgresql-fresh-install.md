# Start a new Pullbox library on PostgreSQL

Use this for a **new, empty v2 library**. SQLite remains the default. If you
already have series, files, accounts, or import history in Pullbox, use the
[SQLite migration guide](sqlite-postgresql-migration.md) instead. Starting an
empty PostgreSQL database does not transfer any of them.

The [standalone Compose example](../docker/docker-compose.postgres.yml) runs
one Pullbox instance and a private database. PostgreSQL has no published port.
The example creates separate, project-scoped volumes for the database, app
state, comics, downloads and imports; it does not mount your current library.
The database administrator and Pullbox use different credentials.

## Prepare the stack

1. Create a **new private directory** outside your existing stack and copy the
   example there as `compose.yaml`. Run the commands below in that directory.
   Choose an unused Compose project name and host port. The example's default
   project name is `pullbox-pg`. Keep the chosen name for subsequent starts:
   changing it selects different volumes.
2. In that directory create a private `.env` file (owner read/write only).
   Set the following values. Choose two **different** random passwords in your
   password manager; long hexadecimal values avoid Compose quoting issues.
   Do not use these placeholders as passwords or commit this file.

   ```dotenv
   COMPOSE_PROJECT_NAME=pullbox-pg
   PULLBOX_IMAGE=<your PostgreSQL-capable Pullbox v2 image tag or digest>
   PULLBOX_PG_HOST_PORT=8585
   PULLBOX_PG_ADMIN_PASSWORD=<random database administrator password>
   PULLBOX_PG_APP_PASSWORD=<different random application role password>
   ```

3. Run `docker compose config --quiet` to check the configuration without
   printing the passwords. Protect the file, its backups and access to Docker:
   someone who can inspect the running containers can read their environment.
   PostgreSQL 18 is the example default. For PostgreSQL 16, set **both**
   `PULLBOX_PG_IMAGE=postgres:16-bookworm` and
   `PULLBOX_PG_VOLUME_ROOT=/var/lib/postgresql/data` before first startup so
   the named volume uses that image's data mount. Do not change the major
   version or mount on an initialized volume as an upgrade.

## Create the database and sign in

Start **only the database**. The next two commands are one-time provisioning;
do not rerun them on restart. If the role or database already exists on what
you thought was a new installation, stop and check the project and volumes.

```sh
docker compose up -d --wait db
docker compose exec db createuser -U postgres --no-superuser --no-createdb --no-createrole --pwprompt pullbox
docker compose exec db createdb -U postgres --owner=pullbox --template=template0 pullbox
docker compose up -d --wait pullbox
```

At the `createuser` prompts enter the **application role** password from
`.env`, not the database administrator password. Pullbox owns only its
application database and can create and migrate its schema without cluster
administrator privileges. A role password mismatch prevents startup; creating a
new database or deleting a volume is not the fix.

On the Docker host, open `http://127.0.0.1:8585` (or your chosen port).
Create your Pullbox administrator account and sign in. This is a **third,
separate credential**. Check **System > About > Database** for PostgreSQL and
its server version. If you are testing from another computer, use an SSH
tunnel or your existing authenticated HTTPS reverse proxy; do not publish the
database port to reach the web UI.

For a fresh-install persistence check, run `docker compose down`, then
`docker compose up -d --wait` in the same project. Sign in with the same
account. You should not be asked to create it again. **Do not add `-v` or
`--volumes`: that removes the database, app state and managed media in this
example.** If setup reappears unexpectedly, stop and check the project name,
database and mounted volumes before creating anything.

## Storage and backups

The `pgdata` volume contains the database; `appdata` contains the application
key and other durable state. Keep both, along with the media volumes. If you
need host-visible folders for imports or a download client, configure those
mounts before using the library and grant Pullbox's UID/GID `65532:65532`
the necessary access. Replacing a populated volume with an empty bind mount
hides its contents; it does not move them. A GCD Local catalog, if used, is
still a separate read-only SQLite file, not part of PostgreSQL.

Set up a [PostgreSQL backup and restore rehearsal](sqlite-postgresql-migration.md#back-up-and-restore-a-postgresql-library).
Pullbox's SQLite backup ZIPs do not back up PostgreSQL.

This example keeps the database on one Docker host. For a separately hosted
server, use a dedicated database owner, private networking, a hostname covered
by the server certificate, and TLS with certificate and hostname verification.
For the asyncpg connection, supply `PGSSLMODE=verify-full` and
`PGSSLROOTCERT` pointing to the trusted CA mounted read-only in Pullbox. Do
not disable verification to work around a hostname or CA mismatch. See
[PostgreSQL TLS verification](https://www.postgresql.org/docs/18/libpq-ssl.html)
and the [asyncpg connection reference](https://magicstack.github.io/asyncpg/current/api/index.html#connection).
