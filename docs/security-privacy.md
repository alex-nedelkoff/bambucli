# Security & Privacy (MFIPPA)

This is the checklist to walk through with the library supervisor / privacy
officer. The app collects **patron identifiers** (names, 14-digit library card
numbers, and sometimes email bodies from `.eml` intake), so it is in scope for
**MFIPPA** (Ontario's Municipal Freedom of Information and Protection of Privacy
Act).

> Distilled from the original pre-launch deployment doc and updated for the
> current **Windows** deployment. For how to run/restart the app, see
> [OPERATIONS.md](OPERATIONS.md).

## One-paragraph summary (for a supervisor meeting)

> This is a 3D-print intake tool that runs entirely on a single dedicated host on
> the library's internal staff network. It collects patron names and library card
> numbers via a web form, slices STL files locally, and prints a paper receipt.
> **No patron data leaves the device** — slicing, receipt printing, ledger
> export, and the email-parsing LLM all run on-device with no cloud or external
> API calls. Full-disk encryption protects PII at rest, and the host is not
> reachable from the public internet or patron Wi-Fi. Every order is written to an
> audit trail (`printqueue/orders.json`) that can be exported as CSV. A retention
> policy on raw emails and working files is recommended. Risks are limited to (a)
> theft/tampering with the host, mitigated by encryption, and (b) staff with host
> access, mitigated by existing HR controls.

## Where patron data lives

| Path | Sensitivity |
|---|---|
| `printqueue/orders.json` | Names, card numbers, files, price — the audit ledger |
| `printqueue/jobs.db`, `feedback.db` | Job history / feedback (SQLite) |
| `printqueue/work/<intake>/` | Uploaded STLs + extracted email bodies (raw PII) |

All of `printqueue/` is **gitignored** and must never be committed or shared.

## Pre-launch checklist

- [ ] **Full-disk encryption (BitLocker)** enabled on the host. Protects patron
      PII at rest if the machine is lost or stolen.
- [ ] **Privacy Impact Assessment (PIA)** initiated with the municipality's
      privacy officer — MFIPPA expects one for any new system collecting patron
      identifiers.
- [ ] **Retention policy** agreed and implemented. Suggested: purge
      `printqueue/work/<intake>/` and any stored `.eml` after 30 days; delete
      sliced 3MFs a week after pickup. *(Not yet automated — a scheduled cleanup
      job is a recommended follow-up.)*
- [ ] **Network scope** confirmed: the app binds port 8000 on the **staff-only**
      LAN, isolated from patron Wi-Fi. Verify the subnet with IT.
- [ ] **No public exposure:** no port forwarding, no public DNS, no open tunnel.
      Remote staff access (if ever needed) goes through the library VPN.
- [ ] **Auth decision documented.** Current state: no app-level auth — it relies
      on network isolation. If the host ever becomes reachable beyond the staff
      LAN, add a shared password / SSO *before* that change.
- [ ] **HTTPS decision documented.** Current state: plain HTTP on the LAN. If
      traffic ever crosses shared Wi-Fi, add a cert via uvicorn's
      `--ssl-keyfile` / `--ssl-certfile`.
- [ ] **Backups** in place and **encrypted** (e.g. a scheduled copy of
      `printqueue/` to an encrypted drive). Confirm the destination is encrypted.
- [ ] **Incident-response note** written: who to notify (privacy officer), how to
      preserve `orders.json` as evidence, and the MFIPPA breach-notification
      obligation to the Ontario IPC if PII is exposed.
- [ ] **Disclosure text** on the intake form reviewed by the privacy officer.
      Draft: *"This form collects your name and library card number to process
      your 3D print order. Data is retained for 30 days and used only by
      makerspace staff."* — adjust per their guidance.

## Notes

- **No telemetry / no cloud.** The pipeline is fully local. The `.eml` intake
  uses a **local** Ollama LLM (`llama3.2:1b` on `127.0.0.1:11434`) — patron text
  is sent only to that on-device model, never to a hosted API.
- **Dependencies** are OSI-approved open source (FastAPI/MIT, Pillow/MIT-CMU,
  python-escpos/MIT). OrcaSlicer is AGPLv3 but used as an unmodified CLI, so
  redistribution obligations don't apply to normal operation.
