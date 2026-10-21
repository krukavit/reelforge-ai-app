from pathlib import Path
import tempfile

from app.path_resolver import TargetRootResolver


def test_relative_path_resolves_inside_target():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td).resolve()
        (root / "src").mkdir()
        (root / "src" / "main.py").write_text("x")
        resolver = TargetRootResolver(root)
        assert resolver.resolve("src/main.py") == (root / "src/main.py").resolve(strict=False)


def test_dot_resolves_to_target_root():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td).resolve()
        resolver = TargetRootResolver(root)
        assert resolver.resolve(".") == root


def test_parent_escape_is_blocked():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td).resolve()
        resolver = TargetRootResolver(root)

        try:
            resolver.resolve("../outside.txt", must_exist=False)
        except ValueError:
            pass
        else:
            raise AssertionError("parent escape was accepted")


def test_absolute_outside_path_is_blocked():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td).resolve()
        resolver = TargetRootResolver(root)
        outside = root.parent / "outside.txt"

        try:
            resolver.resolve(str(outside), must_exist=False)
        except ValueError:
            pass
        else:
            raise AssertionError("absolute outside path was accepted")


def test_existing_symlink_escape_is_blocked():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td).resolve()
        outside = root.parent / f"outside-target-{root.name}"
        outside.mkdir()
        link = root / "escape"
        link.symlink_to(outside, target_is_directory=True)

        resolver = TargetRootResolver(root)

        try:
            resolver.resolve("escape/secret.txt", must_exist=False)
        except ValueError:
            pass
        else:
            raise AssertionError("symlink escape was accepted")


def test_new_file_inside_nonexistent_parent_is_allowed():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td).resolve()
        resolver = TargetRootResolver(root)
        got = resolver.resolve("new/dir/file.txt", must_exist=False)
        assert got == (root / "new/dir/file.txt").resolve(strict=False)


def test_existing_file_is_required_by_default():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td).resolve()
        resolver = TargetRootResolver(root)

        try:
            resolver.resolve("missing.txt")
        except ValueError:
            pass
        else:
            raise AssertionError("missing path was accepted with must_exist=True")


def test_target_root_is_distinct_from_agent_project_root():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td).resolve()
        resolver = TargetRootResolver(root)
        agent_root = Path(__file__).resolve().parent.parent
        assert resolver.root == root
        assert resolver.root != agent_root


def test_relative_root_is_rejected():
    try:
        TargetRootResolver(".")
    except ValueError:
        pass
    else:
        raise AssertionError("relative target root was accepted")


def test_file_root_is_rejected():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td).resolve()
        file_path = root / "file.txt"
        file_path.write_text("x")

        try:
            TargetRootResolver(file_path)
        except ValueError:
            pass
        else:
            raise AssertionError("file was accepted as target root")


if __name__ == "__main__":
    print("TARGET_ROOT_RESOLVER_TESTS_READY")
