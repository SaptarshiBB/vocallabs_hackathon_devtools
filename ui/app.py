"""
ui/app.py

Streamlit web UI for Blast Radius: C++ PR Semantic Risk Analyzer.
Analyzes a GitHub pull request end-to-end from its URL:
PR URL -> fetch diff + metadata -> shallow clone head commit -> pipeline -> report.

Run from the repo root:
    streamlit run ui/app.py
"""

import os
import re
import sys
import shutil
import subprocess
from pathlib import Path

import requests
import streamlit as st
from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

CLONE_ROOT = ROOT / ".blast_radius" / "ui_clones"

LLM_KEY_VARS = (
    "ANTHROPIC_API_KEY",
    "GEMINI_API_KEY",
    "OPENAI_API_KEY",
    "NVIDIA_API_KEY",
    "NEMOTRON_API_KEY",
    "NVIDIA_NIM_API_KEY",
)

PR_URL_RE = re.compile(r"^https?://github\.com/([\w.-]+)/([\w.-]+)/pull/(\d+)/?$")


def parse_pr_url(url: str):
    m = PR_URL_RE.match(url.strip().rstrip("/"))
    if not m:
        raise ValueError(
            "Not a valid GitHub PR URL. Expected: https://github.com/<owner>/<repo>/pull/<number>"
        )
    return m.group(1), m.group(2), int(m.group(3))


def github_headers(diff_format: bool = False):
    headers = {"Accept": "application/vnd.github.v3.diff" if diff_format else "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def fetch_pr(owner: str, repo: str, number: int) -> dict:
    base = f"https://api.github.com/repos/{owner}/{repo}/pulls/{number}"
    meta_resp = requests.get(base, headers=github_headers(), timeout=30)
    if meta_resp.status_code == 404:
        raise RuntimeError(f"PR #{number} not found in {owner}/{repo} (private repos need GITHUB_TOKEN in .env).")
    if meta_resp.status_code == 403:
        raise RuntimeError("GitHub API rate limit reached. Add GITHUB_TOKEN to .env to raise the limit.")
    meta_resp.raise_for_status()
    meta = meta_resp.json()

    diff_resp = requests.get(base, headers=github_headers(diff_format=True), timeout=60)
    diff_resp.raise_for_status()

    head = meta.get("head") or {}
    return {
        "title": meta.get("title", ""),
        "state": meta.get("state", ""),
        "head_sha": head.get("sha", ""),
        "head_ref": head.get("ref", ""),
        "base_ref": (meta.get("base") or {}).get("ref", ""),
        "clone_url": (head.get("repo") or {}).get("clone_url") or f"https://github.com/{owner}/{repo}.git",
        "diff_text": diff_resp.text,
    }


def _run_git(args, cwd=None):
    proc = subprocess.run(
        ["git", "-c", "core.longpaths=true"] + args,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args[:2])} failed: {proc.stderr.strip()[:500]}")
    return proc.stdout.strip()


def clone_cache_dir(owner: str, repo: str, number: int) -> Path:
    return CLONE_ROOT / f"{owner}-{repo}-{number}"


def ensure_clone(clone_url: str, sha: str, cache_dir: Path) -> None:
    if not cache_dir.exists() or not any(cache_dir.iterdir()):
        cache_dir.parent.mkdir(parents=True, exist_ok=True)
        _run_git(["clone", "--depth", "1", clone_url, str(cache_dir)])
    if sha and _run_git(["rev-parse", "HEAD"], cwd=cache_dir) != sha:
        _run_git(["fetch", "--depth", "1", "origin", sha], cwd=cache_dir)
        _run_git(["checkout", "--force", sha], cwd=cache_dir)


def run_pipeline(repo_dir: Path, diff_text: str, token_budget: int, confidence_threshold: int):
    from core.diff_parser import parse_diff
    from core.callgraph import CallGraph
    from core.context_slicer import build_context_bundle
    from core.risk_model import run_risk_model
    from core.crosscheck_model import run_crosscheck_model
    from core.confidence import compute_confidence_scores
    from core.degrade import execute_with_degrade_protection
    from observability.cost_latency_log import log_model_call

    changed_funcs = parse_diff(diff_text, str(repo_dir))
    cg = CallGraph(str(repo_dir))
    cg_slices = [cg.get_n_hop_subgraph(cf.name, max_hops=2) for cf in changed_funcs]
    bundle = build_context_bundle(changed_funcs, cg_slices, diff_text, token_budget=token_budget)

    log_records = []

    def llm_pipeline():
        r_out = run_risk_model(bundle.formatted_context)
        log_records.append(log_model_call(
            model_name=r_out["model"], role="risk_model",
            prompt_tokens=r_out["prompt_tokens"], completion_tokens=r_out["completion_tokens"],
            latency_seconds=r_out["latency"],
        ))
        c_out = run_crosscheck_model(bundle.formatted_context, r_out["risks"])
        log_records.append(log_model_call(
            model_name=c_out["model"], role="crosscheck_model",
            prompt_tokens=c_out["prompt_tokens"], completion_tokens=c_out["completion_tokens"],
            latency_seconds=c_out["latency"],
        ))
        evaluated = compute_confidence_scores(r_out["risks"], c_out["checks"], confidence_threshold=confidence_threshold)
        return {
            "is_degraded": False,
            "risks": [e.to_dict() for e in evaluated],
            "token_count": bundle.total_tokens,
            "cost_usd": sum(r["cost_usd"] for r in log_records),
            "latency_seconds": sum(r["latency_seconds"] for r in log_records),
            "model_info": {"risk_model": r_out["model"], "crosscheck_model": c_out["model"]},
        }

    result = execute_with_degrade_protection(llm_pipeline, changed_funcs, cg_slices)
    return result.to_dict(), log_records


def render_report(report: dict):
    pr = report["pr"]
    result = report["result"]

    st.subheader(f"PR #{report['number']}: {pr['title']}")
    st.caption(f"`{report['owner']}/{report['repo']}` · {pr['base_ref']} ← {pr['head_ref']} · head `{pr['head_sha'][:8]}` · state: {pr['state']}")

    if result.get("is_degraded"):
        st.warning(f"**{result.get('degraded_label')}**\n\n{result.get('degraded_reason', '')}")

    risks = result.get("risks", [])
    changed_funcs = result.get("changed_functions", [])
    
    high_sev_count = sum(1 for r in risks if str(r.get("severity", "")).lower() == "high")
    needs_review_count = sum(1 for r in risks if "NEEDS HUMAN REVIEW" in str(r.get("status", "")).upper() or int(r.get("confidence_score", 0)) < 50)

    # Key Reviewer Metrics
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Changed C++ Functions", len(changed_funcs))
    c2.metric("Total Risks Detected", len(risks))
    c3.metric("High Severity Risks", high_sev_count)
    c4.metric("Needs Human Review", needs_review_count)

    st.divider()

    if not changed_funcs:
        st.info("No modified C++ functions detected in this diff (nothing to analyze).")
    elif not risks:
        st.success("✅ **PR Passed Semantic Check**: No downstream behavioral contract breakages detected.")
    else:
        if high_sev_count > 0:
            st.error("🚨 **Critical Action Required**: High-severity semantic risk or contract violation detected!")
        elif needs_review_count > 0:
            st.warning("⚠️ **Review Required**: Potential risks detected that require manual developer verification.")

    if changed_funcs:
        with st.expander("📄 Modified C++ Functions Detected", expanded=True):
            for cf in changed_funcs:
                func_name = cf.get("name", cf) if isinstance(cf, dict) else getattr(cf, "name", str(cf))
                func_file = cf.get("filepath", "") if isinstance(cf, dict) else getattr(cf, "filepath", "")
                start_l = cf.get("start_line", "") if isinstance(cf, dict) else getattr(cf, "start_line", "")
                end_l = cf.get("end_line", "") if isinstance(cf, dict) else getattr(cf, "end_line", "")
                loc_str = f" `{func_file}:{start_l}-{end_l}`" if func_file else ""
                st.markdown(f"• **`{func_name}`**{loc_str}")

    if risks:
        st.markdown("### ⚠️ Downstream Risks & Behavioral Contract Violations")
        for idx, r in enumerate(risks, 1):
            sev = str(r.get("severity", "medium")).lower()
            box = {"high": st.error, "medium": st.warning}.get(sev, st.info)
            status_text = r.get("status", "")
            with box(f"Risk #{idx} · [{sev.upper()} SEVERITY] · {status_text}"):
                st.markdown(f"**{r.get('description', '')}**")
                st.markdown(f"**Affected Component**: `{r.get('affected_function', 'unknown')}`")
                st.progress(min(100, int(r.get("confidence_score", 0))) / 100.0, text=f"Confidence: {r.get('confidence_score', 0)}/100")
                
                cc_status = str(r.get("crosscheck_status", "verified")).upper()
                note = r.get("crosscheck_note", "")
                if note:
                    st.caption(f"Audit Note: {note}")
                
                with st.expander("Technical Reasoning & Contract Breakdown"):
                    st.write(r.get("reasoning", ""))

    with st.expander("🔍 Static 2-Hop Call Graph Context Slices"):
        st.json(result.get("callgraph_slices", []))


st.set_page_config(page_title="Blast Radius", page_icon="💥", layout="wide")
st.title("💥 Blast Radius — C++ PR Semantic Risk Analyzer")

with st.sidebar:
    st.header("Analysis Settings")
    pr_url = st.text_input("GitHub Pull Request URL", placeholder="https://github.com/owner/repo/pull/123")
    token_budget = st.slider("Token Budget", 2000, 32000, 8000, step=1000)
    confidence_threshold = st.slider("Confidence Threshold", 0, 100, 50)
    analyze_clicked = st.button("Analyze PR", type="primary", disabled=not pr_url.strip())

    if st.button("Clear Clone Cache"):
        shutil.rmtree(CLONE_ROOT, ignore_errors=True)
        st.success("Clone cache cleared.")

    st.divider()
    active_keys = [k for k in LLM_KEY_VARS if os.environ.get(k)]
    if active_keys:
        st.caption("🔒 **Semantic Analysis Engine Ready**")
    else:
        st.warning("Running in **Static Mode** (callgraph only). Configure an API key in `.env` for semantic risk checks.")

if analyze_clicked:
    try:
        owner, repo, number = parse_pr_url(pr_url)
    except ValueError as e:
        st.error(str(e))
        st.stop()

    try:
        with st.spinner(f"Fetching {owner}/{repo}#{number}..."):
            pr = fetch_pr(owner, repo, number)
    except Exception as e:
        st.error(f"Failed to fetch PR: {e}")
        st.stop()

    if not pr["diff_text"].strip():
        st.warning("This PR has an empty diff — nothing to analyze.")
        st.stop()

    cache_dir = clone_cache_dir(owner, repo, number)
    try:
        with st.spinner("Cloning repository at PR head commit (cached after first run)..."):
            ensure_clone(pr["clone_url"], pr["head_sha"], cache_dir)
    except Exception as e:
        st.error(f"Failed to clone repository: {e}")
        st.stop()

    try:
        with st.spinner("Running semantic risk pipeline..."):
            result, log_records = run_pipeline(cache_dir, pr["diff_text"], token_budget, confidence_threshold)
    except Exception as e:
        st.error(f"Pipeline error: {e}")
        st.stop()

    st.session_state.report = {
        "owner": owner, "repo": repo, "number": number, "pr": pr,
        "result": result, "log_records": log_records,
    }

if st.session_state.get("report"):
    render_report(st.session_state.report)
