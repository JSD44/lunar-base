"""Optional real Docker lifecycle check; never uses a game database.

docker pull python:3.12-slim
RUN_DOCKER_TESTS=1 python -m unittest discover -s tests -v
"""

import os
import socket
import sqlite3
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

import docker

from web import config
from web.services import backup_service as backups
from web.services import service_control as control


@unittest.skipUnless(os.getenv("RUN_DOCKER_TESTS") == "1", "set RUN_DOCKER_TESTS=1 to use a disposable Docker container")
class DockerRestoreIntegrationTests(unittest.TestCase):
    def test_real_container_restore_and_preserved_stopped_state(self):
        client = docker.DockerClient(
            base_url=f"unix://{control.DOCKER_SOCKET}", version="auto", timeout=60,
        )
        self.addCleanup(client.close)
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        container = client.containers.run(
            "python:3.12-slim",
            ["python", "-m", "http.server", "8003", "--bind", "0.0.0.0"],
            name=f"lunar-base-restore-test-{uuid.uuid4().hex[:12]}",
            ports={"8003/tcp": ("127.0.0.1", port)},
            restart_policy={"Name": "unless-stopped"},
            detach=True,
        )
        self.addCleanup(container.remove, force=True)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        archives = root / "backups"
        archives.mkdir()
        db_path = root / "game.db"
        filename = "backup_2026-01-01T00-00-00_manual.db"
        for path, value in ((db_path, "current"), (archives / filename, "saved")):
            with sqlite3.connect(path) as db:
                db.execute("CREATE TABLE save (value TEXT)")
                db.execute("INSERT INTO save VALUES (?)", (value,))

        with (
            patch.object(control, "CONTAINER_NAME", container.name),
            patch.object(config, "GAME_DB_PATH", db_path),
            patch.object(config, "BACKUP_DIR", archives),
            patch.object(config, "LUNAR_TEAR_GRPC_HOST", "127.0.0.1"),
            patch.object(config, "LUNAR_TEAR_DEFAULT_GRPC_PORT", port),
            patch.dict(os.environ, {"LUNAR_TEAR_DEFAULT_GRPC_PORT": str(port)}),
        ):
            self.assertTrue(control._wait_for_port(port, True, 30))
            self.assertTrue(control.available())
            result = backups.restore_backup(filename, manage_server=True)
            self.assertTrue(result.server_restarted)
            self.assertTrue(control.is_active())
            with sqlite3.connect(db_path) as db:
                self.assertEqual(db.execute("SELECT value FROM save").fetchone()[0], "saved")
            safety = next(archives.glob("*_pre-restore.db"))
            with sqlite3.connect(safety) as db:
                self.assertEqual(db.execute("SELECT value FROM save").fetchone()[0], "current")
            container.stop(timeout=5)
            self.assertTrue(control._wait_for_port(port, False, 10))
            result = backups.restore_backup(filename, manage_server=True)
            self.assertFalse(result.server_restarted)
            self.assertFalse(control.is_active())


if __name__ == "__main__":
    unittest.main()
