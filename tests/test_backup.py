import pytest

from app import backup


def test_binary_safe_backup_and_no_overwrite(tmp_path, monkeypatch):
    def run(command, **kwargs):
        assert command[-1] == "-Fc" and command[:3] == ["docker", "compose", "exec"]
        kwargs["stdout"].write(b"PGDMP\x00\xff\x80test")
    monkeypatch.setattr(backup.subprocess, "run", run)
    target = tmp_path / "snapshot.dump"
    backup.backup(target)
    assert target.read_bytes() == b"PGDMP\x00\xff\x80test"
    with pytest.raises(ValueError):
        backup.backup(target)


def test_failed_dump_does_not_leave_archive(tmp_path, monkeypatch):
    def run(*args, **kwargs):
        kwargs["stdout"].write(b"invalid")
    monkeypatch.setattr(backup.subprocess, "run", run)
    with pytest.raises(ValueError):
        backup.backup(tmp_path / "invalid.dump")
    assert list(tmp_path.iterdir()) == []


def test_restore_requires_separate_database(tmp_path, monkeypatch):
    source = tmp_path / "snapshot.dump"
    source.write_bytes(b"PGDMPtest")
    calls = []
    monkeypatch.setattr(backup.subprocess, "run", lambda command, **kwargs: calls.append(command))
    for name in ("meme", "postgres", "meme_restore_foo;drop database meme"):
        with pytest.raises(ValueError):
            backup.restore(source, name)
    backup.restore(source, "meme_restore_check")
    assert calls[0][-1] == "meme_restore_check" and "--clean" not in calls[0]
