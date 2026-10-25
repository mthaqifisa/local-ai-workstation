# ADR-0019: Portainer CE for Docker management
Status: Accepted (2026-10-26)

## Context
LAW v4 shipped Portainer on :9000; v6 did not (RESEARCH.md kept the container set to Open WebUI, SearXNG and
Homepage), so the owner found :9000 dead. The owner asked for it back to browse images, containers, volumes and logs
(LAW's own and those of agent-built projects such as Pantry, which run on the same Colima VM).

## Decision
- Portainer CE 2.45.3 (an LTS line) runs from compose.yaml on `0.0.0.0:9000` with its data in
  `~/LAW/docker/portainer`, analytics off.
- It mounts the Colima VM's `/var/run/docker.sock` inside its own container. The Docker socket is still never
  exposed over TCP.
- Portainer CE cannot run without a login (`--no-auth` was removed in 2.0), an exception to ADR-0006's no-login rule
  for this one service. The installer generates `PORTAINER_ADMIN_PASSWORD` into `~/LAW/state/.env`, and Compose
  passes it as a secret file (`/run/secrets/portainer_admin`, `--admin-password-file`). That skips Portainer's
  5-minute first-visit setup window, and the password exists only in `.env`. User: `admin`.
- Homepage (and so the Console's Services tab) shows a Portainer tile with its status; `law doctor` checks the port.

## Consequences
- Anyone on the LAN who has the admin password controls every container on the VM. That includes the agents'
  projects and LAW's own containers, which is equivalent to the machine access the runtime agents already have.
- The admin password applies only when Portainer's data folder is empty. A password changed in Portainer's UI is
  kept in `~/LAW/docker/portainer`. To reset it, stop the container and empty that folder.
