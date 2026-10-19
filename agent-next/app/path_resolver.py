from pathlib import Path


class TargetRootResolver:
    """Resolve task-relative filesystem paths inside an immutable target root."""

    def __init__(self, root):
        root_path = Path(root).expanduser()

        if not root_path.is_absolute():
            raise ValueError("target root must be an absolute path")

        try:
            root_path = root_path.resolve(strict=True)
        except (FileNotFoundError, OSError) as exc:
            raise ValueError("target root must exist") from exc

        if not root_path.is_dir():
            raise ValueError("target root must be a directory")

        self.root = root_path

    def resolve(self, path, must_exist=True):
        if path is None:
            raise ValueError("path is required")

        try:
            candidate = Path(path).expanduser()
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid path") from exc

        if not candidate.is_absolute():
            candidate = self.root / candidate

        try:
            resolved = candidate.resolve(strict=must_exist)
        except (FileNotFoundError, OSError) as exc:
            raise ValueError("path does not exist") from exc

        try:
            resolved.relative_to(self.root)
        except ValueError as exc:
            raise ValueError("path escapes target root") from exc

        return resolved
