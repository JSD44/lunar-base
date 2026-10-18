"""Restore safety checks using temporary databases and a simulated Docker API.

Run with: python -m unittest discover -s tests -v
"""

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import docker
from starlette.requests import Request

from web import config
from web.app import app
from web.routes import backup as backup_routes
from web.services import backup_service as backups
from web.services import service_control as control


class DockerRestoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.db = root / "game.db"
        self.archives = root / "backups"
        self.archives.mkdir()
        self.filename = "backup_2026-01-01T00-00-00_manual.db"
        self.seed(self.db, "current")
        self.seed(self.archives / self.filename, "saved")
        for obj, name, value in (
            (config, "GAME_DB_PATH", self.db),
            (config, "BACKUP_DIR", self.archives),
            (control, "CONTAINER_NAME", "test-grpc"),
            (control, "SETTLE_S", 0),
        ):
            self.enterContext(patch.object(obj, name, value))
        self.client = Mock()
        self.container = self.client.containers.get.return_value
        self.set_state("running")
        self.container.stop.side_effect = lambda **kwargs: self.set_state("exited")
        self.container.start.side_effect = lambda: self.set_state("running")
        self.factory = self.enterContext(patch("docker.DockerClient", return_value=self.client))
        self.port = self.enterContext(patch.object(control, "port_is_open", side_effect=lambda port: self.container.attrs["State"]["Running"]))

    @staticmethod
    def seed(path, value):
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE save (value TEXT)")
            db.execute("INSERT INTO save VALUES (?)", (value,))

    @staticmethod
    def read(path):
        with sqlite3.connect(path) as db:
            return db.execute("SELECT value FROM save").fetchone()[0]

    def set_state(self, status):
        self.container.attrs = {"State": {"Status": status, "Running": status == "running"}}

    def test_running_container_stops_before_swap_and_restarts_afterwards(self):
        original = backups._swap_database

        def swap(path):
            self.assertFalse(control.is_active())
            self.assertEqual(self.read(self.db), "current")
            original(path)

        with patch.object(backups, "_swap_database", side_effect=swap):
            result = backups.restore_backup(self.filename, manage_server=True)
        self.assertEqual(self.read(self.db), "saved")
        safety = next(self.archives.glob("*_pre-restore.db"))
        self.assertEqual(self.read(safety), "current")
        self.container.stop.assert_called_once_with(timeout=30)
        self.container.start.assert_called_once()
        self.assertTrue(result.server_restarted)
        self.assertTrue(control.is_active())

    def test_stopped_container_stays_stopped(self):
        self.set_state("exited")
        result = backups.restore_backup(self.filename, manage_server=True)
        self.assertEqual(self.read(self.db), "saved")
        self.assertFalse(result.server_restarted)
        self.container.stop.assert_not_called()
        self.container.start.assert_not_called()

    def test_swap_failure_still_restarts(self):
        with patch.object(backups, "_swap_database", side_effect=OSError("disk failure")):
            with self.assertRaisesRegex(OSError, "disk failure"):
                backups.restore_backup(self.filename, manage_server=True)
        self.assertEqual(self.read(self.db), "current")
        self.container.start.assert_called_once()

    def test_stop_failure_never_swaps(self):
        self.container.stop.side_effect = docker.errors.APIError("stop denied")
        with self.assertRaisesRegex(control.ServiceError, "stop denied"):
            backups.restore_backup(self.filename, manage_server=True)
        self.assertEqual(self.read(self.db), "current")
        self.container.start.assert_not_called()

    def test_container_still_running_blocks_swap_even_with_closed_port(self):
        self.container.stop.side_effect = None
        self.port.side_effect = None
        self.port.return_value = False
        with patch.object(control, "_wait_for_port", return_value=True), self.assertRaisesRegex(control.ServiceError, "still active"):
            backups.restore_backup(self.filename, manage_server=True)
        self.assertEqual(self.read(self.db), "current")

    def test_open_port_after_stop_blocks_swap_and_attempts_restart(self):
        with patch.object(control, "_wait_for_port", side_effect=[False, True]):
            with self.assertRaisesRegex(control.ServiceError, "still accepting"):
                backups.restore_backup(self.filename, manage_server=True)
        self.assertEqual(self.read(self.db), "current")
        self.container.start.assert_called_once()

    def test_inactive_target_with_another_listener_blocks_swap(self):
        self.set_state("exited")
        self.port.side_effect = None
        self.port.return_value = True
        with self.assertRaises(backups.RestoreBlocked):
            backups.restore_backup(self.filename, manage_server=True)
        self.assertEqual(self.read(self.db), "current")

    def test_restart_timeout_is_reported(self):
        with patch.object(control, "_wait_for_port", side_effect=[True, False]):
            with self.assertRaisesRegex(control.ServiceError, "not listening"):
                backups.restore_backup(self.filename, manage_server=True)
        self.assertEqual(self.read(self.db), "saved")

    def test_missing_container_blocks_both_managed_and_manual_restore(self):
        self.client.containers.get.side_effect = docker.errors.NotFound("no such container")
        self.assertFalse(control.available())
        self.assertIn("no such container", control.unavailable_reason())
        for managed in (False, True):
            with self.subTest(managed=managed), self.assertRaises(backups.RestoreBlocked):
                backups.restore_backup(self.filename, manage_server=managed)
        self.assertEqual(self.read(self.db), "current")

    def test_daemon_failure_does_not_fall_back_to_systemd(self):
        self.factory.side_effect = docker.errors.DockerException("permission denied")
        with patch.object(control, "_systemctl") as systemctl:
            self.assertFalse(control.available())
            self.assertIn("permission denied", control.unavailable_reason())
            systemctl.assert_not_called()

    def test_unknown_state_fails_closed(self):
        self.container.attrs = {"State": {}}
        self.assertFalse(control.available())
        with self.assertRaises(control.ServiceError):
            control.is_active()

    def test_restarting_and_paused_containers_are_active(self):
        for state in ("restarting", "paused"):
            self.set_state(state)
            self.assertTrue(control.is_active())
            with self.assertRaises(backups.RestoreBlocked):
                backups.restore_backup(self.filename)

    def test_missing_backup_does_not_stop_container(self):
        with self.assertRaises(FileNotFoundError):
            backups.restore_backup("missing.db", manage_server=True)
        self.container.stop.assert_not_called()

    def test_without_container_configuration_uses_systemd(self):
        with patch.object(control, "CONTAINER_NAME", ""), patch.object(control, "_systemctl", return_value=["systemctl"]), patch.object(control, "unit_exists", return_value=True):
            self.assertTrue(control.available())
            self.assertEqual(control.target_name(), f"{control.UNIT_NAME}.service")
        self.factory.assert_not_called()

    def test_pages_display_container_target_and_enable_managed_restore(self):
        request = Request({
            "type": "http", "method": "GET", "path": "/backups",
            "headers": [], "scheme": "http", "server": ("test", 80),
            "router": app.router,
        })
        for view in (backup_routes.home, backup_routes.list_backups):
            html = view(request).body.decode()
            self.assertIn("Docker container test-grpc", html)
            self.assertNotIn("test-grpc.service", html)
        self.assertIn('name="manage_server" value="on"', html)


if __name__ == "__main__":
    unittest.main()
