import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from orch.dispatcher import render_dispatcher
from orch.project import ProjectRegistry


class DispatcherRegistryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"

    def register(self):
        repo = self.root / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        (repo / "README.md").write_text("fixture\n", encoding="utf-8")
        return ProjectRegistry(self.home).add(
            repo, profile="safe", review_mode="off"
        )["project"]["project_id"]

    def test_missing_home_unknown_project_does_not_create_registry(self):
        with self.assertRaisesRegex(ValueError, "unknown_project"):
            render_dispatcher(self.home, project_id="missing")
        self.assertFalse(self.home.exists())

    def test_registry_removed_at_open_boundary_is_not_recreated(self):
        self.register()
        original_open = ProjectRegistry.open_readonly

        def remove_at_open(home):
            shutil.rmtree(self.home)
            return original_open(home)

        with mock.patch.object(
            ProjectRegistry, "open_readonly", side_effect=remove_at_open, create=True
        ):
            with self.assertRaisesRegex(ValueError, "unknown_project"):
                render_dispatcher(self.home, project_id="missing")
        self.assertFalse(self.home.exists())

    def test_dangling_config_symlink_is_rejected(self):
        self.home.mkdir()
        projects = self.home / "projects"
        projects.mkdir(mode=0o700)
        (projects / "missing.json").symlink_to(self.root / "absent.json")
        with self.assertRaisesRegex(ValueError, "project_config_unsafe"):
            render_dispatcher(self.home, project_id="missing")
        self.assertTrue((projects / "missing.json").is_symlink())

    def test_projects_symlink_is_rejected_without_touching_external_config(self):
        self.home.mkdir()
        outside = self.root / "outside"
        outside.mkdir()
        config = outside / "missing.json"
        config.write_text('{"project_id":"missing"}', encoding="utf-8")
        (self.home / "projects").symlink_to(outside, target_is_directory=True)
        before = config.read_bytes()
        with self.assertRaisesRegex(ValueError, "private_directory_unsafe"):
            render_dispatcher(self.home, project_id="missing")
        self.assertEqual(config.read_bytes(), before)
        self.assertTrue((self.home / "projects").is_symlink())

    def test_loose_registry_mode_is_rejected_without_chmod(self):
        project_id = self.register()
        projects = self.home / "projects"
        config = projects / (project_id + ".json")
        before = config.read_bytes()
        for mode in (0o755, 0o777):
            with self.subTest(mode=oct(mode)):
                os.chmod(projects, mode)
                with self.assertRaisesRegex(ValueError, "private_directory_unsafe"):
                    render_dispatcher(self.home, project_id=project_id)
                self.assertEqual(stat.S_IMODE(projects.stat().st_mode), mode)
                self.assertEqual(config.read_bytes(), before)
                self.assertFalse((self.home / "dispatchers").exists())

    def test_registered_project_renders(self):
        project_id = self.register()
        result = render_dispatcher(self.home, project_id=project_id)
        self.assertEqual(result["status"], "RENDERED")
        self.assertEqual(result["project_id"], project_id)
        self.assertTrue(Path(result["path"]).is_file())


if __name__ == "__main__":
    unittest.main()
