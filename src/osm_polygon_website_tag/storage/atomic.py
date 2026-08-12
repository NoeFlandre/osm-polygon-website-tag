"""Atomic file writes.

A file is written by first writing to a temporary path, then renaming it
into place. The rename is atomic on POSIX-compatible filesystems, so
readers never see a partially-written target. Auxiliary files are always
cleaned up, even when the rename fails.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from uuid import uuid4

_Mover = Callable[[Path, Path], None]


def _default_mover(src: Path, dst: Path) -> None:
    Path(src).replace(dst)


def atomic_write_file(
    source: str | Path,
    target: str | Path,
    *,
    mover: _Mover | None = None,
) -> Path:
    """Move ``source`` to ``target`` atomically.

    If ``source`` does not exist, :class:`FileNotFoundError` is raised.
    The temporary ``source`` file is always removed on success and on
    failure (unless the failure happened while removing it, in which case
    OSError propagates).
    """
    src = Path(source)
    dst = Path(target)
    if not src.exists():
        raise FileNotFoundError(src)

    move = mover if mover is not None else _default_mover
    try:
        move(src, dst)
    except BaseException:
        # Best-effort cleanup of the partial source.
        try:
            if src.exists():
                src.unlink()
        except OSError:
            pass
        raise
    return dst


def atomic_promote_bundle(
    promotions: list[tuple[Path, Path]],
    *,
    mover: _Mover | None = None,
) -> None:
    """Promote a set of staged files or restore every prior target.

    Individual renames are atomic; this rollback protocol provides the
    all-old-or-all-new contract required for a per-source shard bundle. The
    optional ``mover`` observes every forward rename (backup and promotion);
    rollback uses the trusted default rename so failure injection cannot
    prevent restoration.
    """
    move = mover if mover is not None else _default_mover
    backups: dict[Path, Path | None] = {}
    promoted: list[Path] = []
    token = uuid4().hex
    for staged, _target in promotions:
        if not staged.is_file():
            raise FileNotFoundError(staged)
    try:
        for _staged, target in promotions:
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                backup = target.with_name(f".{target.name}.{token}.backup")
                backups[target] = backup
                move(target, backup)
            else:
                backups[target] = None
        for staged, target in promotions:
            move(staged, target)
            promoted.append(target)
    except BaseException:
        for target in promoted:
            target.unlink(missing_ok=True)
        for target, backup_path in backups.items():
            if backup_path is not None and backup_path.exists():
                _default_mover(backup_path, target)
        raise
    else:
        for backup_path in backups.values():
            if backup_path is not None:
                backup_path.unlink(missing_ok=True)
