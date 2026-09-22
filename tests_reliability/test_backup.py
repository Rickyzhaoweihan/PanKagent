import sqlite3
import pytest
from deploy_reliability.backup import DATABASES, snapshot, verify, restore_drill


def fixture(tmp_path):
    state, config = tmp_path / 'state', tmp_path / 'config'
    for name in DATABASES:
        path = state / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(path) as db:
            db.executescript("PRAGMA journal_mode=WAL; CREATE TABLE reservations(id TEXT, amount REAL, payload BLOB); INSERT INTO reservations VALUES('paid-reservation', 2.5, x'ABCD');")
    asset = state / 'pankgraph-results/resources/assets/plot.svg'
    asset.parent.mkdir(parents=True)
    asset.write_text('<svg/>')
    for name in ('pankagent-vnext/runtime.env', 'pankgraph-results/runtime.env'):
        path = config / name
        path.parent.mkdir(parents=True)
        path.write_text('PRIVATE=test-fixture\n')
    return state, config


def test_restore_every_row_and_asset(tmp_path):
    state, config = fixture(tmp_path)
    snapshot(tmp_path / 'backup', state, config)
    result = restore_drill(tmp_path / 'backup', tmp_path / 'restored')
    assert result['verified'] and result['database_count'] == 4
    assert all(tables['reservations'] == 1 for tables in result['tables'].values())
    assert (tmp_path / 'restored/state/pankgraph-results/resources/assets/plot.svg').read_text() == '<svg/>'
    assert not (tmp_path / 'backup').stat().st_mode & 0o077


def test_tamper_rejected_before_restore(tmp_path):
    state, config = fixture(tmp_path)
    snapshot(tmp_path / 'backup', state, config)
    (tmp_path / 'backup/config/pankagent-vnext/runtime.env').write_text('altered')
    with pytest.raises(ValueError):
        restore_drill(tmp_path / 'backup', tmp_path / 'restored')
    assert not (tmp_path / 'restored').exists()


def test_never_overwrite_live_or_existing_destination(tmp_path):
    state, config = fixture(tmp_path)
    snapshot(tmp_path / 'backup', state, config)
    with pytest.raises(FileExistsError):
        restore_drill(tmp_path / 'backup', state)
    with pytest.raises(FileExistsError):
        snapshot(tmp_path / 'backup', state, config)


def test_unexpected_file_rejected(tmp_path):
    state, config = fixture(tmp_path)
    snapshot(tmp_path / 'backup', state, config)
    (tmp_path / 'backup/extra').write_text('unexpected')
    with pytest.raises(ValueError):
        verify(tmp_path / 'backup')


def test_symlink_source_rejected(tmp_path):
    state, config = fixture(tmp_path)
    asset = state / 'pankgraph-results/resources/assets/link'
    asset.symlink_to(config / 'pankagent-vnext/runtime.env')
    with pytest.raises(ValueError):
        snapshot(tmp_path / 'backup', state, config)
