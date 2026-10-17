---
title: "Docker Desktop on macOS"
description: "Enable host networking in Docker Desktop before you install Druks locally."
sidebarTitle: "Docker Desktop on macOS"
icon: "container"
---

The Compose services use `network_mode: host`. On macOS, Docker Desktop needs host networking for the dashboard and health endpoint at `127.0.0.1:8001`. Docker Desktop 4.34 or later supports this feature.

Before you run the installer:

1. Open Docker Desktop and sign in to your Docker account.
2. Open **Settings → Resources → Network**.
3. Select **Enable host networking**.
4. Select **Apply & restart**.

See [Docker's host networking guide](https://docs.docker.com/engine/network/drivers/host/#docker-desktop) for the platform requirements and limitations.

Then continue with [full local setup](/full-local).
