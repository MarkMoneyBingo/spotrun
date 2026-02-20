"""File synchronization over SSH/rsync."""

from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path

from rich.console import Console

console = Console()

DEFAULT_EXCLUDES = (".venv", "__pycache__", ".git", "*.pyc")

_KNOWN_HOSTS = str(Path.home() / ".spotrun" / "known_hosts")


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

    def rsync_to(self, local_path: str, remote_path: str) -> None:
        """Rsync a local path to the remote host."""
        cmd = [
            "rsync", "-avz", "--progress",
            "-e", self._ssh_cmd_str(),
            local_path,
            f"{self.remote}:{remote_path}",
        ]
        console.print(f"[dim]rsync {local_path} -> {remote_path}[/dim]")
        subprocess.run(cmd, check=True)

    def rsync_project(
        self,
        local_root: str,
        remote_root: str,
        excludes: list[str] | None = None,
    ) -> None:
        """Rsync an entire project directory with sensible defaults."""
        excludes = list(excludes) if excludes is not None else list(DEFAULT_EXCLUDES)
        cmd = [
            "rsync", "-avz", "--progress",
            "-e", self._ssh_cmd_str(),
        ]
        for exc in excludes:
            cmd.extend(["--exclude", exc])
        # Ensure trailing slash so contents are synced into remote_root
        local = local_root.rstrip("/") + "/"
        cmd.extend([local, f"{self.remote}:{remote_root}"])
        console.print(f"[dim]rsync project {local_root} -> {remote_root}[/dim]")
        subprocess.run(cmd, check=True)

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
