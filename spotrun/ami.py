"""Custom AMI creation for spotrun."""

from __future__ import annotations

import time
from pathlib import Path

from rich.console import Console

from spotrun.ec2 import EC2Manager
from spotrun.sync import DataSync

console = Console()

# Bootstrap script bundled inside the package
_BUNDLED_BOOTSTRAP = Path(__file__).resolve().parent / "scripts" / "bootstrap.sh"
# Fallback: development repo layout
_REPO_BOOTSTRAP = Path(__file__).resolve().parent.parent / "scripts" / "bootstrap.sh"


def _default_bootstrap_path() -> str | None:
    """Find the default bootstrap script, checking package-local then repo layout."""
    if _BUNDLED_BOOTSTRAP.exists():
        return str(_BUNDLED_BOOTSTRAP)
    if _REPO_BOOTSTRAP.exists():
        return str(_REPO_BOOTSTRAP)
    return None


class AMIManager:
    """Build and find custom AMIs for spotrun workers."""

    def __init__(self, ec2: EC2Manager) -> None:
        self.ec2 = ec2

    def find_existing(self, project_tag: str = "spotrun", arch: str = "x86_64") -> str | None:
        """Find the most recent AMI tagged with the project tag and matching arch."""
        resp = self.ec2.client.describe_images(
            Owners=["self"],
            Filters=[
                {"Name": "tag:Project", "Values": [project_tag]},
                {"Name": "architecture", "Values": [arch]},
                {"Name": "state", "Values": ["available"]},
            ],
        )
        images = resp.get("Images", [])
        if not images:
            return None
        images.sort(key=lambda i: i["CreationDate"], reverse=True)
        return images[0]["ImageId"]

    def create(
        self,
        key_name: str,
        pem_path: str,
        sg_id: str,
        bootstrap_script: str | None = None,
        requirements_file: str | None = None,
        project_tag: str = "spotrun",
        arch: str = "x86_64",
    ) -> str:
        """Build a custom AMI from Ubuntu base.

        1. Launch a builder instance (t3.medium for x86, t4g.medium for arm64)
        2. SCP and run the bootstrap script
        3. Create an AMI from the configured instance
        4. Clean up the builder
        """
        base_ami = self.ec2.get_ubuntu_ami(arch=arch)
        console.print(f"Base AMI: [bold]{base_ami}[/bold] ({arch})")

        # Launch builder (t4g for ARM, t3 for x86)
        builder_type = "t4g.medium" if arch == "arm64" else "t3.medium"
        with console.status(f"Launching {builder_type} builder instance..."):
            builder_id = self.ec2.request_spot_instance(
                instance_type=builder_type,
                ami_id=base_ami,
                key_name=key_name,
                sg_id=sg_id,
                project_tag=project_tag,
            )
            console.print(f"Builder instance: [bold]{builder_id}[/bold]")

        try:
            with console.status("Waiting for builder to start..."):
                ip = self.ec2.wait_for_running(builder_id)
                console.print(f"Builder IP: [bold]{ip}[/bold]")

            with console.status("Waiting for SSH..."):
                self.ec2.wait_for_ssh(ip)

            sync = DataSync(ip, pem_path)

            # Determine bootstrap script
            script_path = bootstrap_script or _default_bootstrap_path()
            if not script_path:
                raise FileNotFoundError(
                    "No bootstrap script found. Provide one via bootstrap_script argument "
                    "or ensure scripts/bootstrap.sh exists in the spotrun package."
                )

            with console.status("Running bootstrap script..."):
                sync.scp_to(script_path, "/tmp/bootstrap.sh")
                if requirements_file:
                    import os
                    sync.ssh_run("sudo mkdir -p /opt/project", capture=True)
                    sync.ssh_run("sudo chown ubuntu:ubuntu /opt/project", capture=True)
                    remote_name = os.path.basename(requirements_file)
                    sync.scp_to(requirements_file, f"/opt/project/{remote_name}")
                exit_code = sync.ssh_run("chmod +x /tmp/bootstrap.sh && /tmp/bootstrap.sh")
                if exit_code != 0:
                    raise RuntimeError(f"Bootstrap script failed with exit code {exit_code}")

            # Create AMI
            timestamp = int(time.time())
            ami_name = f"{project_tag}-base-{timestamp}"

            with console.status("Creating AMI (this takes a few minutes)..."):
                resp = self.ec2.client.create_image(
                    InstanceId=builder_id,
                    Name=ami_name,
                    Description=f"{project_tag} base image built at {timestamp}",
                )
                ami_id = resp["ImageId"]
                console.print(f"AMI: [bold]{ami_id}[/bold] ({ami_name})")

                waiter = self.ec2.client.get_waiter("image_available")
                waiter.wait(
                    ImageIds=[ami_id],
                    WaiterConfig={"Delay": 15, "MaxAttempts": 80},
                )

            # Tag the AMI
            self.ec2.client.create_tags(
                Resources=[ami_id],
                Tags=[{"Key": "Project", "Value": project_tag}],
            )

            console.print(f"[green]AMI [bold]{ami_id}[/bold] ready[/green]")
            return ami_id
        finally:
            self.ec2.terminate_instance(builder_id)
