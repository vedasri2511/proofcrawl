# Submission checklist (BuildSprint Round 2)

Deadline: **16 Nov 2026, 11:59 PM IST** — form: https://form.workelate.com/previewform/6af8bd0090174e8d87484e41

| # | Deliverable | Where it is | Your action |
|---|---|---|---|
| 1 | Working prototype | whole repo; `python run.py --demo-site` | Run it once on your machine against https://example.com |
| 2 | Source code | this repo | Push to a **public** GitHub repo (commands below); paste the link |
| 3 | README / setup guide | `README.md` | Replace `<your-repo-url>` with your GitHub URL |
| 4 | Live demo / access instructions | README "Install and run" + Docker | Local run instructions are enough; add a deployed URL if you deploy |
| 5 | Explanation + demo video (4–7 min) | script in `docs/VIDEO_SCRIPT.md` | Record, upload (YouTube unlisted / Google Drive "anyone with link"), paste link |

## Push to GitHub

```bash
cd proofcrawl
git init && git add . && git commit -m "ProofCrawl: chat-driven CLI execution with receipts"
git branch -M main
git remote add origin https://github.com/vedasri2511/proofcrawl.git
git push -u origin main
```

Check before pushing: no `.env` file committed (`git status` should not list it).

## Rubric coverage

| Area (marks) | Evidence in this project |
|---|---|
| Working implementation (25) | Real crawl via spawned CLI; no mocked results; live SSE log |
| CLI integration (20) | Registry → argv list, `Popen(shell=False)`, stdout NDJSON + stderr + exit code + output file, timeout, cancel, process-group kill |
| End-to-end quality (15) | Evidence-linked summary, JSON-LD/contacts extraction, valid JSON with named fields, tabs |
| Architecture (15) | Separated CLI / intent / registry / runner / respond; diagram; tests |
| Security & reliability (10) | SSRF guard, robots.txt, limits, scrubbed env, rlimits, redaction, injection quarantine, honest status |
| Video (10) | Script covers every required point in order |
| Originality & UX (5) | Execution receipt with hash stamp, quarantine, partial checkpoints, replay & diff |
