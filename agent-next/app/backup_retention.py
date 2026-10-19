"""Bounded retention for backups created and recorded by Agent Next.

Safety rule: automatic cleanup may delete only regular files that are already
recorded as successful Agent-Next backups in the local StateStore. It never
scans arbitrary directories, never follows symlinks, and never deletes the
newest backup for a target.
"""
from __future__ import annotations

import os
from pathlib import Path


class BackupRetention:
    def __init__(self, store, max_per_target=5):
        self.store = store
        self.max_per_target = max(1, int(max_per_target))

    def prune(self, target):
        """Keep newest max_per_target successful backups for target.

        Returns a report. Missing files are removed from tracking only by
        marking them PRUNED_MISSING; actual files are never invented/deleted
        based on directory scans.
        """
        rows = self.store.list_backups(target=target, status="SUCCESS")
        # list_backups is newest-first. A path may have been reused by a
        # later backup, so never unlink a file while a newer SUCCESS record
        # still points at the same path. Older duplicate records are only
        # retired from tracking.
        latest_by_path = {}
        for row in rows:
            latest_by_path.setdefault(row["backup_path"], row["id"])

        candidates = rows[self.max_per_target:]
        deleted = []
        skipped = []
        pruned_records = 0

        # A recorded symlink must never be allowed to make its target
        # deletable.  Otherwise pruning a real file that is referenced by a
        # newer/parallel recorded symlink would silently break that link.
        symlink_targets = set()
        for row in rows:
            raw_link = row["backup_path"]
            link_path = Path(os.path.expanduser(raw_link))
            if link_path.is_symlink():
                try:
                    symlink_targets.add(os.path.realpath(str(link_path)))
                except OSError:
                    # Broken symlink is still protected as a symlink record;
                    # there is simply no resolvable target to protect.
                    pass

        for row in candidates:
            raw = row["backup_path"]
            if latest_by_path.get(raw) != row["id"]:
                self.store.mark_backup_status(row["id"], "PRUNED")
                pruned_records += 1
                continue
            path = Path(os.path.expanduser(raw))
            try:
                # Never follow symlinks and never delete directories. Also
                # protect regular files referenced by recorded symlinks.
                if path.is_symlink() or not path.is_file():
                    skipped.append({"path": raw, "reason": "NOT_REGULAR_FILE"})
                    continue
                try:
                    resolved = os.path.realpath(str(path))
                except OSError:
                    resolved = None
                if resolved and resolved in symlink_targets:
                    skipped.append({"path": raw, "reason": "REFERENCED_BY_SYMLINK"})
                    continue
                path.unlink()
                self.store.mark_backup_status(row["id"], "PRUNED")
                deleted.append(raw)
            except OSError as exc:
                skipped.append({"path": raw, "reason": str(exc)})
        return {
            "target": target,
            "max_per_target": self.max_per_target,
            "kept": len(rows) - len(candidates) + len(skipped),
            "deleted": deleted,
            "pruned_records": pruned_records,
            "skipped": skipped,
        }
