---
kind: how-to
owner: @VytCepas
status: unreviewed
---

# Run the dashboard as a service

`serve` renders the live fleet dashboard — the overview table, per-project
drill-ins, and (opt-in) the re-check/heal buttons — and blocks until
interrupted. Run ad hoc that means a dashboard that dies with your terminal.
This guide installs it as a systemd *user* service so it is always there,
survives reboots, and restarts itself after a crash.

## Install the service

The unit serves `$PORT_ROOT` (else `~/port`) read-only on `127.0.0.1:8787`.
Override either, or enable the mutating buttons, in a private env file. A user
unit reads no shell profile, so an exported `PORT_ROOT` belongs there too:

```bash
mkdir -p ~/.config/projects-orchestrator
printf 'PO_FLEET_ROOT=%s\nPO_SERVE_PORT=%s\n' "$HOME/projects" 8787 \
  > ~/.config/projects-orchestrator/serve.env
```

Skip that step to accept the defaults.

```bash
mkdir -p ~/.config/systemd/user
cp contrib/systemd/fleet-controller-serve.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now fleet-controller-serve.service
```

Check it:

```bash
systemctl --user status fleet-controller-serve.service        # running?
journalctl --user -u fleet-controller-serve.service -n 30     # its log
curl -s http://127.0.0.1:8787/api/fleet | head -c 200              # answering?
```

The unit expects the console script at `~/.local/bin/projects-orchestrator` (a
`uv tool install` / `pip install --user` puts it there). Adjust `ExecStart` if
yours lives elsewhere.

## Surviving logout: linger

User units normally start at login and stop at logout. For a workbench that
should serve whether or not you have a session open:

```bash
loginctl enable-linger "$USER"
```

On WSL this needs systemd enabled in `/etc/wsl.conf` (`[boot]
systemd=true`) — and note WSL itself stops when its last window closes unless
the distro is kept running (`wsl --manage <distro> --set-sparse` does not do
this; a background `wsl -d <distro>` keeps it alive).

## Enabling the action buttons

The dashboard is read-only by default. To allow re-check and heal from the
page, add to `serve.env`:

```bash
PO_SERVE_ACTIONS=1
```

and restart the service. The buttons stay safe to expose to yourself: the
server refuses a non-loopback bind with actions enabled, every mutating POST
needs the CSRF token the page carries, and a heal still ends in a draft PR,
never a merge (PO-156, ADR-006).

## Reaching it from another device

The bind is loopback on purpose — the unit does not offer a way to widen it.
From a phone or second machine, tunnel instead:

```bash
ssh -L 8787:127.0.0.1:8787 workbench      # then open http://127.0.0.1:8787
```

or serve the loopback port over your tailnet (`tailscale serve 8787`), which
keeps the auth question Tailscale's problem rather than the dashboard's.
