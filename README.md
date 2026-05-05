# research-scorer

Automated research paper discovery and scoring pipeline. Harvests papers from arXiv and RePEc daily, scores each one against your operational context using Claude, and delivers a ranked digest via email.

**What makes this different from generic arXiv tools:** the scorer doesn't use generic relevance. It reads your current research gaps, your tech stack, your retired hypotheses, and your working assumptions — then scores every paper against *that* context. When your priorities change, you update a text file and scoring adapts automatically.

## How it works

```
Daily cron job (3 scripts, chained):

research_harvester.py    Fetch new papers from arXiv + RePEc NEP
        &&
research_scorer.py       Score each paper with Claude Haiku (~$0.01/paper)
        &&
research_ranked_generator.py   Rank, summarize, email weekly digest
```

**Cost:** ~$2-3/month for scoring (Claude Haiku). Infrastructure runs on any Linux box you already have.

## Quick start

```bash
# 1. Clone and install
git clone https://github.com/BD7691/research-scorer.git
cd research-scorer
pip install -r requirements.txt

# 2. Configure
cp .env.example .env
# Edit .env: add your ANTHROPIC_API_KEY (required) and SMTP settings (optional)

# 3. Set up your context (the important part)
# Edit these files to describe YOUR domain:
#   context/domain_context.md  — who you are, what you do
#   context/tech_stack.md      — what you can build with
#   context/gaps.md            — what you're trying to solve right now
#   context/graveyard.md       — approaches you've tried and abandoned
#   context/assumptions.md     — what you believe to be true
#
# See examples/ for two complete configurations.

# 4. Edit config.yaml
# Pick your arXiv categories, RePEc feeds, and entity routing targets.

# 5. Run it
python research_harvester.py && python research_scorer.py && python research_ranked_generator.py

# 6. Check results
cat output/research_ranked.md
```

## Configuration

Everything lives in `config.yaml`. The key sections:

### Sources — where to harvest from

```yaml
sources:
  arxiv:
    enabled: true
    sets: ["q-fin:q-fin:TR", "q-fin:q-fin:ST"]  # your arXiv categories
  arxiv_filtered:
    enabled: false          # optional second pipeline with keyword pre-filter
    keyword_tier1: [...]    # papers must match at least one keyword
  repec_nep:
    enabled: true
    feeds: {nep-fmk: "https://..."}
```

Full arXiv category list: https://arxiv.org/category_taxonomy

### Context — what the scorer knows about you

```yaml
context:
  roadmap_file: "context/gaps.md"
  gaps_file: "context/gaps.md"
  graveyard_file: "context/graveyard.md"
  assumptions_file: "context/assumptions.md"
```

These files are read every run and injected into the scorer prompt. Update them as your priorities change — scoring adapts automatically.

### Entities — routing targets

```yaml
entities:
  route_threshold: 4
  targets:
    - name: "Project Alpha"
      domain: "signal processing, regime detection"
    - name: "Project Beta"
      domain: "risk management, portfolio construction"
```

Papers scoring >= 4 on any entity get flagged for that entity's queue.

### Delivery — how you get results

```yaml
delivery:
  email:
    enabled: true
    digest_threshold: 18    # minimum score for digest inclusion
    digest_max_papers: 5    # max papers per digest
```

SMTP credentials go in `.env`, not `config.yaml`.

## Scoring framework

Every paper gets scored on 5 dimensions:

**Gates (binary pass/fail):**
- **Evidence** (1-5): quality of empirical support. Below 2 = disqualified.
- **Buildability** (1-5): can you build this with your stack? Below 2 = disqualified.

**Weighted dimensions (only if gates pass):**
- **Capital Impact** (1-5, weight 2.0): effect on outcomes if implemented
- **Time-to-Value** (1-5, weight 1.75): time from paper to working implementation
- **Relevance** (1-5, weight 1.25): match to your documented active gaps

**Formula:** `weighted_total = (CI * 2.0) + (TV * 1.75) + (RG * 1.25)` — max 25.0

The scorer prompt template is in `scorer_prompt.md`. The framework is domain-agnostic; your context files make it specific.

## What a scored paper looks like

```json
{
  "id": "arxiv:2603.12345",
  "gates": {"evidence": 4, "buildability": 3},
  "scores": {"capital_impact": 4, "time_to_value": 3, "relevance": 5},
  "weighted_total": 19.50,
  "bot_relevance": {"Project Alpha": 4, "Project Beta": 2},
  "one_line_summary": "HMM regime detection on 5-min bars with VIX features",
  "rationale": "Directly addresses the regime detection gap..."
}
```

## Cron setup

```bash
# Daily at 6 AM ET (10:00 UTC)
0 10 * * * cd /path/to/research-scorer && python research_harvester.py && python research_scorer.py && python research_ranked_generator.py

# Weekly digest on Fridays
0 10 * * 5 cd /path/to/research-scorer && python research_ranked_generator.py --weekly-digest
```

## Examples

Two complete example configurations in `examples/`:

- **systematic-macro/** — a systematic macro trading desk tracking rates, FX, and commodities research
- **comp-bio/** — a computational biology lab doing CRISPR delivery optimization

Each includes a `config.yaml` and `context/` files you can use as templates.

## Data storage

All data is append-only JSONL in `data/`:
- `harvest.jsonl` — every paper ever harvested
- `scored.jsonl` — every scoring result
- `harvester_runs.jsonl` — run logs for monitoring

No database required.

## License

MIT
