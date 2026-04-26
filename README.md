# SonarAI

> **Agentic AI pipeline**: `sonar-report.json` → clone repo → LLM fix → GitHub PR

SonarAI automatically detects, analyses, and fixes SonarQube-reported code quality and security issues using a three-LLM agent loop (Planner → Generator → Critic) orchestrated with LangGraph.

---

## Architecture

```
sonar-report.json
      │
      ▼
┌─────────────┐     ┌───────────┐     ┌──────────┐
│   Ingest    │────▶│ Load Repo │────▶│  Planner │  LLM·1: chain-of-thought
│ (parse+sort)│     │(clone+AST)│     │          │  strategy + confidence
└─────────────┘     └───────────┘     └────┬─────┘
                                           │
                                      ┌────▼─────┐
                                      │Generator │  LLM·2: unified diff
                                      └────┬─────┘
                                           │
                                      ┌────▼─────┐
                                      │  Critic  │  LLM·3: adversarial review
                                      └────┬─────┘
                                           │ rejected (max 1 retry)
                                      ┌────▼─────┐
                                      │ Validate │  git apply + mvn compile + test
                                      └────┬─────┘
                                           │
                              ┌────────────▼──────────────┐
                              │         Deliver            │
                              │  HIGH  → PR + CODEOWNERS  │
                              │  MEDIUM → Draft PR        │
                              │  LOW   → escalation .md   │
                              └───────────────────────────┘
```

---

## Setup

### 1. Prerequisites

- Python 3.11+
- Java JDK + Maven (for compile/test validation)
- GCP project with Vertex AI enabled
- GitHub personal access token (repo + PR scope)

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Authenticate with GCP

```bash
gcloud auth application-default login
```

### 4. Configure environment

```bash
cp .env.example .env
# Edit .env and fill in GCP_PROJECT and GITHUB_TOKEN at minimum
```

---

## Usage

```bash
python main.py \
  --report sonar-report.json \
  --repo   https://github.com/owner/repo.git \
  --sha    abc123def456
```

### Arguments

| Argument | Required | Description |
|----------|----------|-------------|
| `--report` | ✅ | Path to `sonar-report.json` |
| `--repo` | ✅ | GitHub HTTPS clone URL |
| `--sha` | ✅ | Exact commit SHA used during the Sonar scan |

---

## Sonar Report Format

SonarAI expects the standard Sonar API export format:

```json
{
  "issues": [
    {
      "key": "AY...",
      "rule": "java:S2259",
      "severity": "CRITICAL",
      "component": "project-key:src/main/java/com/example/Foo.java",
      "line": 42,
      "message": "A NullPointerException could be thrown...",
      "status": "OPEN",
      "effort": "5min"
    }
  ]
}
```

Issues with status `WONTFIX` or `FALSE_POSITIVE` are automatically skipped.  
Issues are processed in priority order: BLOCKER → CRITICAL → MAJOR → MINOR.

---

## Confidence & PR Strategy

| Confidence | Action |
|------------|--------|
| **HIGH** (≥0.8) | Normal PR + auto-assign from CODEOWNERS |
| **MEDIUM** (≥0.5) | Draft PR + review request comment |
| **LOW** (<0.5) | `escalations/{issueKey}_{rule}.md` written, no PR |

---

## Supported Rules (Rule KB)

| Rule | Name | Severity |
|------|------|----------|
| `java:S2259` | Null Pointer Dereference | CRITICAL |
| `java:S2095` | Resource Leak | CRITICAL |
| `java:S106` | Standard Outputs (use logger) | MAJOR |
| `java:S5547` | Weak Cipher Algorithm | CRITICAL |
| `java:S2068` | Hardcoded Credentials | BLOCKER |
| `java:S1192` | Duplicated String Literal | MINOR |
| `java:S3776` | Cognitive Complexity | CRITICAL |
| `java:S1481` | Unused Local Variable | MINOR |
| `java:S2293` | Diamond Operator | MINOR |
| `java:S2166` | Assert Statement Side Effects | MAJOR |

Rules not in the KB fall back to generic LLM reasoning.

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `GCP_PROJECT` | *(required)* | GCP project ID |
| `GITHUB_TOKEN` | *(required)* | GitHub PAT |
| `GCP_LOCATION` | `us-central1` | Vertex AI region |
| `VERTEX_MODEL` | `claude-sonnet-4-5@20251001` | Primary LLM |
| `VERTEX_FALLBACK_MODEL` | `gemini-1.5-pro-002` | Fallback LLM |
| `MAX_CRITIC_RETRIES` | `1` | Max Critic→Generator loops |
| `COMPILE_TIMEOUT` | `120` | mvn compile timeout (s) |
| `TEST_TIMEOUT` | `180` | mvn test timeout (s) |
| `CLONE_DIR` | `/tmp/sonar-ai-repos` | Repo clone directory |
| `ESCALATION_DIR` | `escalations` | Escalation file output |
| `CONFIDENCE_HIGH_THRESHOLD` | `0.8` | Score for HIGH label |
| `CONFIDENCE_MEDIUM_THRESHOLD` | `0.5` | Score for MEDIUM label |

---

## Running Tests

```bash
pytest tests/ -v
```

---

## Project Structure

```
sonar-ai/
├── main.py                  # CLI entry point
├── requirements.txt
├── .env.example
├── data/
│   ├── rule_kb.json         # Top-10 Java rule knowledge base
│   └── sample-report.json   # Example Sonar report for testing
├── sonar_ai/
│   ├── __init__.py
│   ├── config.py            # Pydantic Settings
│   ├── state.py             # AgentState TypedDict
│   ├── parser.py            # Sonar JSON parser + Rule KB loader
│   ├── repo_loader.py       # Git clone, file resolution, AST extraction
│   ├── prompts.py           # LangChain prompt templates (Planner, Generator, Critic)
│   ├── agents.py            # Three LLM node functions
│   ├── validator.py         # git apply + mvn compile + mvn test
│   ├── deliver.py           # PR creation + escalation writer
│   └── graph.py             # LangGraph state graph assembly
├── tests/
│   ├── test_parser.py
│   └── test_repo_loader.py
└── escalations/             # Auto-created for LOW-confidence issues
```

---

## Post-MVP Roadmap

- RAG / vector DB for prior fix retrieval
- Sonar rescan validation (confirm rule no longer fires)
- Parallel fan-out via LangGraph Send API
- Docker sandbox for `mvn` execution
- Redis + RQ job queue
- Full 200-rule KB
- LangGraph Postgres checkpointer (resume failed runs)
- LangSmith tracing

---

*SonarAI v0.1.0 — Iteration 1*
