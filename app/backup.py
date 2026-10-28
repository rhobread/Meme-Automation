"""Binary-safe PostgreSQL backups; restore requires an explicit separate database."""
import argparse
import os
from pathlib import Path
import re
import subprocess
import tempfile


def backup(destination):
    target = Path(destination).resolve()
    if not target.parent.is_dir() or target.exists():
        raise ValueError("Choose an existing backup directory and a new filename")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as output:
            temporary = Path(output.name)
            subprocess.run(["docker", "compose", "exec", "-T", "db", "pg_dump", "-U", "meme", "-d", "meme", "-Fc"],
                           stdout=output, stderr=subprocess.DEVNULL, check=True, timeout=300)
            output.flush()
            os.fsync(output.fileno())
        with temporary.open("rb") as source:
            if source.read(5) != b"PGDMP":
                raise ValueError("Invalid PostgreSQL backup output")
        os.link(temporary, target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def restore(source, database):
    if database == "meme" or not re.fullmatch(r"meme_restore_[a-z0-9_]{1,40}", database):
        raise ValueError("Restore only to a separately created meme_restore_* verification database")
    with Path(source).open("rb") as archive:
        if archive.read(5) != b"PGDMP":
            raise ValueError("Invalid PostgreSQL archive")
        archive.seek(0)
        subprocess.run(["docker", "compose", "exec", "-T", "db", "pg_restore", "-U", "meme", "--exit-on-error",
                        "--no-owner", "-d", database], stdin=archive, stderr=subprocess.DEVNULL, check=True, timeout=300)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("backup").add_argument("destination")
    command = sub.add_parser("restore-check")
    command.add_argument("source")
    command.add_argument("database")
    args = parser.parse_args()
    try:
        if args.action == "backup":
            backup(args.destination)
        else:
            restore(args.source, args.database)
    except Exception:
        parser.exit(1, "Backup/restore failed; check Docker, paths and isolated database. No restore to the application database is allowed.\n")
    print("Backup operation completed; verify and protect the archive before relying on it.")


if __name__ == "__main__":
    main()
