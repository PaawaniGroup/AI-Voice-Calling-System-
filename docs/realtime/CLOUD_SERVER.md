# Where the app runs: PC behind ngrok, or a cloud server near the GPUs (decision note; nothing here has been done)

Everything priced here is UNVERIFIED (no vendor page was read for it) unless marked otherwise. The operator's current answer (27 Oct 2026): stay on the PC behind ngrok for now; this note is for the later decision.

## Why it matters
- Gate G1 (round trip to an always-on server, median <= 40 ms) is judged only from the host the service will run on (`--declared-from cloud`). From the PC the Pod round trip is probably above 40 ms (ESTIMATE: PC to the Cloudflare edge was 57 ms average earlier, MEASURED), so a PC run is informational.
- Ocean crossings per turn (ESTIMATE, reasoning not measurement), if the GPUs are far from the caller: with the app on the PC, one turn crosses the long path for the STT upload, for the Brain request and for every voice clause (about 3 or more round trips); with the app next to the GPUs, the long path is crossed by the caller's audio only (about 1 round trip in total) and the model hops are short. This is why the move is expected to help the persistent-transport gates; it is not proven. It needs the GPUs' region (UNKNOWN to me: read it in the RunPod console) and where Plivo and the callers are (UNKNOWN).

## What the cloud server needs
| Item | Proposal | Status |
|---|---|---|
| Region | the same datacentre region as the GPU endpoints / Pods (Secure Cloud datacentre shown in the RunPod console) | region UNKNOWN: operator reads it |
| Size | 2 vCPU / 4 GB RAM for the app and a small MongoDB; 4 vCPU / 8 GB if FreeSWITCH and Mongo run on it too | UNVERIFIED sizing: it depends on which components run on the PC today (the app, Mongo on port 27117, the media bridge, ngrok), which I have not inventoried |
| Monthly cost | about USD 12-48 for the 2 vCPU / 4 GB to 4 vCPU / 8 GB range, plus a domain (about USD 1-2 per month) and traffic | UNVERIFIED |
| TLS and name | a public DNS name with a CA certificate (Let's Encrypt, free) so Plivo reaches `https://` and `wss://` without ngrok | UNVERIFIED how Plivo validates the media socket certificate: check its docs before the move |
| Setup (one-off, offline preparation first) | Linux host; Python venv with the repository's requirements; MongoDB (restore from `scripts/backup_mongo.sh` output, verified with `scripts/verify_backup_restore.sh`); the service started the way `scripts/` does today; keys and settings copied by the operator, never by me and never printed; firewall: only 443 (and SSH from the operator's address) open | steps from the repo's own scripts; not executed |
| Cutover | keep the PC + ngrok path untouched as the fallback; point ONE test number at the new host first | |

## What the operator must do in Plivo
1. Create or point a DNS name at the cloud server. 2. In the Plivo console set the application's answer URL (and the hang-up / callback URLs) to the new public name; the stream URL in the answer XML is built from the app's public base URL setting, so that setting changes too (otherwise Plivo's request-signature check fails, because it signs the URL). 3. Test with one test number, then move the others. 4. Keep the old ngrok URL in a note so the change can be undone by switching the URLs back. (Exact console labels: UNVERIFIED.)

## Risks of moving
- The audio path for live calls changes; a mistake breaks calls (mitigation: test number first, old path kept, no change during business hours).
- Two copies of the data (Mongo) can diverge; decide which is authoritative before the cutover, and take a backup of the PC's Mongo first (the repository has the scripts; running them is the operator's step).
- Secrets move machines: new key file locations, nothing in the repository, nothing printed; rotate any key that was ever pasted.
- A cloud host is a new always-on cost and a new thing to patch; the PC is free but must stay on and online during calls.
- The region guess may be wrong; measure first (`rt_rtt_probe.py --declared-from cloud` from a cheap trial host costs a few cents) before committing to a monthly server.
