#!/bin/bash
# Backup script for Spac2 PostgreSQL Database
# Uses docker exec to run pg_dump inside the container to avoid host dependencies.

# Determine directory of this script
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

# Create backup directory
BACKUP_DIR="$DIR/backups"
mkdir -p "$BACKUP_DIR"

TIMESTAMP=$(date +"%Y%m%d_%H%M%S")
BACKUP_FILE="$BACKUP_DIR/backup_spac2_$TIMESTAMP.sql"

echo "Starting database backup from container 'spac2-postgres'..."

# Run pg_dump inside container and write to host file
docker exec -t spac2-postgres pg_dump -U postgres spac2 > "$BACKUP_FILE"

if [ $? -eq 0 ]; then
  # Compress backup file
  gzip -f "$BACKUP_FILE"
  echo "Backup successfully completed and compressed: ${BACKUP_FILE}.gz"
else
  echo "Error: Database backup failed!" >&2
  exit 1
fi
