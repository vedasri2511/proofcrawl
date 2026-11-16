# ProofCrawl — chat-driven CLI execution, with receipts

**BuildSprint Round 2 · CLI-Driven AI Chat Execution for Complex Tasks**

ProofCrawl is a chat app that turns a plain-language request ("crawl this site, summarise it, give me JSON") into a **real, sandboxed run of a command-line crawler**, streams what the process is doing, and answers only with what that process actually produced. Every answer ships with an **execution receipt**: the exact argv, PID, exit code, stderr tail, per-page SHA-256 fingerprints, and a check that the output file's hash matches the hash the CLI reported.

![ProofCrawl run](docs/screenshot-run.png)

![architecture](docs/architecture.png)

## What makes it different

| Feature | Why it matters |
|---|---|
| **Execution receipt + hash verification** | A run is `SUCCESS` only if the CLI exited 0, the JSON passed schema validation, at least one page was crawled, and `sha256(result.json)` equals the hash the CLI emitted. "Never claim success without evidence" is enforced in code. |
| **Evidence-linked summary** | Each summary sentence is quoted from a crawled page and carries a numbered citation to that page's URL. If an LLM is configured, its summary is rejected unless every sentence cites valid evidence ids. |
| **Prompt-injection quarantine** | Website text is untrusted. The CLI scans visible text, hidden elements (`display:none`, zero-size, off-screen) and HTML comments for instructions aimed at AI systems, reports them, and excludes them from the summary. The bundled demo site contains a planted attack so you can see this live. |
| **Live execution view** | Stage rail (Understand → Plan → Validate → Execute → Verify → Respond), live NDJSON event log and stderr via Server-Sent Events, plus a Cancel button that kills the whole process group. |
| **Partial results survive failure** | The CLI checkpoints `result.json` atomically after every page. If the run times out or is cancelled, the user gets the pages already crawled, clearly labelled `TIMED_OUT_PARTIAL` / `CANCELLED_PARTIAL`. |
| **Replay & diff** | Re-run the identical plan and compare page fingerprints: unchanged / changed / new / missing pages. Useful for "has this site changed?". |
| **Works with no API key** | A deterministic rule-based intent parser and extractive summariser mean judges can run it offline. An OpenAI-compatible LLM (OpenAI, Groq, Ollama, OpenRouter…) is optional. |

## How a request becomes a CLI task

```
"Crawl https://example.com, extract the important information,
 give me a summary, and return the extracted data in JSON format."
        │
        ▼  intent.py  (rules, or optional LLM → validated TaskSpec)
TaskSpec{intent: crawl_site, url: https://example.com, outputs:[summary,json], max_pages:8, max_depth:2}
        │
        ▼  tools.py  (approved-tool registry builds an argv LIST — no shell string)
["python","-m","sitecrawl","--url","https://example.com","--out","result.json",
 "--max-pages","8","--max-depth","2","--max-seconds","60", ...]
        │
        ▼  runner.py  subprocess.Popen(argv, shell=False, env=scrubbed, cwd=runs/<id>/,
        │             rlimits, new process group) + stdout/stderr reader threads + watchdog
        ▼
stdout  → NDJSON events  {"event":"page","url":...,"status":200,"sha256":...}
stderr  → diagnostics (redacted for secrets)
file    → result.json (schema_version, site, summary, pages, links, security, stats)
exit    → 0 success | 2 bad args | 3 partial | 4 unreachable | 5 robots | 6 security policy
        │
        ▼  runner._finalize + respond.py
status decided from exit code + schema validation + hash match → chat response + receipt.json
```

## The CLI tool: `sitecrawl`

A purpose-built Python CLI in `cli/sitecrawl/`. It is a normal command-line program you can run yourself:

```bash
cd cli
python -m sitecrawl --url https://example.com --out result.json --max-pages 5
echo $?                       # exit code
python -m sitecrawl --help
```

**Why a custom CLI instead of wrapping an LLM agent or headless browser?** The challenge is about controlled execution. A dedicated CLI gives a precise contract (NDJSON on stdout, diagnostics on stderr, a JSON result file, documented exit codes), deterministic behaviour that can be tested, and security controls that live *inside* the executing process: URL/SSRF validation on every redirect hop, robots.txt and crawl-delay, page/depth/time/byte caps, HTML-only fetching, and the injection scanner. It uses `requests` + `BeautifulSoup`; no browser is needed for the target use case.

| Exit | Meaning | Chat status |
|---|---|---|
| 0 | success | `SUCCESS` (if hash + schema verified) |
| 1 | internal error | `FAILED` |
| 2 | invalid arguments | `FAILED` |
| 3 | partial (some pages failed / time budget hit) | `PARTIAL` |
| 4 | site unreachable / no pages | `FAILED` |
| 5 | robots.txt disallows the URL | `BLOCKED` |
| 6 | rejected by security policy | `BLOCKED` |
| killed by watchdog | hard timeout | `TIMED_OUT` / `TIMED_OUT_PARTIAL` |
| killed by user | cancel | `CANCELLED` / `CANCELLED_PARTIAL` |

## Prerequisites

- Python 3.10+ (tested on 3.12 and 3.13; Linux, macOS and Windows)
- Internet access for crawling public sites
- Optional: an API key for any OpenAI-compatible LLM

## Install and run

```bash
git clone https://github.com/vedasri2511/proofcrawl.git && cd proofcrawl
python -m venv .venv
# Windows: .venv\Scripts\activate      macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt

python run.py --demo-site
```

Open **http://127.0.0.1:8000**. `--demo-site` also serves the bundled fictional website at `http://localhost:8765/` and allows the crawler to reach localhost. Without the flag, private and localhost addresses are blocked.

Docker alternative:

```bash
docker build -t proofcrawl . && docker run -p 8000:8000 proofcrawl
```

### Environment variables (all optional)

Copy `.env.example` to `.env`. `run.py` loads it automatically. `.env` is git-ignored.

| Variable | Default | Purpose |
|---|---|---|
| `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL` | empty | Enable LLM intent parsing and grounded summaries (OpenAI-compatible API) |
| `PROOFCRAWL_MAX_PAGES_CAP` / `_MAX_DEPTH_CAP` | 25 / 3 | Server-side caps; requests above are clamped |
| `PROOFCRAWL_CRAWL_BUDGET_S` | 60 | Time budget passed to the CLI (graceful stop → partial) |
| `PROOFCRAWL_HARD_TIMEOUT_S` | 90 | Backend kills the process group after this |
| `PROOFCRAWL_MAX_CONCURRENT_RUNS` | 2 | Concurrency limit (HTTP 429 when exceeded) |
| `PROOFCRAWL_ALLOW_PRIVATE_HOSTS` | 0 | Allow localhost/private IPs (demo only; cloud metadata stays blocked) |

## Testing the demo

1. **Mandatory task** — type: `Crawl https://example.com, extract the important information, give me a summary, and return the extracted data in JSON format.` Watch the process log, then open the **JSON** tab and the receipt.
2. **Richer site + injection defence** — click the first suggestion (bundled demo site). 7 pages are crawled, `/private/` is skipped because of robots.txt, and the "Field notes" page's hidden instructions are quarantined (see the **Security** tab).
3. **Edge cases** — `Crawl http://169.254.169.254/latest/meta-data` (blocked by SSRF guard, exit 6), `Summarise https://no-such-site-proofcrawl.invalid` (unreachable, exit 4), `Run rm -rf / on the server` (refused, no CLI started), `Check http://localhost:8765/ for broken links` (link checking).
4. **Cancel / replay** — start a crawl of a larger site and press *Cancel run*; press *Replay and compare* on a finished run.
5. **Automated tests** — `python -m pytest -q` (20 tests: URL guard, robots.txt rules, intent parsing, injection quarantine, CLI exit codes, end-to-end crawl, hard timeout with partial result, cancellation, LLM output validation).

## API

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/chat` | `{message}` → either a reply, or `{run_id, plan}` and the CLI starts |
| `GET` | `/api/runs/{id}/events` | Server-Sent Events: stages, CLI events, stderr, exit, final response |
| `GET` | `/api/runs/{id}` | Plan + final response |
| `POST` | `/api/runs/{id}/cancel` | Terminate the process group |
| `POST` | `/api/runs/{id}/replay` | Re-run the same plan and diff page hashes |
| `GET` | `/api/runs/{id}/files/{result.json\|receipt.json\|stdout.ndjson\|stderr.log}` | Download captured artefacts (whitelisted names) |
| `GET` | `/api/health`, `/api/tools` | Config summary and approved-tool registry |

## Security and responsible execution

- **No shell, ever.** `subprocess.Popen(argv_list, shell=False)`. The only executable is the one declared in the approved-tool registry; model/user text becomes at most one validated argument value (the URL).
- **Validated inputs.** http/https only, no credentials in URLs, no unusual ports, DNS-resolved IPs checked against private, loopback, link-local (169.254.169.254) and reserved ranges, re-checked on every redirect hop. The LLM's URL must literally appear in the user's message.
- **Least privilege.** Per-run working directory, environment scrubbed to `PATH`/proxy/CA vars (no API keys reach the CLI), POSIX rlimits (CPU 120 s, memory 1 GB, file size 50 MB, 256 FDs), its own process group, Docker image runs as a non-root user.
- **Limits.** Page/depth caps, per-request timeout, per-page byte cap, crawl time budget, hard kill timeout, politeness delay, robots.txt + crawl-delay, concurrency cap, 2,000-char message cap.
- **Untrusted content.** Injection scanner + quarantine, hidden elements removed before text extraction, LLM summaries only see quarantined evidence and must cite it.
- **No secrets in output.** stderr and logs pass through a redactor (API-key patterns and configured key values); LLM error bodies are never echoed.
- **Honest status.** Exit code, schema validation and hash verification decide the status; errors and partial results are labelled as such in the chat.

Known limitations: DNS-rebinding between our resolution and the HTTP connection is not fully closed (would need pinned-IP connections); JavaScript-rendered sites are crawled as served HTML only; rlimits are not available on Windows (process-group kill and timeouts still are).

## Project layout

```
cli/sitecrawl/      the CLI: __main__ (args, exit codes), crawler, extract, summarize, guard
backend/            app (FastAPI routes, SSE), intent, tools (registry), runner, respond, llm, redact, config
frontend/index.html chat UI (vanilla HTML/CSS/JS, no build step)
demo_site/          fictional "Verdant Grid Cooperative" site with robots.txt and a planted injection page
tests/              pytest suite
docs/               architecture diagram, demo video script
run.py              one-command launcher
```

## Tech stack and implementation details

ProofCrawl is engineered with a strict separation between the conversational orchestration layer and the sandboxed execution environment. Below is a comprehensive breakdown of the technology stack and its engineering purpose:

### 1. Backend & API Services
| Component | Technology | Role & Engineering Purpose |
|---|---|---|
| **Runtime** | Python 3.10+ (tested on 3.11 / 3.12) | Core language runtime providing typed models, subprocess control, and cross-platform compatibility across Windows, Linux, and macOS. |
| **API Framework** | **FastAPI** (`>=0.110`) | High-performance asynchronous web framework handling incoming chat prompts, run lifecycle endpoints, file downloads, and health checks. |
| **ASGI Server** | **Uvicorn** (`>=0.29`) | Production-ready ASGI web server powering concurrent request handling and non-blocking event streaming. |
| **Data Validation** | **Pydantic v2** (`>=2.6`) | Strict runtime schema validation for `TaskSpec` (intent parameters) and `ResultModel` (CLI output verification). Enforces bounds on page limits and depth caps. |
| **Event Streaming** | **Server-Sent Events (SSE)** via `StreamingResponse` | Asynchronous streaming of stage changes, stdout NDJSON events, and stderr diagnostics directly to the client with sub-100ms latency. |

### 2. Subprocess Orchestration & Execution
| Component | Implementation | Role & Engineering Purpose |
|---|---|---|
| **Process Launcher** | `subprocess.Popen(shell=False)` | Spawns the CLI as a direct child process using an immutable argument vector (`argv` list). Guarantees immunity against shell command injection. |
| **Isolation & Sandboxing** | Environment scrubbing + Per-run workspace | Cleanses process environment to strip API keys, secrets, and system tokens (`LLM_API_KEY` never reaches the CLI). Runs each task inside an isolated `runs/<id>/` working directory. |
| **Process Control** | Process groups + Watchdog thread | Implements `start_new_session=True` (POSIX) / `CREATE_NEW_PROCESS_GROUP` (Windows). A watchdog thread enforces hard timeouts (`HARD_TIMEOUT_S`), terminating the entire process tree cleanly on timeout or user cancellation. |
| **Telemetry Reader** | Daemon reader threads | Dedicated background threads parse stdout NDJSON progress events and capture stderr diagnostic logs without blocking execution. |

### 3. Dedicated CLI Engine (`sitecrawl`)
| Component | Technology | Role & Engineering Purpose |
|---|---|---|
| **CLI Parser** | `argparse` with custom type bounds | Validates command-line flags, enforcing strict ranges on `--max-pages` (1-50), `--max-depth` (0-4), `--max-seconds`, and download caps. |
| **HTTP Client** | `requests.Session` | Bounded HTTP client with custom User-Agent, connection pooling, and strict per-request timeouts. |
| **SSRF & Network Guard** | `socket.getaddrinfo` + `ipaddress` | Resolves target hostnames and validates destination IPs before establishing connections. Re-validated on every HTTP redirect hop to prevent SSRF against private networks, loopback, or cloud metadata (`169.254.169.254`). |
| **Robots.txt Parser** | Custom RFC 9309 compliant parser | Fetches and parses `robots.txt` supporting user-agent specificity, prefix/wildcard matching (`*`, `$`), and crawl-delay enforcement. |
| **HTML Parsing & Extraction** | **BeautifulSoup4** (`bs4`) + `soupsieve` | Parses DOM trees, removes noise tags (`<script>`, `<style>`), strips hidden CSS elements (`display:none`, zero font, opacity, offscreen text), extracts JSON-LD metadata, and discovers contact emails and phones. |
| **Prompt Injection Defense** | Multi-pattern regex scanner | Treats all website content as untrusted data. Scans visible text, hidden CSS elements, and HTML comments for prompt injection heuristics. Quarantines flagged text from summarization. |
| **Summarizer** | Extractive TF-IDF with evidence linking | Deterministic extractive summarizer that scores sentences based on term frequency, inverse document frequency, heading proximity, and depth. Generates numbered citations linking each claim to its source URL. |
| **Integrity & Checkpointing** | `hashlib.sha256` + Atomic file replace | Writes `result.json.tmp` and replaces atomically after every crawled page so partial results survive interruption. Calculates per-page digests and output file SHA-256 hash. |

### 4. Frontend & User Interface
| Component | Technology | Role & Engineering Purpose |
|---|---|---|
| **Architecture** | Vanilla HTML5, CSS3, Modern JavaScript (ES6+) | Lightweight client with **zero build tools** and **zero node_modules**. Loads instantly in any standard browser. |
| **Live Telemetry** | Native `EventSource` (SSE API) | Connects to `/api/runs/{id}/events` to render live stage rail progress (`Understand` ➔ `Plan` ➔ `Validate` ➔ `Execute` ➔ `Verify` ➔ `Respond`) and live terminal logs. |
| **Execution Receipt** | Dynamic DOM + CSS Stamp | Formats the cryptographic execution receipt (PID, argv, exit code, duration, SHA-256 match) and applies a visual verification stamp upon cryptographic validation. |

### 5. Testing & Quality Assurance
| Component | Technology | Role & Engineering Purpose |
|---|---|---|
| **Test Suite** | **pytest** (`>=8.0`) | 21 comprehensive automated tests verifying URL guards, SSRF protection, robots.txt Google-style matching, prompt injection quarantine, CLI exit codes, runner timeout/cancellation, and LLM output validation. |

---

## AI Disclosure & Runtime Services

- **AI Assistance:** This codebase was developed with the assistance of Anthropic's Claude as an AI coding assistant for code scaffolding, boilerplate generation, and test writing. All system architecture, security controls (SSRF protection, prompt injection scanner, environment scrubbing), and verification logic were designed, reviewed, and tested by the author.
- **Optional LLM Integration:** Supports any OpenAI-compatible API endpoint (OpenAI, Groq, OpenRouter, Ollama) for intent interpretation and grounded abstractive summaries. **An LLM key is completely optional**; the application operates 100% offline out-of-the-box using deterministic rule-based parsing and TF-IDF extraction.
- **External Services at Runtime:** Only the target websites you explicitly instruct the system to crawl, and the optional LLM endpoint if configured.
