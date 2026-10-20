---
title: "Colima on macOS"
description: "Run Druks on macOS with Colima."
sidebarTitle: "Colima on macOS"
icon: "apple"
---

[Full local setup](/full-local) on macOS needs a Docker host. [Docker Desktop](/container-runtimes/docker-desktop) is one option. Colima is a valid, open source alternative to Docker Desktop. Colima runs the Docker daemon in a Linux VM. The Compose services use `network_mode: host`, so "the host" is that VM, not your Mac. Colima forwards the ports that the VM listens on to your Mac.

Three installer defaults assume Docker Desktop. On Colima they behave as follows:

| Default | Docker Desktop | Colima |
| --- | --- | --- |
| `DRUKS_DOCKER_GID=0` | The mounted Docker socket has group `root` | The socket has the VM's `docker` group. Add it, see step 2 |
| `[sandbox].proxy_url = "http://172.17.0.1:8880"` | `172.17.0.1` is the bridge gateway | Same gateway. No change |
| `DRUKS_WEB_BIND_HOST=0.0.0.0` | Port publishing follows Docker Desktop's settings | Colima listens on `*:8001` on the Mac. See [ports and exposure](#ports-and-exposure) |

## Prerequisites

- Free disk space on the Mac. The service images, the sandbox base image, and the two sandbox templates use about 11 GB before the first agent run. A VM disk grows on the Mac's disk. When the Mac's disk fills, the VM's filesystem records I/O errors that remain after you free space.
- The same Docker CLI and Compose plugin that the installer requires.

## 1. Start a VM for Druks

If you might be using more than one local instance of Druks, give each installation its own Colima profile. Two local installations cannot share one Docker daemon, because Drukbox, the secrets exchange, and the secrets proxy use fixed host-network ports.

```bash
colima start druks --cpu 4 --memory 8 --disk 60 \
  --vm-type vz --mount-type virtiofs --activate=false
export DOCKER_CONTEXT=colima-druks
```

`--activate=false` keeps your current Docker context. Set `DOCKER_CONTEXT` in each shell that runs the installer or `docker compose` for this installation. Omit the flag to make this VM the default context.

Colima mounts your home directory into the VM with write access. Thus, the installer's default paths under `~` work without extra mounts.

## 2. Give Drukbox access to the Docker socket

Read the group of the socket inside the VM:

```bash
colima ssh -p druks -- stat -c '%g' /var/run/docker.sock
```

Colima's socket belongs to the `docker` group, for example `991`, with mode `660`. The installer writes `DRUKS_DOCKER_GID=0` on macOS and writes it again on every run. Thus, a change to `.env` does not last. Add the group in `compose.override.yaml` in the installation directory before you run the installer. The installer creates this file only when it is absent and never changes it.

```yaml
services:
  drukbox:
    group_add: ["991"]
  drukbox-exchange:
    group_add: ["991"]
```

Use the number that `stat` printed.

## 3. Install and verify

Run the installer and the steps in [full local setup](/full-local) with `DOCKER_CONTEXT` set. Then prove the sandbox path:

```bash
docker compose exec web druks doctor --sandbox
```

The `sandbox_e2e` check creates a real sandbox container through the socket. A wrong group fails this check.

Colima's default `bridge` gateway is `172.17.0.1`. Thus, the default `[sandbox].proxy_url` works.

## Ports and exposure

Colima forwards each port that the VM listens on to the Mac. The installer binds the dashboard to `0.0.0.0` on macOS, and Colima then listens on `*:8001` on the Mac. The local instance has no browser authentication. Thus, any device that reaches your Mac on your network can open the dashboard. Do not run a local installation on an untrusted network.

Two running Colima VMs that both listen on `8001` compete for the same Mac port, and the first one to bind wins. Nothing reports the conflict. Stop one VM, or run the other installation on another dashboard port.

To move the dashboard, change `DRUKS_WEB_PORT`, `DRUKS_UPSTREAM`, `[urls].endpoint`, and `[sandbox].issuer_url` together.

## Reset a Colima VM

`colima delete <profile>` keeps the container runtime data disk. The next `colima start` attaches it again with its images, volumes, and any filesystem damage. To start from an empty daemon, also delete the data:

```bash
colima delete druks --data
```

WARNING: This deletes the Postgres volume of the installation. The installation directory on your Mac remains.
