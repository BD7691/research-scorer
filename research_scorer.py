"""
Research Paper Scorer
Scores unscored papers from data/harvest.jsonl using Claude with
dynamic prompt injection. Writes results to data/scored.jsonl.

Usage: python research_scorer.py [--config config.yaml]
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timezone

import anthropic
import yaml
from dotenv import load_dotenv

load_dotenv()


def log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[{ts}] {msg}")


def load_config(config_path="config.yaml"):
    with open(config_path, encoding="utf-8") as f:
        return yaml.safe_load(f)


# --- Dynamic Context Injection ---

def read_context_file(filepath, max_chars=2000, tail_lines=20):
    if not os.path.exists(filepath):
        return "Not configured"
    with open(filepath, encoding="utf-8") as f:
        content = f.read()
    if len(content) < max_chars:
        return content
    lines = content.strip().split("\n")
    return "\n".join(lines[-tail_lines:])


def build_dynamic_context(config):
    ctx = config.get("context", {})
    return {
        "{{ROADMAP_PHASE}}": read_context_file(ctx.get("roadmap_file", "context/gaps.md")),
        "{{OPEN_GAPS}}": read_context_file(ctx.get("gaps_file", "context/gaps.md")),
        "{{RETIRED_HYPOTHESES}}": read_context_file(
            ctx.get("graveyard_file", "context/graveyard.md")),
        "{{ASSUMPTIONS}}": read_context_file(ctx.get("assumptions_file", "context/assumptions.md")),
    }


def build_static_context(config):
    ctx = config.get("context", {})
    result = {}
    domain_file = ctx.get("domain_context_file", "context/domain_context.md")
    if os.path.exists(domain_file):
        with open(domain_file, encoding="utf-8") as f:
            result["{{DOMAIN_CONTEXT}}"] = f.read()
    stack_file = ctx.get("tech_stack_file", "context/tech_stack.md")
    if os.path.exists(stack_file):
        with open(stack_file, encoding="utf-8") as f:
            result["{{TECH_STACK}}"] = f.read()
    # Build entities section from config
    entities = config.get("entities", {})
    targets = entities.get("targets", [])
    if targets:
        lines = []
        for t in targets:
            lines.append(f"| {t['name']} | {t['domain']} |")
        entity_table = "| Entity | Domain Focus |\n|--------|-------------|\n" + "\n".join(lines)
        result["{{ENTITIES}}"] = entity_table
    return result


def assemble_prompt(template, title, abstract, dynamic_ctx, static_ctx):
    prompt = template
    for placeholder, value in static_ctx.items():
        prompt = prompt.replace(placeholder, value)
    for placeholder, value in dynamic_ctx.items():
        prompt = prompt.replace(placeholder, value)
    prompt = prompt.replace("{{TITLE}}", title)
    prompt = prompt.replace("{{ABSTRACT}}", abstract)
    return prompt


def validate_placeholders(prompt):
    placeholders = ["{{ROADMAP_PHASE}}", "{{OPEN_GAPS}}", "{{RETIRED_HYPOTHESES}}",
                    "{{ASSUMPTIONS}}", "{{TITLE}}", "{{ABSTRACT}}",
                    "{{DOMAIN_CONTEXT}}", "{{TECH_STACK}}", "{{ENTITIES}}"]
    return [p for p in placeholders if p in prompt]


# --- File I/O ---

def load_harvest_records(harvest_file):
    records = []
    if not os.path.exists(harvest_file):
        return records
    with open(harvest_file, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


def load_scored_ids(scored_file):
    ids = set()
    if not os.path.exists(scored_file):
        return ids
    with open(scored_file, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                ids.add(json.loads(line)["id"])
            except (json.JSONDecodeError, KeyError):
                continue
    return ids


def batch_update_harvest(harvest_file, updates):
    if not os.path.exists(harvest_file) or not updates:
        return
    lines = []
    with open(harvest_file, encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            if not stripped:
                lines.append(line)
                continue
            try:
                record = json.loads(stripped)
                pid = record.get("id")
                if pid in updates:
                    u = updates[pid]
                    if u.get("scored"):
                        record["scored"] = True
                    if u.get("disqualified"):
                        record["disqualified"] = True
                        record["disqualify_reason"] = u.get("disqualify_reason")
                    if "score_failures" in u:
                        record["score_failures"] = u["score_failures"]
                lines.append(json.dumps(record) + "\n")
            except json.JSONDecodeError:
                lines.append(line)
    with open(harvest_file, "w", encoding="utf-8") as f:
        f.writelines(lines)


# --- Scoring ---

def score_paper(client, assembled_prompt, model, max_tokens, temperature):
    response = client.messages.create(
        model=model, max_tokens=max_tokens, temperature=temperature,
        messages=[{"role": "user", "content": assembled_prompt}])
    text = ""
    for block in response.content:
        if block.type == "text":
            text += block.text
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```\w*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
        text = text.strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        repaired = " ".join(line.strip() for line in text.split("\n") if line.strip())
        parsed = json.loads(repaired)
    return parsed, response.usage.input_tokens, response.usage.output_tokens


def remap_to_schema(haiku_output, paper_id, config, pipeline="primary", prompt_version="v1"):
    entities = config.get("entities", {})
    targets = entities.get("targets", [])
    default_relevance = {t["name"]: 1 for t in targets}
    model = config.get("scoring", {}).get("model", "claude-haiku-4-5-20251001")

    return {
        "id": paper_id,
        "pipeline": pipeline,
        "gates": {
            "evidence": haiku_output["evidence"],
            "buildability": haiku_output["buildability"],
        },
        "scores": {
            "capital_impact": haiku_output.get("capital_impact"),
            "time_to_value": haiku_output.get("time_to_value"),
            "relevance": haiku_output.get("relevance"),
        },
        "weighted_total": haiku_output.get("weighted_total"),
        "bot_relevance": haiku_output.get("bot_relevance", default_relevance),
        "one_line_summary": haiku_output.get("one_line_summary", ""),
        "rationale": haiku_output.get("rationale", ""),
        "disqualified": haiku_output.get("disqualified", False),
        "disqualify_reason": haiku_output.get("disqualify_reason"),
        "scored_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "scorer_model": model,
        "scorer_prompt_version": prompt_version,
    }


def score_pipeline(client, papers, scorer_template, dynamic_ctx, static_ctx,
                   config, pipeline, prompt_version, harvest_updates):
    scoring = config.get("scoring", {})
    model = scoring.get("model", "claude-haiku-4-5-20251001")
    max_tokens = scoring.get("max_tokens", 500)
    temperature = scoring.get("temperature", 0)
    data_dir = config.get("paths", {}).get("data_dir", "data")
    scored_file = os.path.join(data_dir, "scored.jsonl")

    scored_count, error_count = 0, 0
    total_in, total_out = 0, 0

    for paper in papers:
        paper_id = paper["id"]
        title = paper.get("title", "")
        abstract = paper.get("abstract", "")
        log(f"Scoring [{pipeline}]: {paper_id} -- {title[:60]}...")

        assembled = assemble_prompt(scorer_template, title, abstract, dynamic_ctx, static_ctx)
        unreplaced = validate_placeholders(assembled)
        if unreplaced:
            log(f"ERROR: unreplaced placeholders: {unreplaced}. Skipping {paper_id}.")
            error_count += 1
            continue

        try:
            output, in_tok, out_tok = score_paper(client, assembled, model, max_tokens, temperature)
            total_in += in_tok
            total_out += out_tok
            scored_record = remap_to_schema(output, paper_id, config, pipeline, prompt_version)
            with open(scored_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(scored_record) + "\n")
            harvest_updates[paper_id] = {
                "scored": True,
                "disqualified": scored_record.get("disqualified", False),
                "disqualify_reason": scored_record.get("disqualify_reason"),
            }
            scored_count += 1
            wt = scored_record.get("weighted_total")
            wt_str = f"{wt:.2f}" if wt is not None else "DQ"
            log(f"  Scored: {wt_str}/25 | tokens: {in_tok}+{out_tok}")

        except anthropic.AuthenticationError:
            log("FATAL: API key invalid or expired. Stopping.")
            if harvest_updates:
                batch_update_harvest(os.path.join(data_dir, "harvest.jsonl"), harvest_updates)
            sys.exit(1)

        except anthropic.RateLimitError:
            log("Rate limited. Sleeping 60s then retrying once.")
            time.sleep(60)
            try:
                output, in_tok, out_tok = score_paper(
                    client, assembled, model, max_tokens, temperature)
                total_in += in_tok
                total_out += out_tok
                scored_record = remap_to_schema(
                    output, paper_id, config, pipeline, prompt_version)
                with open(scored_file, "a", encoding="utf-8") as f:
                    f.write(json.dumps(scored_record) + "\n")
                harvest_updates[paper_id] = {
                    "scored": True,
                    "disqualified": scored_record.get("disqualified", False),
                    "disqualify_reason": scored_record.get("disqualify_reason"),
                }
                scored_count += 1
            except Exception as e2:
                log(f"Retry failed for {paper_id}: {e2}. Skipping.")
                error_count += 1

        except json.JSONDecodeError as e:
            log(f"JSON parse error for {paper_id}: {e}. Skipping.")
            error_count += 1
            harvest_updates[paper_id] = {"score_failures": paper.get("score_failures", 0) + 1}

        except Exception as e:
            log(f"Error scoring {paper_id}: {e}. Skipping.")
            error_count += 1
            harvest_updates[paper_id] = {"score_failures": paper.get("score_failures", 0) + 1}

    return scored_count, error_count, total_in, total_out


def main():
    config_path = "config.yaml"
    for i, arg in enumerate(sys.argv[1:], 1):
        if arg == "--config" and i < len(sys.argv) - 1:
            config_path = sys.argv[i + 1]

    config = load_config(config_path)
    scoring = config.get("scoring", {})
    data_dir = config.get("paths", {}).get("data_dir", "data")
    harvest_file = os.path.join(data_dir, "harvest.jsonl")
    scored_file = os.path.join(data_dir, "scored.jsonl")

    log("=== Research Scorer starting ===")

    prompt_path = scoring.get("prompt_file", "scorer_prompt.md")
    if not os.path.exists(prompt_path):
        log(f"FATAL: scorer prompt not found at {prompt_path}")
        sys.exit(1)
    with open(prompt_path, encoding="utf-8") as f:
        primary_template = f.read()

    harvest_records = load_harvest_records(harvest_file)
    scored_ids = load_scored_ids(scored_file)

    to_score_primary = [
        r for r in harvest_records
        if r["id"] not in scored_ids and not r.get("scored", False)
        and not r.get("disqualified", False) and r.get("score_failures", 0) < 3
        and r.get("pipeline", "primary") == "primary"
    ]
    to_score_filtered = [
        r for r in harvest_records
        if r["id"] not in scored_ids and not r.get("scored", False)
        and not r.get("disqualified", False) and r.get("score_failures", 0) < 3
        and r.get("pipeline") == "filtered"
    ]

    log(f"Harvest: {len(harvest_records)} total, {len(scored_ids)} already scored")
    log(f"To score: {len(to_score_primary)} primary, {len(to_score_filtered)} filtered")

    filtered_template = None
    filtered_prompt_path = scoring.get("filtered_prompt_file")
    if filtered_prompt_path and os.path.exists(filtered_prompt_path):
        with open(filtered_prompt_path, encoding="utf-8") as f:
            filtered_template = f.read()
        log(f"Filtered scorer prompt loaded: {filtered_prompt_path}")
    elif to_score_filtered:
        log(
            "WARN: No filtered scorer prompt configured. "
            f"Using primary for {len(to_score_filtered)} filtered papers."
        )
        filtered_template = primary_template

    if not to_score_primary and not to_score_filtered:
        log("No papers to score. Exiting.")
        return

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        log("FATAL: ANTHROPIC_API_KEY not set. Check .env file.")
        sys.exit(1)

    client = anthropic.Anthropic(api_key=api_key)
    dynamic_ctx = build_dynamic_context(config)
    static_ctx = build_static_context(config)
    log("Dynamic + static context loaded")

    harvest_updates = {}

    p_scored, p_errors, p_in, p_out = score_pipeline(
        client, to_score_primary, primary_template, dynamic_ctx, static_ctx,
        config, pipeline="primary", prompt_version="v1", harvest_updates=harvest_updates)

    f_scored, f_errors, f_in, f_out = (0, 0, 0, 0)
    if to_score_filtered and filtered_template:
        f_scored, f_errors, f_in, f_out = score_pipeline(
            client, to_score_filtered, filtered_template, dynamic_ctx, static_ctx,
            config, pipeline="filtered", prompt_version="filtered_v1",
            harvest_updates=harvest_updates)

    if harvest_updates:
        batch_update_harvest(harvest_file, harvest_updates)
        log(f"Harvest file updated: {len(harvest_updates)} entries marked scored")

    total_tokens = p_in + p_out + f_in + f_out
    log(f"Primary scored: {p_scored}, filtered scored: {f_scored}")
    log(f"Token usage: {total_tokens} total")
    log(f"=== Scorer complete. Scored: {p_scored + f_scored}, errors: {p_errors + f_errors} ===")

    warn_threshold = scoring.get("token_warn_threshold", 400000)
    if total_tokens > warn_threshold:
        log(f"COST WARNING: {total_tokens} tokens exceeds {warn_threshold} threshold")


if __name__ == "__main__":
    main()
