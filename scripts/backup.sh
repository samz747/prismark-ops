#!/bin/sh
# Daily backup of the database and uploaded documents. Keep 14 days.
# Add to the server's crontab:  0 2 * * * /path/to/prismark-ops/scripts/backup.sh
set -e
cd "$(dirname "$0")/.."
STAMP=$(date +%Y-%m-%d)
mkdir -p backups
. ./.env
docker compose exec -T db pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB" | gzip > "backups/db-$STAMP.sql.gz"
docker compose run --rm -T --entrypoint tar app czf - -C /data uploads > "backups/uploads-$STAMP.tar.gz"
find backups -type f -mtime +14 -delete
echo "Backup done: $STAMP"
