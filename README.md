# spotrun

Burst compute jobs to AWS spot instances. Zero config, one command.

**The problem:** You have a CPU-bound workload (ML training, optimization, simulation) that takes hours on your laptop. A cloud machine with 64 vCPUs would finish in minutes, but setting one up means clicking through the AWS console, configuring SSH keys, syncing files, and remembering to tear it down so you don't get a surprise bill.

**spotrun** automates the entire lifecycle: provision infrastructure, build an AMI, launch a spot instance, sync your files, run your command, and tear everything down. It even picks the cheapest AWS region automatically. Set 2 environment variables, run one command.

## Install

```bash
pip install spotrun
```

Requires Python 3.10+ and an AWS account.

## Prerequisites

- **AWS account** with an access key that has EC2 permissions
- **rsync** and **ssh** installed locally (pre-installed on macOS/Linux)
- No AWS console interaction required -- spotrun creates everything it needs

## Quick Start

### 1. Set your AWS credentials

```bash
export AWS_ACCESS_KEY_ID=AKIA...
export AWS_SECRET_ACCESS_KEY=...
```

That's it -- just 2 env vars. spotrun automatically finds the cheapest AWS region for your instance type. Set `AWS_REGION` to override if you prefer a specific region.

You can also use a `.env` file or AWS credentials file -- boto3 reads credentials from all standard sources.

### 2. Run your workload

```bash
spotrun launch --workers 8 --sync ./data --sync ./src "python train.py"
```

That's it. spotrun will:

1. **Find the cheapest region** by querying spot prices across 8 AWS regions
2. Create a key pair and security group (first run only, reused after)
3. Build a custom AMI with Python, venv, and common packages (first run only)
4. Launch a spot instance sized for your worker count
5. Rsync your files to the instance
6. Run your command over SSH, streaming output live
7. Terminate the instance when done

You only pay for the minutes you use, at spot prices (typically 60-90% cheaper than on-demand).

## CLI

```bash
# Launch, sync files, run a command, auto-teardown on completion
spotrun launch --workers 8 --sync data/ --sync src/ "python train.py"

# Launch and drop into an interactive SSH session
spotrun launch --workers 4 --ssh

# Check current spot prices for your workload size
spotrun prices --workers 8

# Pre-build the base AMI (saves time on first launch)
spotrun setup --rebuild-ami

# Manually tear down a running instance
spotrun teardown
```

### Options

| Flag | Description |
|------|-------------|
| `--workers, -w` | Number of parallel workers (default: 4). Determines instance size. |
| `--sync, -s` | Local paths to rsync to `/opt/project` on the instance. Repeatable. |
| `--ssh` | Drop into an interactive SSH session instead of running a command. |
| `--arm` | Include ARM/Graviton instances (often 20-40% cheaper, see below). |
| `--tag` | Project tag for AWS resources (default: `spotrun`). |
| `--bootstrap` | Path to a custom bootstrap script (replaces the default). |
| `--requirements, -r` | Path to `requirements.txt` to pre-install in the AMI. |

## Python API

Use spotrun as a library for programmatic control:

```python
from spotrun import Session

# Context manager -- instance auto-terminates on exit
with Session(workers=8) as s:
    s.sync_project(".")
    s.run("cd /opt/project && python train.py")
```

With more control:

```python
from spotrun import Session, select_instance, estimate_cost

# Check what instance you'd get
instance_type, vcpus = select_instance(workers=8)
print(f"Will use {instance_type} ({vcpus} vCPUs)")

# Manual lifecycle
s = Session(workers=8)
ip = s.launch()
s.sync_project(".", excludes=[".git", "__pycache__", "*.pyc"])
s.sync(["data/model.pt", ".env"])
exit_code = s.run("cd /opt/project && python train.py --workers 8")
s.teardown()
```

### Session API

| Method | Description |
|--------|-------------|
| `Session(workers, region, project_tag, include_arm)` | Create a session. `workers` determines instance size. `include_arm=True` enables ARM/Graviton. |
| `.launch()` | Provision infra, find/build AMI, launch spot instance. Returns public IP. |
| `.sync(paths)` | Rsync individual files/directories to the instance. |
| `.sync_project(root, excludes)` | Rsync an entire project directory with sensible defaults. |
| `.run(command)` | Run a shell command on the instance via SSH. Returns exit code. |
| `.ssh()` | Open an interactive SSH session (replaces current process). |
| `.teardown()` | Terminate the instance and clean up state. |
| `.get_pricing_info()` | Return pricing info without launching anything. |

## Instance Sizing

spotrun picks the smallest instance that fits your workload using the formula:

```
vCPUs needed = workers * 2 + 2
```

The `* 2` accounts for hyperthreading (2 vCPUs per physical core), and `+ 2` reserves capacity for the OS and SSH.

**Default (x86_64):**

| Instance | vCPUs | Max Workers |
|----------|-------|-------------|
| c6a.xlarge | 4 | 1 |
| c6a.2xlarge | 8 | 3 |
| c6a.4xlarge | 16 | 7 |
| c6a.8xlarge | 32 | 15 |
| c6a.12xlarge | 48 | 23 |
| c6a.16xlarge | 64 | 31 |

**With `--arm` (ARM/Graviton):**

| Instance | vCPUs | Max Workers |
|----------|-------|-------------|
| c6g.xlarge | 4 | 1 |
| c6g.2xlarge | 8 | 3 |
| c6g.4xlarge | 16 | 7 |
| c6g.8xlarge / c7g.8xlarge | 32 | 15 |
| c6g.12xlarge | 48 | 23 |
| c6g.16xlarge | 64 | 31 |

Use `spotrun prices --workers N` to see current spot prices, or `spotrun prices --workers N --arm` to compare with ARM instances.

## ARM/Graviton Instances

ARM/Graviton instances (c6g, c7g) are typically **20-40% cheaper** than their x86 equivalents (c6a) at the same vCPU count. However, some software may have compatibility issues with ARM architecture (native extensions, pre-built binaries, etc.).

ARM is **off by default**. To enable it, pass `--arm` to the CLI or `include_arm=True` to the Python API:

```bash
# CLI
spotrun launch --workers 8 --arm "python train.py"

# Python API
Session(workers=8, include_arm=True)
```

When ARM is enabled, spotrun considers both x86 and ARM instances and picks the cheapest option. If your workload is pure Python (no native C extensions), ARM is usually a safe and cheaper choice.

## Automatic Region Selection

Spot prices vary significantly across AWS regions -- sometimes 2-3x for the same instance type. When you don't set `AWS_REGION`, spotrun automatically queries spot prices across 8 major regions and launches in the cheapest one:

```
us-east-1, us-east-2, us-west-2,
eu-west-1, eu-central-1,
ap-south-1, ap-southeast-1, ap-northeast-1
```

```bash
# See prices across all regions for your workload
spotrun prices --workers 8

# Output:
# ┌─────────────────────────────────────────────┐
# │ Region           │ $/hr    │                 │
# ├──────────────────┼─────────┼─────────────────┤
# │ ap-south-1       │ $0.0532 │ <-- cheapest    │
# │ us-east-2        │ $0.0618 │                 │
# │ us-east-1        │ $0.0734 │                 │
# │ ...              │         │                 │
# └─────────────────────────────────────────────┘
```

To pin a specific region, set the environment variable:

```bash
export AWS_REGION=us-east-1
```

Infrastructure (key pair, security group, AMI) is created per-region and cached, so switching regions incurs a one-time AMI build.

## Custom Bootstrap

The default bootstrap script installs Python 3, a venv, build-essential, and rsync. You can replace it with your own:

```bash
spotrun setup --bootstrap ./my-bootstrap.sh
```

Or pre-install your Python dependencies into the AMI:

```bash
spotrun setup --requirements requirements.txt
```

This bakes your dependencies into the AMI so they don't need to be installed on every launch.

## How It Works

### Infrastructure (one-time setup)

On first run, spotrun creates:
- **Key pair** (`spotrun-{region}`) -- saved to `~/.spotrun/spotrun-{region}.pem`
- **Security group** (`spotrun-ssh`) -- allows SSH (port 22) inbound

Both are tagged and reused on subsequent runs.

### AMI (one-time build)

spotrun builds a custom AMI from Ubuntu 24.04 LTS:
1. Launches a `t3.medium` builder instance
2. Runs the bootstrap script (installs Python, venv, system packages)
3. Installs your `requirements.txt` if provided
4. Snapshots the instance as an AMI
5. Terminates the builder

The AMI is cached by tag -- subsequent runs skip this step. Use `spotrun setup --rebuild-ami` to force a rebuild.

### State

Active instance info is saved to `~/.spotrun/state.json`. This allows `spotrun teardown` to work without arguments. The state file is automatically cleaned up on teardown.

## Cost Awareness

Spot instances are billed per-second with a one-minute minimum. Typical c6a spot prices are $0.03-0.30/hr depending on size and region. Use `spotrun prices` to check current rates.

spotrun always terminates instances on:
- Command completion (success or failure)
- Errors during launch
- `spotrun teardown`

The only case where an instance stays running is `spotrun launch` without a command or `--ssh` (for manual use). Always run `spotrun teardown` when you're done.

## Security Notes

- The security group allows SSH from `0.0.0.0/0` (all IPs). For production use, restrict this to your IP.
- Your `.pem` key is stored at `~/.spotrun/` with `400` permissions (read-only, owner only).
- Credentials are passed via environment variables or boto3's standard credential chain.
- spotrun does **not** store or transmit your AWS credentials -- boto3 handles authentication directly.

## License

MIT
