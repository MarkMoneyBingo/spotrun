"""File synchronization over SSH/rsync."""

from __future__ import annotations

import fnmatch
import os
import re
import shlex
import subprocess
from pathlib import Path

from rich.console import Console
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    TextColumn,
    TransferSpeedColumn,
)

console = Console()

DEFAULT_EXCLUDES = (".venv", "__pycache__", ".git", "*.pyc")

_KNOWN_HOSTS = str(Path.home() / ".spotrun" / "known_hosts")

# rsync --info=progress2 emits lines like:
#   1,234,567  45%   12.34MB/s    0:01:23 (xfr#10, to-chk=90/200)
_PROGRESS2_RE = re.compile(
    r"[\s,]*([\d,]+)\s+(\d+)%\s+(\S+)\s+(\S+)",
)


def _dir_size(root: str, excludes: list[str]) -> int:
    """Walk *root* and return total bytes, skipping *excludes* patterns."""
    total = 0
    root_path = Path(root).resolve()
    for dirpath, dirnames, filenames in os.walk(root_path):
        # Prune excluded directories in-place
        dirnames[:] = [
            d for d in dirnames
            if not any(fnmatch.fnmatch(d, pat) for pat in excludes)
        ]
        for f in filenames:
            if any(fnmatch.fnmatch(f, pat) for pat in excludes):
                continue
            try:
                total += (Path(dirpath) / f).stat().st_size
            except OSError:
                pass
    return total


def _fmt_size(nbytes: int) -> str:
    """Human-readable size string."""
    for unit in ("B", "KB", "MB", "GB"):
        if nbytes < 1024:
            return f"{nbytes:.1f} {unit}"
        nbytes /= 1024
    return f"{nbytes:.1f} TB"


class DataSync:
    """Transfer files to/from a remote host over SSH."""

    def __init__(self, host: str, pem_path: str, user: str = "ubuntu") -> None:
        self.host = host
        self.pem_path = pem_path
        self.user = user
        self.ssh_opts = [
            "-i", pem_path,
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", f"UserKnownHostsFile={_KNOWN_HOSTS}",
            "-o", "ServerAliveInterval=15",
            "-o", "ServerAliveCountMax=3",
        ]
        self.remote = f"{user}@{host}"

    def _ssh_cmd_str(self) -> str:
        """Build an SSH command string for rsync -e, properly quoting paths."""
        return "ssh " + " ".join(shlex.quote(opt) for opt in self.ssh_opts)

    def rsync_to(self, local_path: str, remote_path: str,
                 quiet: bool = False) -> None:
        """Rsync a local path to the remote host."""
        cmd = [
            "rsync", "-az",
            "-e", self._ssh_cmd_str(),
            local_path,
            f"{self.remote}:{remote_path}",
        ]
        if quiet:
            subprocess.run(cmd, check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            console.print(f"[dim]rsync {local_path} -> {remote_path}[/dim]")
            cmd.insert(2, "--info=progress2")
            subprocess.run(cmd, check=True)

    def rsync_project(
        self,
        local_root: str,
        remote_root: str,
        excludes: list[str] | None = None,
        quiet: bool = False,
        n_instances: int = 1,
    ) -> None:
        """Rsync an entire project directory with a progress bar.

        Args:
            local_root: Local directory to sync.
            remote_root: Remote destination path.
            excludes: Glob patterns to exclude.
            quiet: If True, suppress all output.
            n_instances: Total instances being synced to (for display only).
        """
        excludes = list(excludes) if excludes is not None else list(DEFAULT_EXCLUDES)

        cmd = [
            "rsync", "-az", "--info=progress2",
            "-e", self._ssh_cmd_str(),
        ]
        for exc in excludes:
            cmd.extend(["--exclude", exc])
        # Ensure trailing slash so contents are synced into remote_root
        local = local_root.rstrip("/") + "/"
        cmd.extend([local, f"{self.remote}:{remote_root}"])

        if quiet:
            quiet_cmd = [c for c in cmd if c != "--info=progress2"]
            subprocess.run(quiet_cmd, check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return

        # Calculate total transfer size for the progress bar
        total_bytes = _dir_size(local_root, excludes)
        total_display = total_bytes * n_instances

        if n_instances > 1:
            desc = f"Syncing project ({n_instances} instances x {_fmt_size(total_bytes)})"
        else:
            desc = f"Syncing project ({_fmt_size(total_bytes)})"

        with Progress(
            TextColumn("[bold blue]{task.description}"),
            BarColumn(),
            DownloadColumn(),
            TransferSpeedColumn(),
            console=console,
        ) as progress:
            task = progress.add_task(desc, total=total_display)

            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )

            last_bytes = 0
            for line in proc.stdout:
                m = _PROGRESS2_RE.match(line.strip())
                if m:
                    transferred = int(m.group(1).replace(",", ""))
                    if transferred > last_bytes:
                        progress.update(task, advance=transferred - last_bytes)
                        last_bytes = transferred

            proc.wait()
            if proc.returncode != 0:
                raise subprocess.CalledProcessError(proc.returncode, cmd)

            # Ensure bar completes (rsync may not report 100% exactly)
            progress.update(task, completed=total_display)

    def scp_to(self, local_path: str, remote_path: str) -> None:
        """SCP a single file to the remote host."""
        cmd = [
            "scp",
            *self.ssh_opts,
            local_path,
            f"{self.remote}:{remote_path}",
        ]
        subprocess.run(cmd, check=True)

    def ssh_run(
        self, command: str, capture: bool = False, quiet: bool = False,
        stop_event=None,
    ) -> int | str:
        """Run a command on the remote host via SSH.

        If capture=True, return stdout as a string.
        If quiet=True, suppress all output and just return exit code.
        If stop_event is provided (threading.Event), poll it during quiet execution
        so the caller can interrupt a long-running SSH command.
        Otherwise, stream to terminal and return the exit code.
        """
        cmd = [
            "ssh",
            *self.ssh_opts,
            self.remote,
            command,
        ]
        if capture:
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                raise subprocess.CalledProcessError(
                    result.returncode, cmd, result.stdout, result.stderr
                )
            return result.stdout
        if quiet:
            if stop_event is not None:
                proc = subprocess.Popen(
                    cmd, stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                while proc.poll() is None:
                    if stop_event.is_set():
                        proc.terminate()
                        try:
                            proc.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            proc.kill()
                            proc.wait()
                        return -1
                    import time
                    time.sleep(1)
                return proc.returncode
            result = subprocess.run(
                cmd, stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            return result.returncode
        # Force PTY allocation so remote Rich progress bars render properly.
        # Pass local terminal width so remote Rich renders at the right size.
        try:
            cols = os.get_terminal_size().columns
        except OSError:
            cols = 120
        wrapped = f"export COLUMNS={cols}; {command}"
        interactive_cmd = ["ssh", "-t", *self.ssh_opts, self.remote, wrapped]
        result = subprocess.run(interactive_cmd)
        return result.returncode

    def ssh_interactive(self) -> None:
        """Replace this process with an interactive SSH session."""
        args = ["ssh", *self.ssh_opts, self.remote]
        os.execvp("ssh", args)
