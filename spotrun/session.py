"""Session -- the main user-facing orchestrator."""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from spotrun.ami import AMIManager
from spotrun.ec2 import EC2Manager, find_cheapest_region
from spotrun.pricing import all_instance_types, select_instance
from spotrun.sync import DataSync

console = Console()
STATE_FILE = Path.home() / ".spotrun" / "state.json"


class Session:
    """Orchestrates the full lifecycle: launch, sync, run, teardown."""

    def __init__(
        self,
        workers: int = 4,
        region: str | None = None,
        project_tag: str = "spotrun",
        bootstrap_script: str | None = None,
        requirements_file: str | None = None,
        include_arm: bool = False,
        no_hyperthreading: bool = False,
        save_state: bool = True,
        auto_install: bool = True,
    ) -> None:
        self.workers = workers
        self.project_tag = project_tag
        self._save_state_enabled = save_state
        self.bootstrap_script = bootstrap_script
        self.requirements_file = requirements_file
        self.include_arm = include_arm
        self.no_hyperthreading = no_hyperthreading
        self.auto_install = auto_install

        # Auto-select cheapest region if none specified
        if region is None and "AWS_REGION" not in os.environ:
            instance_type, _ = select_instance(workers, include_arm=include_arm)
            region, _ = find_cheapest_region(instance_type)

        self.ec2 = EC2Manager(region=region)
        self.ami_mgr = AMIManager(self.ec2)
        self._sync: DataSync | None = None
        self._instance_id: str | None = None
        self._ip: str | None = None
        self._pem_path: str | None = None

    def launch(
        self,
        bootstrap_script: str | None = None,
        requirements_file: str | None = None,
        idle_timeout: int = 300,
    ) -> str:
        """Provision infrastructure and launch a spot instance. Returns public IP.

        Args:
            bootstrap_script: Path to a shell script to run during AMI build.
            requirements_file: Path to requirements.txt to pre-install in AMI.
            idle_timeout: Seconds of inactivity before auto-terminating the
                instance.  Inactivity means no active SSH connections (including
                non-PTY / quiet-mode sessions).  Defaults to 300 (5 minutes).
                Set to 0 to disable.
        """
        bootstrap_script = bootstrap_script or self.bootstrap_script
        requirements_file = requirements_file or self.requirements_file

        from spotrun.pricing import instance_arch

        # 1. Ensure infra
        key_name, pem_path, sg_id = self.ec2.ensure_infra(self.project_tag)
        self._pem_path = pem_path

        # 2. Spot prices and instance selection (cheapest that fits)
        prices = self.ec2.get_spot_prices(all_instance_types(include_arm=self.include_arm))
        instance_type, vcpus = select_instance(self.workers, prices=prices, include_arm=self.include_arm)
        arch = instance_arch(instance_type)
        spot_price = prices.get(instance_type)
        self._show_pricing(instance_type, vcpus, spot_price, prices)

        # 3. Find or create AMI (must match selected architecture)
        ami_id = self.ami_mgr.find_existing(self.project_tag, arch=arch)
        if ami_id:
            console.print(f"Using existing AMI: [bold]{ami_id}[/bold] ({arch})")
        else:
            console.print(f"No existing AMI found for {arch}, building one...")
            ami_id = self.ami_mgr.create(
                key_name, pem_path, sg_id,
                bootstrap_script=bootstrap_script,
                requirements_file=requirements_file,
                project_tag=self.project_tag,
                arch=arch,
            )

        # 5. Request spot instance
        # Graviton/ARM has no hyperthreading — skip CpuOptions for ARM
        if self.no_hyperthreading and arch != "arm64":
            threads_per_core = 1
            core_count = vcpus // 2  # x86: 2 threads per core by default
        else:
            threads_per_core = None
            core_count = None
        with console.status(f"Requesting [bold]{instance_type}[/bold] spot instance..."):
            self._instance_id = self.ec2.request_spot_instance(
                instance_type=instance_type,
                ami_id=ami_id,
                key_name=key_name,
                sg_id=sg_id,
                project_tag=self.project_tag,
                threads_per_core=threads_per_core,
                core_count=core_count,
            )
            console.print(f"Instance: [bold]{self._instance_id}[/bold]")

        # Save state early so `spotrun teardown` can find orphaned instances
        self._save_state(key_name, sg_id)

        # 6. Wait for running + SSH (clean up instance on failure)
        try:
            with console.status("Waiting for instance to start..."):
                self._ip = self.ec2.wait_for_running(self._instance_id)
                console.print(f"Public IP: [bold]{self._ip}[/bold]")

            with console.status("Waiting for SSH..."):
                self.ec2.wait_for_ssh(self._ip)
        except Exception:
            try:
                self.teardown()
            except Exception:
                pass  # Original exception takes priority
            raise

        self._sync = DataSync(self._ip, pem_path)

        # Update state with IP now that instance is running
        self._save_state(key_name, sg_id)

        # Install idle watchdog (on by default — shuts down after inactivity)
        if idle_timeout and idle_timeout > 0:
            self._install_idle_watchdog(idle_timeout)

        console.print("[green bold]Instance ready.[/green bold]")
        return self._ip

    def sync(self, paths: list[str], remote_base: str = "/opt/project",
             quiet: bool = False) -> None:
        """Rsync individual paths to the remote instance."""
        if not self._sync:
            raise RuntimeError("No active session. Call launch() first.")
        for path in paths:
            self._sync.rsync_to(path, remote_base, quiet=quiet)

    def sync_project(
        self,
        local_root: str = ".",
        remote_root: str = "/opt/project",
        excludes: list[str] | None = None,
        quiet: bool = False,
        n_instances: int = 1,
    ) -> None:
        """Rsync an entire project directory."""
        if not self._sync:
            raise RuntimeError("No active session. Call launch() first.")
        self._sync.rsync_project(
            local_root, remote_root, excludes=excludes,
            quiet=quiet, n_instances=n_instances,
        )

    def install_deps(self, remote_root: str = "/opt/project") -> bool:
        """Detect and install Python dependencies on the remote instance.

        Checks for requirements.txt or pyproject.toml in the remote project
        directory and installs into the existing venv.

        Returns True if dependencies were installed, False if skipped.
        """
        if not self._sync:
            raise RuntimeError("No active session. Call launch() first.")

        # Guard: skip if no venv exists (e.g. custom bootstrap that skipped it)
        venv_check = self._sync.ssh_run(
            f"test -d {remote_root}/.venv/bin", quiet=True,
        )
        if venv_check != 0:
            return False

        # Detect which deps file exists (single SSH round-trip)
        detect_cmd = (
            f"test -f {remote_root}/requirements.txt && echo requirements "
            f"|| (test -f {remote_root}/pyproject.toml && echo pyproject "
            f"|| echo none)"
        )
        try:
            result = self._sync.ssh_run(detect_cmd, capture=True)
        except subprocess.CalledProcessError:
            console.print("[yellow]Warning: could not detect dependency files[/yellow]")
            return False
        if not isinstance(result, str):
            return False
        deps_type = result.strip()

        if deps_type == "none":
            return False

        venv_pip = f"{remote_root}/.venv/bin/pip"
        venv_python = f"{remote_root}/.venv/bin/python"

        if deps_type == "requirements":
            console.print("[dim]Installing dependencies from requirements.txt...[/dim]")
            install_cmd = f"{venv_pip} install --quiet -r {remote_root}/requirements.txt"
        elif deps_type == "pyproject":
            console.print("[dim]Installing dependencies from pyproject.toml...[/dim]")
            install_cmd = (
                f"{venv_python} << 'PYEOF'\n"
                "import subprocess, sys\n"
                "try:\n"
                "    import tomllib\n"
                "except ImportError:\n"
                '    subprocess.check_call([sys.executable, "-m", "pip", "install", "--quiet", "tomli"])\n'
                "    import tomli as tomllib\n"
                f'with open("{remote_root}/pyproject.toml", "rb") as f:\n'
                '    deps = tomllib.load(f).get("project", {}).get("dependencies", [])\n'
                "if deps:\n"
                '    subprocess.check_call([sys.executable, "-m", "pip", "install", "--quiet"] + deps)\n'
                '    print(f"Installed {len(deps)} dependencies from pyproject.toml")\n'
                "else:\n"
                '    print("No dependencies found in pyproject.toml")\n'
                "PYEOF"
            )
        else:
            return False

        exit_code = self._sync.ssh_run(install_cmd)
        if not isinstance(exit_code, int):
            exit_code = -1
        if exit_code != 0:
            console.print("[yellow]Warning: dependency installation returned non-zero exit code[/yellow]")
        return True

    def run(self, command: str, quiet: bool = False, stop_event=None,
            activate_venv: bool = True, remote_root: str = "/opt/project") -> int:
        """Run a command on the remote instance. Returns exit code.

        Args:
            command: Shell command to execute.
            quiet: If True, suppress all output.
            stop_event: threading.Event to interrupt long-running commands.
            activate_venv: If True (default), activate the project venv before
                running the command. Safe even if no venv exists.
            remote_root: Project root on the remote instance.
        """
        if not self._sync:
            raise RuntimeError("No active session. Call launch() first.")
        if activate_venv:
            activate = f"{remote_root}/.venv/bin/activate"
            command = (
                f"if [ -f {activate} ]; then source {activate}; fi && "
                f"({command})"
            )
        result = self._sync.ssh_run(command, quiet=quiet, stop_event=stop_event)
        if not isinstance(result, int):
            return -1
        return result

    def ssh(self) -> None:
        """Drop into an interactive SSH session (replaces this process)."""
        if not self._sync:
            raise RuntimeError("No active session. Call launch() first.")
        self._sync.ssh_interactive()

    def teardown(self) -> None:
        """Terminate the instance and clean up state."""
        if self._instance_id:
            self.ec2.terminate_instance(self._instance_id)
            self._instance_id = None
            self._ip = None
            self._sync = None
        self._clear_state()

    def get_pricing_info(self) -> dict:
        """Return pricing info without launching anything."""
        prices = self.ec2.get_spot_prices(all_instance_types(include_arm=self.include_arm))
        instance_type, vcpus = select_instance(self.workers, prices=prices, include_arm=self.include_arm)
        spot_price = prices.get(instance_type)
        return {
            "instance_type": instance_type,
            "vcpus": vcpus,
            "spot_price": spot_price,
            "all_prices": prices,
        }

    # -- Context manager --

    def __enter__(self) -> Session:
        self.launch()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.teardown()

    # -- Internal --

    def _install_idle_watchdog(self, timeout_seconds: int) -> None:
        """Install a background watchdog that shuts down the instance after
        *timeout_seconds* of inactivity.

        Inactivity is defined as: no active SSH connections to the instance.
        This covers both interactive (PTY) and non-interactive (quiet/command)
        sessions — any ``sshd`` child process with ``@`` in its name indicates
        an active connection.
        """
        self.run(
            "nohup bash -c '"
            f"while true; do sleep {timeout_seconds}; "
            'if ! pgrep -af "sshd:.*@" > /dev/null; then '
            "sudo shutdown -h now; "
            "fi; done"
            "' >/dev/null 2>&1 &",
            quiet=True,
            activate_venv=False,
        )

    def _show_pricing(
        self,
        instance_type: str,
        vcpus: int,
        spot_price: float | None,
        all_prices: dict[str, float],
    ) -> None:
        table = Table(title="Spot Prices", show_header=True)
        table.add_column("Instance", style="cyan")
        table.add_column("vCPUs", justify="right")
        table.add_column("$/hr", justify="right", style="green")
        from spotrun.pricing import COMPUTE_INSTANCES
        for itype, vcpu_count, arch in COMPUTE_INSTANCES:
            if itype not in all_prices:
                continue
            price = all_prices[itype]
            price_str = f"${price:.4f}"
            marker = " <--" if itype == instance_type else ""
            table.add_row(itype, str(vcpu_count), price_str + marker)
        console.print(table)
        if spot_price is not None and spot_price > 0:
            console.print(
                Panel(
                    f"[bold]{instance_type}[/bold] ({vcpus} vCPUs) @ "
                    f"[green]${spot_price:.4f}/hr[/green]",
                    title="Selected Instance",
                )
            )

    def _save_state(self, key_name: str, sg_id: str) -> None:
        if not self._save_state_enabled:
            return
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        state = {
            "instance_id": self._instance_id,
            "ip": self._ip,
            "region": self.ec2.region,
            "pem_path": self._pem_path,
            "key_name": key_name,
            "sg_id": sg_id,
        }
        content = json.dumps(state, indent=2)
        fd = os.open(
            str(STATE_FILE),
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
            stat.S_IRUSR | stat.S_IWUSR,
        )
        with os.fdopen(fd, "w") as f:
            f.write(content)

    def _clear_state(self) -> None:
        if not self._save_state_enabled:
            return
        Session.clear_state_file()

    @staticmethod
    def clear_state_file() -> None:
        """Remove the state file from disk (static — usable without an instance)."""
        if STATE_FILE.exists():
            STATE_FILE.unlink()

    @staticmethod
    def load_state() -> dict | None:
        """Load saved state from disk, or None if no state file."""
        if STATE_FILE.exists():
            return json.loads(STATE_FILE.read_text())
        return None
