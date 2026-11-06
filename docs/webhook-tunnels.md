---
title: "Webhook tunnels"
description: "Receive provider webhooks on a local Druks installation through an HTTPS tunnel."
icon: "cable"
---

A local installation listens on `127.0.0.1:8001`. GitHub, Linear, Jira, Slack, and Twilio cannot reach that address, or a private tailnet address. To receive their events, run an HTTPS tunnel from a public hostname to the local dashboard port. Then set `urls.webhook_host` to that hostname.

[Full local setup](full-local.md#webhook-caveat) lists what works without a tunnel.

## What to forward

Forward only these path prefixes to `http://127.0.0.1:8001`:

| Path prefix | Used by |
| --- | --- |
| `/_external/` | Provider webhooks. Druks verifies the signature of each delivery |
| `/mcp` | Druks' own MCP server. Sandboxes call it at `https://<webhook_host>/mcp` with a bearer token |

Do not forward `/`. A local installation has no browser authentication, so a public dashboard is open to anyone.

`/mcp` is required. When `urls.webhook_host` is set, Druks gives each sandbox `https://<webhook_host>/mcp` as the address of the Druks MCP server. Chat, phone calls, and the Software Factory ticket tools use this address. A tunnel that forwards only `/_external/` breaks them.

If you run the calls server, Twilio also streams audio to `/_calls/*`, which must go to `127.0.0.1:8002` with the WebSocket upgrade. See the [webhook caveat](full-local.md#webhook-caveat).

The tunnel must keep request bodies and signature headers unchanged, and keep the request path.

## Configure Druks

Set the public hostname in `druks.toml`. Leave out the scheme and the path. Add the port if the tunnel does not use `443`:

```toml
[urls]
webhook_host = "druks.example.ts.net"
```

Druks builds `https://<webhook_host>` from this value. Thus, `host:port` also works, for example `druks.example.ts.net:8443`.

Run the installer again. Then recreate the services so that they read the new file, and rebuild the sandbox templates:

```bash
cd ~/druks
docker compose up -d --force-recreate
docker compose exec web druks sandboxes build
```

## Verify

```bash
docker compose exec web druks doctor
```

The `webhook_ingress` check sends an unsigned POST to `https://<webhook_host>/_external/github/events/`. Druks rejects it with HTTP 401, and the check passes:

```text
✓  webhook_ingress  https://<webhook_host>/_external/github/events/ reaches druks
```

Any other response came from something in front of Druks. The check does not probe `/mcp`. To check it, send a POST from a machine outside your network. Druks answers HTTP 401 without a token.

Then set the webhook URL of each provider, for example in the GitHub App:

```text
https://<webhook_host>/_external/github/events/
```

Keep the tunnel running whenever you expect events. Druks does not receive a delivery that fails while the tunnel is down. GitHub can redeliver one from the App's **Advanced → Recent Deliveries** page.

## Tailscale Funnel

Funnel publishes a node of your tailnet at `https://<machine>.<tailnet>.ts.net`. The hostname is stable, so the provider webhook URLs stay the same.

### One-time setup

Funnel is not on by default. A tailnet admin must enable it:

1. Enable MagicDNS and HTTPS certificates for the tailnet.
2. Add the `funnel` node attribute to the tailnet policy file. The first `tailscale funnel` command offers a link that adds the default attribute.

See [Tailscale Funnel](https://tailscale.com/kb/1223/funnel) for the requirements.

### Start the tunnel

Funnel listens only on ports `443`, `8443`, and `10000`. Check that no other process on the Mac or host already listens on the port that you choose:

```bash
lsof -nP -iTCP:443 -sTCP:LISTEN
```

A local web server, or a Docker or VM runtime that publishes `443` on every interface, takes the connection before Funnel does. The symptoms point at Tailscale instead: TLS fails with `tlsv1 alert internal error`, and the public DNS record does not appear. Use `8443` in that case.

Forward the two path prefixes using the port appropriate to your setup (here 8443 is used):

```bash
tailscale funnel --bg --https=8443 --set-path /_external/ http://127.0.0.1:8001/_external/
tailscale funnel --bg --https=8443 --set-path /mcp http://127.0.0.1:8001/mcp
tailscale funnel status
```

Then set the host and port:

```toml
[urls]
webhook_host = "<machine>.<tailnet>.ts.net:8443"
```

Public DNS for the hostname can take up to 10 minutes after the first start. `tailscale funnel status` and the Funnel badge in the admin console show the configuration, not public reachability. Test from a device outside the tailnet, or run `druks doctor`. A device on the tailnet resolves the hostname to its tailnet address and does not use the public path.

To stop the public exposure:

```bash
tailscale funnel reset
```
