"""
Research Paper Harvester
Fetches papers from arXiv (OAI-PMH) and RePEc NEP (RSS).
Appends to data/harvest.jsonl. Logs runs to data/harvester_runs.jsonl.

Usage: python research_harvester.py [--config config.yaml]
Cron:  0 10 * * * python research_harvester.py
"""

import hashlib
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta

import feedparser
import requests
import yaml


def log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[{ts}] {msg}")


def load_config(config_path="config.yaml"):
    with open(config_path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_existing_ids(harvest_file):
    ids = set()
    if os.path.exists(harvest_file):
        with open(harvest_file, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ids.add(json.loads(line)["id"])
                except (json.JSONDecodeError, KeyError):
                    pass
    return ids


def load_last_harvest_date(run_log_file):
    default = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d")
    if not os.path.exists(run_log_file):
        return default
    last_date = default
    with open(run_log_file, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                run_at = entry.get("run_at", "")
                if run_at:
                    last_date = run_at[:10]
            except json.JSONDecodeError:
                pass
    return last_date


def make_harvest_record(paper_id, title, authors, abstract, source, category,
                        published, url, pipeline="primary"):
    return {
        "id": paper_id, "title": title, "authors": authors,
        "abstract": abstract, "source": source, "category": category,
        "published": published,
        "harvested": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "url": url, "pipeline": pipeline,
        "scored": False, "disqualified": False, "disqualify_reason": None,
    }


def keyword_prefilter(title, abstract, tier1, tier2):
    text = (title + " " + abstract).lower()
    return any(kw in text for kw in tier1) or any(kw in text for kw in tier2)


def fetch_arxiv_oaipmh(oai_url, set_specs, from_date, delay_sec,
                       pipeline="primary", keyword_filter=None, daily_ceiling=None):
    papers, errors = [], []
    filtered_count = 0
    ceiling_reached = False
    ns = {"oai": "http://www.openarchives.org/OAI/2.0/",
          "arxiv": "http://arxiv.org/OAI/arXiv/"}

    for set_spec in set_specs:
        if ceiling_reached:
            break
        try:
            resumption_token = None
            while True:
                if ceiling_reached:
                    break
                if resumption_token:
                    params = {"verb": "ListRecords", "resumptionToken": resumption_token}
                else:
                    params = {"verb": "ListRecords", "metadataPrefix": "arXiv",
                              "set": set_spec, "from": from_date}
                resp = requests.get(oai_url, params=params, timeout=30)
                resp.raise_for_status()
                root = ET.fromstring(resp.text)

                error_elem = root.find(".//oai:error", ns)
                if error_elem is not None:
                    code = error_elem.get("code", "unknown")
                    if code == "noRecordsMatch":
                        log(f"arXiv {set_spec}: no records since {from_date}")
                        break
                    errors.append(f"arXiv {set_spec}: OAI error {code}")
                    break

                for record in root.findall(".//oai:record", ns):
                    try:
                        header = record.find("oai:header", ns)
                        if header is not None and header.get("status") == "deleted":
                            continue
                        metadata = record.find(".//arxiv:arXiv", ns)
                        if metadata is None:
                            continue
                        ident = header.find("oai:identifier", ns)
                        ident_text = ident.text if ident is not None else ""
                        arxiv_id = ident_text.split(":")[-1] if ident_text else ""
                        paper_id = f"arxiv:{arxiv_id}"
                        te = metadata.find("arxiv:title", ns)
                        title = ""
                        if te is not None and te.text:
                            title = te.text.strip().replace("\n", " ")
                        authors = []
                        for a in metadata.findall("arxiv:authors/arxiv:author", ns):
                            fn = a.find("arxiv:forenames", ns)
                            ln = a.find("arxiv:keyname", ns)
                            parts = []
                            if fn is not None and fn.text:
                                parts.append(fn.text.strip())
                            if ln is not None and ln.text:
                                parts.append(ln.text.strip())
                            if parts:
                                authors.append(" ".join(parts))
                        ae = metadata.find("arxiv:abstract", ns)
                        abstract = ""
                        if ae is not None and ae.text:
                            abstract = ae.text.strip().replace("\n", " ")
                        ce = metadata.find("arxiv:categories", ns)
                        categories = ce.text.strip() if ce is not None and ce.text else ""
                        ds = header.find("oai:datestamp", ns)
                        published = ds.text if ds is not None and ds.text else ""
                        url = f"https://arxiv.org/abs/{arxiv_id}"
                        if keyword_filter:
                            t1, t2 = keyword_filter
                            if not keyword_prefilter(title, abstract, t1, t2):
                                filtered_count += 1
                                continue
                            if daily_ceiling and len(papers) >= daily_ceiling:
                                log(f"WARN: daily ceiling ({daily_ceiling}) reached")
                                ceiling_reached = True
                                break
                        source_name = "arxiv" if pipeline == "primary" else "arxiv_filtered"
                        papers.append(make_harvest_record(
                            paper_id=paper_id, title=title, authors=authors,
                            abstract=abstract, source=source_name,
                            category=categories, published=published,
                            url=url, pipeline=pipeline))
                    except Exception as e:
                        errors.append(f"arXiv parse error: {e}")
                tok = root.find(".//oai:resumptionToken", ns)
                if tok is not None and tok.text and tok.text.strip():
                    resumption_token = tok.text.strip()
                    time.sleep(delay_sec)
                else:
                    break
            time.sleep(delay_sec)
        except Exception as e:
            errors.append(f"arXiv {set_spec}: {e}")
    if keyword_filter:
        log(f"Filtered pipeline: {filtered_count} papers rejected by keyword pre-filter")
    return papers, errors


def fetch_repec_nep_rss(feeds, delay_sec):
    papers, errors = [], []
    for feed_name, feed_url in feeds.items():
        try:
            feed = feedparser.parse(feed_url)
            if feed.bozo and not feed.entries:
                errors.append(f"NEP {feed_name}: feed parse error")
                time.sleep(delay_sec)
                continue
            for entry in feed.entries:
                try:
                    link = entry.get("link", "")
                    guid = entry.get("id", entry.get("guid", ""))
                    raw_id = guid or link
                    if not raw_id:
                        continue
                    arxiv_match = (re.search(r"arx:papers:(\d{4}\.\d{4,5})", raw_id + link)
                                   or re.search(r"arxiv\.org/abs/(\d{4}\.\d{4,5})", raw_id + link))
                    if arxiv_match:
                        paper_id = f"arxiv:{arxiv_match.group(1)}"
                    elif raw_id.startswith("http"):
                        paper_id = f"repec:{hashlib.sha256(raw_id.encode()).hexdigest()[:12]}"
                    else:
                        paper_id = f"repec:{raw_id}"
                    title = entry.get("title", "").strip().replace("\n", " ")
                    if not title:
                        continue
                    authors = []
                    creator = entry.get("author", entry.get("dc_creator", ""))
                    if creator:
                        authors = [a.strip() for a in creator.split(",") if a.strip()]
                    abstract = entry.get("description", entry.get("summary", ""))
                    if abstract:
                        abstract = re.sub(r"<[^>]+>", "", abstract.strip().replace("\n", " "))
                    published = entry.get("published", entry.get("dc_date", ""))
                    if published:
                        published = published[:10]
                    papers.append(make_harvest_record(
                        paper_id=paper_id, title=title, authors=authors,
                        abstract=abstract or "", source="repec_nep",
                        category=feed_name, published=published, url=link))
                except Exception as e:
                    errors.append(f"NEP {feed_name} entry: {e}")
            time.sleep(delay_sec)
        except Exception as e:
            errors.append(f"NEP {feed_name}: {e}")
    return papers, errors


def dedup_and_append(new_papers, existing_ids, harvest_file):
    unique, duped = [], 0
    for paper in new_papers:
        if paper["id"] in existing_ids:
            duped += 1
            continue
        existing_ids.add(paper["id"])
        unique.append(paper)
    if unique:
        with open(harvest_file, "a", encoding="utf-8") as f:
            for paper in unique:
                f.write(json.dumps(paper) + "\n")
    return len(unique), duped


def check_silent_failure(harvest_file, hours):
    if not os.path.exists(harvest_file):
        return "WARN: harvest.jsonl does not exist"
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    with open(harvest_file, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
                harvested = record.get("harvested", "")
                if harvested:
                    ts = datetime.fromisoformat(harvested.replace("Z", "+00:00"))
                    if ts >= cutoff:
                        return None
            except (json.JSONDecodeError, ValueError):
                continue
    return f"WARN: no new papers in {hours}h"


def check_per_source_failure(run_log_file, threshold, active_sources):
    if not os.path.exists(run_log_file):
        return []
    warnings = []
    consecutive_zeros = {s: 0 for s in active_sources}
    with open(run_log_file, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                found = json.loads(line).get("papers_found", {})
                for source in consecutive_zeros:
                    if found.get(source, 0) == 0:
                        consecutive_zeros[source] += 1
                    else:
                        consecutive_zeros[source] = 0
            except json.JSONDecodeError:
                continue
    for source, count in consecutive_zeros.items():
        if count >= threshold:
            if source.startswith("arxiv") and datetime.now(timezone.utc).weekday() in (4, 5):
                continue
            warnings.append(f"WARN: {source} returned 0 papers for {count} consecutive runs")
    return warnings


def main():
    config_path = "config.yaml"
    for i, arg in enumerate(sys.argv[1:], 1):
        if arg == "--config" and i < len(sys.argv) - 1:
            config_path = sys.argv[i + 1]

    config = load_config(config_path)
    data_dir = config.get("paths", {}).get("data_dir", "data")
    os.makedirs(data_dir, exist_ok=True)
    harvest_file = os.path.join(data_dir, "harvest.jsonl")
    run_log_file = os.path.join(data_dir, "harvester_runs.jsonl")

    log("=== Research Harvester starting ===")
    existing_ids = load_existing_ids(harvest_file)
    log(f"Existing papers: {len(existing_ids)}")
    from_date = load_last_harvest_date(run_log_file)
    log(f"Fetching papers since: {from_date}")

    all_papers, all_errors = [], []
    source_counts, source_status = {}, {}
    sources = config.get("sources", {})

    arxiv_cfg = sources.get("arxiv", {})
    if arxiv_cfg.get("enabled", False):
        log("Fetching arXiv OAI-PMH (primary)...")
        papers, errors = fetch_arxiv_oaipmh(
            oai_url=arxiv_cfg.get("oai_url", "https://oaipmh.arxiv.org/oai"),
            set_specs=arxiv_cfg.get("sets", []),
            from_date=from_date, delay_sec=arxiv_cfg.get("delay_sec", 3))
        source_counts["arxiv"] = len(papers)
        source_status["arxiv"] = "error" if errors else "ok"
        all_papers.extend(papers)
        all_errors.extend(errors)
        log(f"arXiv: {len(papers)} papers, {len(errors)} errors")

    filt_cfg = sources.get("arxiv_filtered", {})
    if filt_cfg.get("enabled", False):
        log("Fetching arXiv (filtered)...")
        t1 = set(filt_cfg.get("keyword_tier1", []))
        t2 = set(filt_cfg.get("keyword_tier2", []))
        papers, errors = fetch_arxiv_oaipmh(
            oai_url=filt_cfg.get(
                "oai_url",
                arxiv_cfg.get("oai_url", "https://oaipmh.arxiv.org/oai")),
            set_specs=filt_cfg.get("sets", []),
            from_date=from_date, delay_sec=filt_cfg.get("delay_sec", 3),
            pipeline="filtered", keyword_filter=(t1, t2),
            daily_ceiling=filt_cfg.get("daily_ceiling", 30))
        source_counts["arxiv_filtered"] = len(papers)
        source_status["arxiv_filtered"] = "error" if errors else "ok"
        all_papers.extend(papers)
        all_errors.extend(errors)
        log(f"arXiv filtered: {len(papers)} papers post-filter, {len(errors)} errors")

    nep_cfg = sources.get("repec_nep", {})
    if nep_cfg.get("enabled", False):
        log("Fetching RePEc NEP RSS...")
        papers, errors = fetch_repec_nep_rss(
            feeds=nep_cfg.get("feeds", {}), delay_sec=nep_cfg.get("delay_sec", 5))
        source_counts["repec_nep"] = len(papers)
        source_status["repec_nep"] = "error" if errors else "ok"
        all_papers.extend(papers)
        all_errors.extend(errors)
        log(f"RePEc NEP: {len(papers)} papers, {len(errors)} errors")

    new_count, dedup_count = dedup_and_append(all_papers, existing_ids, harvest_file)
    log(f"Total found: {len(all_papers)}, new: {new_count}, deduped: {dedup_count}")

    monitoring = config.get("monitoring", {})
    run_warnings = []
    sw = check_silent_failure(harvest_file, monitoring.get("silent_failure_hours", 72))
    if sw:
        run_warnings.append(sw)
        log(sw)
    active = [s for s in source_counts if source_status.get(s) != "disabled"]
    zero_thresh = monitoring.get("consecutive_zero_threshold", 3)
    for w in check_per_source_failure(run_log_file, zero_thresh, active):
        run_warnings.append(w)
        log(w)

    with open(run_log_file, "a", encoding="utf-8") as f:
        f.write(json.dumps({
            "run_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "papers_found": source_counts, "papers_new": new_count,
            "papers_deduped": dedup_count, "errors": all_errors,
            "warnings": run_warnings, "source_status": source_status,
            "harvest_total": len(existing_ids),
        }) + "\n")

    log(f"=== Harvester complete. {new_count} new papers added. ===")


if __name__ == "__main__":
    main()
