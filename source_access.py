"""Keep sandbox source discovery inside the folders the user selected."""

from pathlib import Path
import fnmatch
import os
import stat


def _require_source(path, root, directory=False):
    """Reject path escapes, symlink traversal and nonregular source files.

    The operating-system sandbox remains the security boundary. These checks
    additionally stop provider metadata from expanding a source's read scope.
    A user-selected root may itself have a canonical filesystem alias.
    """
    path, root = Path(path).absolute(), Path(root).absolute()
    try:
        relative = path.relative_to(root)
        if (not relative.parts and not directory) or ".." in relative.parts:
            raise ValueError
        current = root.resolve(strict=True)
        mode = current.lstat().st_mode
        for index, part in enumerate(relative.parts):
            current = current / part
            mode = current.lstat().st_mode
            if stat.S_ISLNK(mode):
                raise ValueError
            if index < len(relative.parts) - 1 and not stat.S_ISDIR(mode):
                raise ValueError
        if not (stat.S_ISDIR(mode) if directory else stat.S_ISREG(mode)):
            raise ValueError
        return current
    except (OSError, ValueError, RuntimeError):
        raise ValueError("The source file is outside its authorized folder or is unavailable.") from None


def require_source_path(path, root):
    return _require_source(path, root)


def require_source_directory(path, root):
    return _require_source(path, root, directory=True)


def source_glob(root, pattern, errors=None):
    """Enumerate a bounded-depth pattern without entering symlinked folders.

    Path.glob can follow directory symlinks before a caller validates the final
    file. Validate every directory before listing it; reject invalid siblings
    individually so one bad entry cannot hide other authorized sources.
    """
    root = Path(root)
    parts = Path(pattern).parts
    if not parts or any(part in ("..", "**") for part in parts) or Path(pattern).is_absolute():
        raise ValueError("Unsupported source discovery pattern.")

    def rejected():
        if errors is not None:
            errors.append(True)

    def walk(folder, remaining):
        try:
            _require_source(folder, root, directory=True)
            entries = os.scandir(folder)
        except (OSError, ValueError):
            rejected()
            return
        with entries:
            for entry in entries:
                if not fnmatch.fnmatchcase(entry.name, remaining[0]):
                    continue
                child = folder / entry.name
                if len(remaining) > 1:
                    yield from walk(child, remaining[1:])
                else:
                    try:
                        require_source_path(child, root)
                    except (OSError, ValueError):
                        rejected()
                        continue
                    yield child

    yield from walk(root, parts)
