"""Git source: sparse, shallow clone of a documentation repository.

Why git and not the GitHub REST API:
* no API rate limit (60 req/h without a token) and no token to keep secret;
* `git ls-tree` gives the blob SHA of every file in one call - a content hash computed by
  GitHub itself, so "is the file changed?" is a string comparison;
* sparse checkout downloads only the folders we index (RST files, without ~550 screenshots).
"""
from __future__ import annotations

import logging
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("grabber.git")


class GitError(RuntimeError):
    pass


@dataclass
class RemoteFile:
    path: str  # path inside the repository, e.g. content/applications/.../wave.rst
    blob_sha: str
    size: int = 0  # filled after download


class GitSource:
    def __init__(
        self,
        repo_url: str,
        branch: str,
        include: list[str],
        workdir: Path,
        extensions: list[str] | None = None,
        timeout: int = 300,
        retries: int = 3,
    ) -> None:
        self.repo_url = repo_url
        self.branch = branch
        self.include = [p.strip("/") for p in include]
        self.workdir = Path(workdir)
        self.extensions = tuple(extensions or [".rst"])
        self.timeout = timeout
        self.retries = retries

    # ------------------------------------------------------------------ git helpers
    def _git(self, *args: str, cwd: Path | None = None, retry: bool = False) -> str:
        cmd = ["git", *args]
        attempts = self.retries if retry else 1
        last_err = ""
        for attempt in range(1, attempts + 1):
            try:
                proc = subprocess.run(
                    cmd,
                    cwd=cwd or self.workdir,
                    capture_output=True,
                    text=True,
                    timeout=self.timeout,
                    encoding="utf-8",
                )
            except FileNotFoundError as exc:
                raise GitError("git is not installed or not in PATH") from exc
            except subprocess.TimeoutExpired:
                last_err = f"timeout after {self.timeout}s"
            else:
                if proc.returncode == 0:
                    return proc.stdout
                last_err = (proc.stderr or proc.stdout).strip()
            if attempt < attempts:
                delay = 2 ** attempt
                log.warning("git %s failed (attempt %d/%d): %s; retry in %ds", args[0], attempt, attempts, last_err, delay)
                time.sleep(delay)
        raise GitError(f"git {' '.join(args)} failed: {last_err}")

    def _sparse_patterns(self) -> list[str]:
        patterns = []
        for path in self.include:
            for ext in self.extensions:
                patterns.append(f"/{path}/**/*{ext}")
        return patterns

    # ------------------------------------------------------------------ public API
    def sync(self) -> str:
        """Clone on first run, fetch + hard reset afterwards. Returns HEAD commit SHA."""
        if not (self.workdir / ".git").exists():
            if self.workdir.exists():
                shutil.rmtree(self.workdir)
            self.workdir.parent.mkdir(parents=True, exist_ok=True)
            log.info("cloning %s (branch %s, sparse, depth 1)", self.repo_url, self.branch)
            self._git(
                "clone", "--filter=blob:none", "--no-checkout", "--depth", "1",
                "--branch", self.branch, self.repo_url, str(self.workdir),
                cwd=self.workdir.parent, retry=True,
            )
            self._git("sparse-checkout", "set", "--no-cone", *self._sparse_patterns())
            self._git("checkout", self.branch, retry=True)
        else:
            log.info("fetching updates for branch %s", self.branch)
            self._git("sparse-checkout", "set", "--no-cone", *self._sparse_patterns())
            self._git("fetch", "--depth", "1", "origin", self.branch, retry=True)
            self._git("reset", "--hard", "FETCH_HEAD", retry=True)
        return self.head_commit()["sha"]

    def head_commit(self) -> dict:
        out = self._git("log", "-1", "--format=%H%n%cI%n%s").splitlines()
        return {"sha": out[0], "date": out[1] if len(out) > 1 else "", "subject": out[2] if len(out) > 2 else ""}

    def list_files(self) -> list[RemoteFile]:
        # NB: no `-l` (size) flag - in a blob-less partial clone it makes git download every
        # blob (including ~550 screenshots) one by one just to print its size: 3.5 min vs 0.1 s.
        out = self._git("ls-tree", "-r", "HEAD", "--", *self.include)
        files: list[RemoteFile] = []
        for line in out.splitlines():
            # <mode> SP <type> SP <object> TAB <path>
            meta, _, path = line.partition("\t")
            parts = meta.split()
            if len(parts) < 3 or parts[1] != "blob":
                continue
            if not path.endswith(self.extensions):
                continue
            files.append(RemoteFile(path=path, blob_sha=parts[2], size=0))
        return files

    def read(self, path: str) -> bytes:
        file_path = self.workdir / path
        if file_path.exists():
            return file_path.read_bytes()
        # not checked out (should not happen with sparse patterns) -> read the blob directly
        proc = subprocess.run(
            ["git", "show", f"HEAD:{path}"], cwd=self.workdir, capture_output=True, timeout=self.timeout
        )
        if proc.returncode != 0:
            raise GitError(f"cannot read {path}: {proc.stderr.decode(errors='replace').strip()}")
        return proc.stdout
