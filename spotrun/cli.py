"""spotrun CLI -- powered by Typer."""

from __future__ import annotations

from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from spotrun.pricing import COMPUTE_INSTANCES, all_instance_types, select_instance
from spotrun.session import Session

app = typer.Typer(
    name="spotrun",
    help="Burst compute to AWS spot instances. Zero config, one command.",
    add_completion=False,
)
console = Console()


@app.command()
def launch(
    workers: int = typer.Option(4, "--workers", "-w", min=1, help="Number of parallel workers"),
    sync: Optional[list[str]] = typer.Option(None, "--sync", "-s", help="Paths to sync to the instance"),
    command: Optional[str] = typer.Argument(None, help="Command to run on the instance"),
    ssh: bool = typer.Option(False, "--ssh", help="Drop into an interactive SSH session"),
    project_tag: str = typer.Option("spotrun", "--tag", help="Project tag for AWS resources"),
    bootstrap: Optional[str] = typer.Option(None, "--bootstrap", help="Path to custom bootstrap script"),
    requirements: Optional[str] = typer.Option(None, "--requirements", "-r", help="Path to requirements.txt"),
    arm: bool = typer.Option(False, "--arm", help="Include ARM/Graviton instances (cheaper but may have compatibility issues)"),
    no_ht: bool = typer.Option(False, "--no-ht", help="Disable hyperthreading (1 thread per core). Best for CPU-bound single-threaded workloads like Python multiprocessing"),
) -> None:
    """Launch a spot instance, optionally sync files and run a command."""
    session = Session(
        workers=workers,
        project_tag=project_tag,
        bootstrap_script=bootstrap,
        requirements_file=requirements,
        include_arm=arm,
        no_hyperthreading=no_ht,
    )
    should_teardown = True

    try:
        session.launch()

        if sync:
            session.sync(sync)

        if ssh:
            console.print("[bold]Entering SSH session. Run 'spotrun teardown' when done.[/bold]")
            session.ssh()
            # ssh_interactive replaces the process via execvp, so teardown won't run.
            # If ssh returns (shouldn't), fall through to teardown.
        elif command:
            exit_code = session.run(command)
            if exit_code != 0:
                console.print(f"[red]Command exited with code {exit_code}[/red]")
                raise typer.Exit(code=exit_code)
            else:
                console.print("[green]Command completed successfully.[/green]")
        else:
            # No command, no SSH -- keep instance running for manual use
            should_teardown = False
            state = Session.load_state()
            if state:
                console.print(f"Instance running at [bold]{state['ip']}[/bold]")
                console.print("Run [bold]spotrun teardown[/bold] when done.")
    except typer.Exit:
        raise
    except Exception as e:
        console.print(f"[red bold]Error:[/red bold] {e}")
        should_teardown = True  # Always clean up on error
        raise typer.Exit(code=1)
    finally:
        if should_teardown:
            try:
                session.teardown()
            except Exception as e:
                console.print(f"[red]Teardown error:[/red] {e}")


@app.command()
def setup(
    rebuild_ami: bool = typer.Option(False, "--rebuild-ami", help="Force rebuild the base AMI"),
    bootstrap: Optional[str] = typer.Option(None, "--bootstrap", help="Path to custom bootstrap script"),
    requirements: Optional[str] = typer.Option(None, "--requirements", "-r", help="Path to requirements.txt"),
    project_tag: str = typer.Option("spotrun", "--tag", help="Project tag"),
    region: Optional[str] = typer.Option(None, "--region", help="AWS region (default: auto-select or AWS_REGION)"),
    arm: bool = typer.Option(False, "--arm", help="Build ARM/Graviton AMI instead of x86"),
) -> None:
    """Set up infrastructure and build the base AMI."""
    from spotrun.ami import AMIManager
    from spotrun.ec2 import EC2Manager

    arch = "arm64" if arm else "x86_64"
    ec2 = EC2Manager(region=region)
    key_name, pem_path, sg_id = ec2.ensure_infra(project_tag)
    console.print(f"[green]Infrastructure ready in [bold]{ec2.region}[/bold].[/green]")

    ami_mgr = AMIManager(ec2)
    existing = ami_mgr.find_existing(project_tag, arch=arch)

    if existing and not rebuild_ami:
        console.print(f"AMI already exists: [bold]{existing}[/bold] ({arch})")
        console.print("Use [bold]--rebuild-ami[/bold] to force a rebuild.")
        return

    ami_id = ami_mgr.create(
        key_name, pem_path, sg_id,
        bootstrap_script=bootstrap,
        requirements_file=requirements,
        project_tag=project_tag,
        arch=arch,
    )
    console.print(f"[green bold]AMI ready: {ami_id} ({arch})[/green bold]")


@app.command()
def prices(
    workers: int = typer.Option(4, "--workers", "-w", min=1, help="Number of parallel workers"),
    arm: bool = typer.Option(False, "--arm", help="Include ARM/Graviton instances"),
) -> None:
    """Show current spot prices for compute instances."""
    import os

    from botocore.exceptions import ClientError

    from spotrun.ec2 import CANDIDATE_REGIONS, EC2Manager

    instance_type, vcpus = select_instance(workers, include_arm=arm)
    explicit_region = os.environ.get("AWS_REGION")
    region_prices: list[tuple[str, float]] = []

    if explicit_region:
        # Single-region view (explicit region set)
        ec2 = EC2Manager(region=explicit_region)
        spot_prices = ec2.get_spot_prices(all_instance_types(include_arm=arm))

        table = Table(title=f"Spot Prices ({explicit_region})", show_header=True)
        table.add_column("Instance", style="cyan")
        table.add_column("Arch", style="dim")
        table.add_column("vCPUs", justify="right")
        table.add_column("$/hr", justify="right", style="green")
        table.add_column("", style="bold yellow")

        for itype, vcpu_count, iarch in COMPUTE_INSTANCES:
            if not arm and iarch == "arm64":
                continue
            price = spot_prices.get(itype)
            price_str = f"${price:.4f}" if price is not None else "n/a"
            marker = "<-- selected" if itype == instance_type else ""
            table.add_row(itype, iarch, str(vcpu_count), price_str, marker)

        console.print(table)
        price = spot_prices.get(instance_type)
        if price is not None:
            console.print(f"Estimated cost: [green]${price:.4f}/hr[/green]")
    else:
        # Cross-region view (auto-select cheapest)
        with console.status("Checking spot prices across regions..."):
            for region in CANDIDATE_REGIONS:
                try:
                    client = EC2Manager(region=region)
                    rp = client.get_spot_prices([instance_type])
                    price = rp.get(instance_type)
                    if price is not None:
                        region_prices.append((region, price))
                except ClientError as e:
                    code = e.response["Error"]["Code"]
                    if code in (
                        "AuthFailure", "UnauthorizedOperation",
                        "InvalidClientTokenId", "ExpiredToken",
                    ):
                        raise
                    continue
                except Exception:
                    continue

        if not region_prices:
            console.print("[yellow]No pricing data available for any region.[/yellow]")
        else:
            region_prices.sort(key=lambda x: x[1])

            table = Table(
                title=f"Spot Prices for {instance_type} ({vcpus} vCPUs)",
                show_header=True,
            )
            table.add_column("Region", style="cyan")
            table.add_column("$/hr", justify="right", style="green")
            table.add_column("", style="bold yellow")

            for i, (region, price) in enumerate(region_prices):
                marker = "<-- cheapest" if i == 0 else ""
                table.add_row(region, f"${price:.4f}", marker)

            console.print(table)

    console.print(
        f"\n[bold]{workers}[/bold] workers -> [bold]{instance_type}[/bold] "
        f"({vcpus} vCPUs)"
    )
    if not explicit_region and region_prices:
        cheapest_region, cheapest_price = region_prices[0]
        console.print(
            f"Cheapest: [bold]{cheapest_region}[/bold] at "
            f"[green]${cheapest_price:.4f}/hr[/green]"
        )


@app.command()
def teardown() -> None:
    """Terminate the running spot instance."""
    state = Session.load_state()
    if not state:
        console.print("[yellow]No active instance found.[/yellow]")
        return

    instance_id = state.get("instance_id")
    region = state.get("region")
    if not instance_id:
        console.print("[yellow]State file is incomplete. Clearing.[/yellow]")
        Session._clear_state()
        return

    from spotrun.ec2 import EC2Manager

    try:
        ec2 = EC2Manager(region=region)
        ec2.terminate_instance(instance_id)
    except Exception as e:
        console.print(f"[yellow]Teardown warning: {e}[/yellow]")

    Session._clear_state()
    console.print("[green]Teardown complete.[/green]")


if __name__ == "__main__":
    app()
