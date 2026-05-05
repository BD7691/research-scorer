"""
Research Ranked List Generator
Generates ranked list from data/scored.jsonl. Produces daily summary.
Sends weekly email digest via SMTP.

Usage: python research_ranked_generator.py [--config config.yaml] [--weekly-digest] [--dry-run]
"""

import json
import os
import smtplib
import sys
from datetime import datetime, timezone, timedelta
from email.mime.text import MIMEText

import requests as http_requests
import yaml
from dotenv import load_dotenv

load_dotenv()


def log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[{ts}] {msg}")


def load_config(config_path="config.yaml"):
    with open(config_path, encoding="utf-8") as f:
        return yaml.safe_load(f)


# --- Data Loading ---

def load_harvest_urls(harvest_file):
    urls = {}
    if not os.path.exists(harvest_file):
        return urls
    with open(harvest_file, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
                urls[r["id"]] = r.get("url", "")
            except (json.JSONDecodeError, KeyError):
                continue
    return urls


def load_scored_records(scored_file):
    records = []
    if not os.path.exists(scored_file):
        return records
    with open(scored_file, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


def load_harvest_stats(harvest_file):
    total, recent_7d = 0, 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    if os.path.exists(harvest_file):
        with open(harvest_file, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                total += 1
                try:
                    record = json.loads(line)
                    harvested = record.get("harvested", "")
                    if harvested:
                        ts = datetime.fromisoformat(harvested.replace("Z", "+00:00"))
                        if ts >= cutoff:
                            recent_7d += 1
                except (json.JSONDecodeError, ValueError):
                    continue
    return total, recent_7d


def get_last_harvest_time(runs_file):
    if not os.path.exists(runs_file):
        return None
    last_run = None
    with open(runs_file, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                last_run = json.loads(line).get("run_at")
            except json.JSONDecodeError:
                continue
    return last_run


def get_source_counts_7d(runs_file):
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    arxiv_total, filtered_total, nep_total = 0, 0, 0
    if not os.path.exists(runs_file):
        return 0, 0, 0
    with open(runs_file, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                run_at = entry.get("run_at", "")
                if run_at:
                    ts = datetime.fromisoformat(run_at.replace("Z", "+00:00"))
                    if ts >= cutoff:
                        found = entry.get("papers_found", {})
                        arxiv_total += found.get("arxiv", 0)
                        filtered_total += found.get("arxiv_filtered", 0)
                        nep_total += found.get("repec_nep", 0)
            except (json.JSONDecodeError, ValueError):
                continue
    return arxiv_total, filtered_total, nep_total


# --- Cross-source Dedup ---

def dedup_active_records(records):
    seen = {}
    for rec in records:
        summary = (rec.get("one_line_summary") or "").strip().lower()
        key = " ".join(summary[:50].split())
        if not key:
            key = rec.get("id", "")
        if key not in seen:
            seen[key] = rec
            continue
        existing = seen[key]
        pipeline = rec.get("pipeline") or "primary"
        if pipeline == "filtered":
            if (rec.get("weighted_total") or 0) > (existing.get("weighted_total") or 0):
                seen[key] = rec
        else:
            if rec.get("scored_at", "") > existing.get("scored_at", ""):
                seen[key] = rec
    return list(seen.values())


# --- Ranked List ---

def generate_ranked_md(scored_records, config):
    now = datetime.now(timezone.utc)
    data_dir = config.get("paths", {}).get("data_dir", "data")
    harvest_file = os.path.join(data_dir, "harvest.jsonl")
    runs_file = os.path.join(data_dir, "harvester_runs.jsonl")
    harvest_total, harvest_7d = load_harvest_stats(harvest_file)
    delivery = config.get("delivery", {})
    threshold = delivery.get("email", {}).get("digest_threshold", 18)

    active = [
        r for r in scored_records
        if not r.get("disqualified", False)
        and r.get("weighted_total") is not None
    ]
    active = dedup_active_records(active)
    active.sort(key=lambda r: r.get("weighted_total", 0), reverse=True)
    disqualified_count = sum(1 for r in scored_records if r.get("disqualified", False))
    pending_review = sum(1 for r in active if r.get("weighted_total", 0) >= threshold)
    last_harvest = get_last_harvest_time(runs_file) or "unknown"

    lines = [
        "# Research Intelligence Pipeline -- Ranked List",
        f"**Generated:** {now.strftime('%Y-%m-%dT%H:%M:%SZ')}",
        "",
        "## Top 10 Unextracted Papers",
        "",
        "| # | Score | Source | Entity Relevance | Age | Summary |",
        "|---|-------|--------|-----------------|-----|---------|",
    ]

    for i, r in enumerate(active[:10], 1):
        wt = r.get("weighted_total", 0)
        source = r.get("id", "").split(":")[0] if ":" in r.get("id", "") else "?"
        bot_rel = r.get("bot_relevance", {})
        tags = " ".join(
            f"{b}({s})" for b, s in sorted(
                bot_rel.items(), key=lambda x: -x[1])
            if s >= 3
        ) or "-"
        scored_at = r.get("scored_at", "")
        age_str = "?"
        if scored_at:
            try:
                age_days = (now - datetime.fromisoformat(scored_at.replace("Z", "+00:00"))).days
                age_str = f"{age_days}d"
            except ValueError:
                pass
        summary = (r.get("one_line_summary", "") or "")[:80]
        lines.append(f"| {i} | {wt:.1f} | {source} | {tags} | {age_str} | {summary} |")

    if not active[:10]:
        lines.append("| - | - | - | - | - | No scored papers yet |")

    lines += [
        "", "## Queue Statistics",
        f"- Total harvested: {harvest_total}",
        f"- Total scored: {len(scored_records)}",
        f"- Disqualified: {disqualified_count}",
        f"- Pending review (score >= {threshold}): {pending_review}",
        f"- Queue depth: {len(active)}",
        f"- Last harvest: {last_harvest}",
        f"- Papers added (7 days): {harvest_7d}", "",
    ]
    return "\n".join(lines)


# --- Daily Summary ---

def generate_daily_summary(scored_records, config):
    data_dir = config.get("paths", {}).get("data_dir", "data")
    harvest_file = os.path.join(data_dir, "harvest.jsonl")
    runs_file = os.path.join(data_dir, "harvester_runs.jsonl")
    summary_file = config.get("delivery", {}).get("daily_summary", "output/daily_summary.md")

    _, harvest_7d = load_harvest_stats(harvest_file)
    arxiv_7d, filtered_7d, nep_7d = get_source_counts_7d(runs_file)
    active = [
        r for r in scored_records
        if not r.get("disqualified", False)
        and r.get("weighted_total") is not None
    ]
    active.sort(key=lambda r: r.get("weighted_total", 0), reverse=True)
    disqualified_count = sum(1 for r in scored_records if r.get("disqualified", False))
    last_harvest = get_last_harvest_time(runs_file)
    last_harvest_age = "unknown"
    if last_harvest:
        try:
            ts = datetime.fromisoformat(last_harvest.replace("Z", "+00:00"))
            age_hours = (datetime.now(timezone.utc) - ts).total_seconds() / 3600
            last_harvest_age = f"{age_hours:.0f}h ago"
        except ValueError:
            pass

    top_title, top_score, top_entities = "none", 0, ""
    if active:
        top = active[0]
        top_title = (top.get("one_line_summary") or top.get("id", "unknown"))[:60]
        top_score = top.get("weighted_total", 0)
        bot_rel = top.get("bot_relevance", {})
        top_entities = " ".join(
            f"{b}({s})" for b, s in sorted(
                bot_rel.items(), key=lambda x: -x[1])
            if s >= 3
        )

    lines = [
        "## RESEARCH PIPELINE",
        (f"- Harvested (7d): {harvest_7d} papers | "
         f"Scored: {len(scored_records)} | DQ: {disqualified_count}"),
        (f"- Last harvest: {last_harvest_age} | arXiv ({arxiv_7d}), "
         f"filtered ({filtered_7d}), RePEc ({nep_7d})"),
        f'- Top unreviewed: "{top_title}" | {top_score:.1f}/25 | {top_entities}',
        f"- Queue depth: {len(active)} pending review", "",
    ]
    os.makedirs(os.path.dirname(summary_file) or ".", exist_ok=True)
    with open(summary_file, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    log(f"Daily summary written: {summary_file}")


# --- PDF Acquisition ---

def fetch_digest_pdfs(digest_papers, sources_dir):
    os.makedirs(sources_dir, exist_ok=True)
    results = []
    for r in digest_papers:
        paper_id = r.get("id", "")
        if not paper_id.startswith("arxiv:"):
            results.append((paper_id, None, "skipped -- not arXiv"))
            continue
        arxiv_id = paper_id.replace("arxiv:", "")
        filename = f"arxiv_{arxiv_id.replace('.', '_')}.pdf"
        filepath = os.path.join(sources_dir, filename)
        if os.path.exists(filepath) and os.path.getsize(filepath) > 1000:
            results.append((paper_id, filename, "already exists"))
            continue
        pdf_url = f"https://arxiv.org/pdf/{arxiv_id}"
        try:
            resp = http_requests.get(
                pdf_url,
                headers={"User-Agent": "research-scorer/1.0"},
                timeout=30,
            )
            resp.raise_for_status()
            content = resp.content
            if not content[:5] == b"%PDF-":
                results.append((paper_id, filename, "not a PDF"))
                continue
            with open(filepath, "wb") as f:
                f.write(content)
            size_kb = len(content) / 1024
            results.append((paper_id, filename, f"downloaded ({size_kb:.0f} KB)"))
            log(f"  PDF saved: {filename} ({size_kb:.0f} KB)")
        except Exception as e:
            results.append((paper_id, filename, f"download failed: {e}"))
            log(f"  PDF download failed for {arxiv_id}: {e}")
    return results


# --- Weekly Email Digest ---

def generate_weekly_digest(scored_records, config, dry_run=False):
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=7)
    data_dir = config.get("paths", {}).get("data_dir", "data")
    harvest_file = os.path.join(data_dir, "harvest.jsonl")
    delivery = config.get("delivery", {})
    email_cfg = delivery.get("email", {})
    threshold = email_cfg.get("digest_threshold", 18)
    max_papers = email_cfg.get("digest_max_papers", 5)
    sources_dir = delivery.get("sources_dir", "output/sources")
    _, harvest_7d = load_harvest_stats(harvest_file)

    recent_scored = []
    for r in scored_records:
        if r.get("disqualified", False):
            continue
        scored_at = r.get("scored_at", "")
        if not scored_at:
            continue
        try:
            ts = datetime.fromisoformat(scored_at.replace("Z", "+00:00"))
            if ts >= cutoff and r.get("weighted_total") is not None:
                if r["weighted_total"] >= threshold:
                    recent_scored.append(r)
        except ValueError:
            continue

    recent_scored.sort(key=lambda r: r.get("weighted_total", 0), reverse=True)
    pdf_results = fetch_digest_pdfs(recent_scored, sources_dir) if not dry_run else []

    active_all = [
        r for r in scored_records
        if not r.get("disqualified", False)
        and r.get("weighted_total") is not None
    ]
    pending_review = sum(1 for r in active_all if r.get("weighted_total", 0) >= threshold)
    queue_depth = len(active_all)

    total_scored_week = len([
        r for r in scored_records
        if r.get("scored_at", "") and
        datetime.fromisoformat(r["scored_at"].replace("Z", "+00:00")) >= cutoff
    ])

    date_str = now.strftime("%Y-%m-%d")
    subject = f"Research Digest -- {date_str} -- {total_scored_week} papers scored"

    body_lines = [
        "WEEKLY RESEARCH DIGEST",
        f"Generated: {now.strftime('%Y-%m-%dT%H:%M:%SZ')}",
        f"Papers harvested (7 days): {harvest_7d}",
        f"Papers scored: {total_scored_week}",
        f"Queue depth: {queue_depth}", "",
    ]

    if not recent_scored:
        body_lines.append(f"No high-priority papers this week (threshold: {threshold}/25).")
    else:
        display = recent_scored[:max_papers]
        body_lines.append(f"--- TOP PAPERS (score >= {threshold}) ---")
        body_lines.append("")

        # Load harvest for full titles/URLs
        harvest_data = {}
        if os.path.exists(harvest_file):
            with open(harvest_file, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        h = json.loads(line)
                        harvest_data[h["id"]] = h
                    except (json.JSONDecodeError, KeyError):
                        continue

        for i, r in enumerate(display, 1):
            wt = r.get("weighted_total", 0)
            scores = r.get("scores", {})
            gates = r.get("gates", {})
            bot_rel = r.get("bot_relevance", {})
            bot_str = " ".join(f"{b}({s})" for b, s in sorted(bot_rel.items(), key=lambda x: -x[1]))

            h = harvest_data.get(r.get("id", ""), {})
            paper_title = h.get("title", r.get("one_line_summary", r.get("id", "")))
            paper_url = h.get("url", "")

            source = r.get("id", "").split(":")[0] if ":" in r.get("id", "") else "?"

            body_lines.append(f"{i}. {paper_title}")
            body_lines.append(
                f"   Score: {wt:.2f}/25 | "
                f"CI:{scores.get('capital_impact', '?')} "
                f"TV:{scores.get('time_to_value', '?')} "
                f"RG:{scores.get('relevance', '?')} | "
                f"Gates: Ev{gates.get('evidence', '?')} Bu{gates.get('buildability', '?')}")
            body_lines.append(f"   Entity relevance: {bot_str}")
            body_lines.append(f"   Source: {source}")
            body_lines.append(f"   Summary: {r.get('one_line_summary', '')}")
            body_lines.append(f"   Rationale: {r.get('rationale', '')}")
            if paper_url:
                body_lines.append(f"   URL: {paper_url}")

            # Route to highest-scoring entity
            if bot_rel:
                highest = max(bot_rel, key=bot_rel.get)
                body_lines.append(f"   Route: {highest} (highest relevance)")
            body_lines.append("")

    if pdf_results:
        body_lines += ["--- PDF ACQUISITION ---"]
        for paper_id, filename, status in pdf_results:
            body_lines.append(f"  {paper_id}: {filename or ''} -- {status}")
        body_lines.append("")

    body_lines += [
        "--- QUEUE SUMMARY ---",
        f"Pending review (score >= {threshold}): {pending_review}",
        f"Total scored (not extracted): {queue_depth}",
        "", "--- END ---",
        "Generated by research-scorer",
    ]

    body = "\n".join(body_lines)

    if dry_run:
        print(f"\n[DRY RUN] Subject: {subject}")
        print(f"[DRY RUN] Body:\n{body}")
        return

    smtp_host = os.environ.get("SMTP_HOST", "127.0.0.1")
    smtp_port = int(os.environ.get("SMTP_PORT", "587"))
    smtp_user = os.environ.get("SMTP_USER", "")
    smtp_pass = os.environ.get("SMTP_PASS", "")
    smtp_from = os.environ.get("SMTP_FROM", "research-scorer@example.com")
    recipient = os.environ.get("RECIPIENT_EMAIL", "")

    if not recipient:
        log("WARN: RECIPIENT_EMAIL not set. Skipping email delivery.")
        return

    try:
        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"] = smtp_from
        msg["To"] = recipient
        with smtplib.SMTP(smtp_host, smtp_port, timeout=10) as s:
            s.starttls()
            s.login(smtp_user, smtp_pass)
            s.send_message(msg)
        log(f"Weekly digest email sent to {recipient}")
    except Exception as e:
        log(f"Weekly digest email FAILED: {e}. Check ranked list directly.")


# --- Main ---

def main():
    config_path = "config.yaml"
    weekly_digest = "--weekly-digest" in sys.argv
    dry_run = "--dry-run" in sys.argv
    for i, arg in enumerate(sys.argv[1:], 1):
        if arg == "--config" and i < len(sys.argv) - 1:
            config_path = sys.argv[i + 1]

    config = load_config(config_path)
    data_dir = config.get("paths", {}).get("data_dir", "data")
    scored_file = os.path.join(data_dir, "scored.jsonl")
    delivery = config.get("delivery", {})
    ranked_output = delivery.get("ranked_output", "output/research_ranked.md")

    log("=== Research Ranked Generator starting ===")

    scored_records = load_scored_records(scored_file)
    log(f"Loaded {len(scored_records)} scored records")

    os.makedirs(os.path.dirname(ranked_output) or ".", exist_ok=True)
    ranked_md = generate_ranked_md(scored_records, config)
    with open(ranked_output, "w", encoding="utf-8") as f:
        f.write(ranked_md)
    log(f"Ranked list written: {ranked_output}")

    generate_daily_summary(scored_records, config)

    if weekly_digest:
        log("Generating weekly email digest...")
        generate_weekly_digest(scored_records, config, dry_run=dry_run)

    log("=== Ranked Generator complete ===")


if __name__ == "__main__":
    main()
