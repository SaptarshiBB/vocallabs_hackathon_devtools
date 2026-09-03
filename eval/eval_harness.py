"""
eval/eval_harness.py

Evaluation harness for Blast Radius semantic risk analyzer.
Evaluates a directory of test cases (PR diff + C++ codebase + ground truth JSON)
and computes Precision, Recall, and F1 Score.
"""

import os
import sys
import json
import glob
from dataclasses import dataclass
from typing import List, Dict, Any, Tuple

from dotenv import load_dotenv
load_dotenv()

# Ensure project root is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

@dataclass
class EvalMetrics:
    total_cases: int
    true_positives: int
    false_positives: int
    false_negatives: int
    true_negatives: int
    precision: float
    recall: float
    f1_score: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total_cases": self.total_cases,
            "true_positives": self.true_positives,
            "false_positives": self.false_positives,
            "false_negatives": self.false_negatives,
            "true_negatives": self.true_negatives,
            "precision": round(self.precision, 4),
            "recall": round(self.recall, 4),
            "f1_score": round(self.f1_score, 4),
        }


def run_pipeline_for_case(case_dir: str, use_mock_if_offline: bool = True) -> Dict[str, Any]:
    """
    Runs Blast Radius pipeline on a single test case directory.
    """
    diff_file = os.path.join(case_dir, "pr.diff")
    repo_dir = os.path.join(case_dir, "repo")

    if not os.path.exists(diff_file) or not os.path.exists(repo_dir):
        raise FileNotFoundError(f"Missing pr.diff or repo/ in {case_dir}")

    with open(diff_file, "r", encoding="utf-8") as f:
        diff_text = f.read()

    from core.diff_parser import parse_diff
    from core.callgraph import CallGraph
    from core.context_slicer import build_context_bundle
    from core.confidence import compute_confidence_scores

    changed_funcs = parse_diff(diff_text, repo_dir)
    cg = CallGraph(repo_dir)

    cg_slices = []
    for cf in changed_funcs:
        slice_res = cg.get_n_hop_subgraph(cf.name, max_hops=2)
        cg_slices.append(slice_res)

    bundle = build_context_bundle(changed_funcs, cg_slices, diff_text, token_budget=8000)

    # Check for live LLM API keys
    api_available = any(k in os.environ for k in ("ANTHROPIC_API_KEY", "GEMINI_API_KEY", "OPENAI_API_KEY", "NVIDIA_API_KEY", "NEMOTRON_API_KEY", "NVIDIA_NIM_API_KEY"))

    if api_available and not use_mock_if_offline:
        from core.risk_model import run_risk_model
        from core.crosscheck_model import run_crosscheck_model
        from core.degrade import execute_with_degrade_protection

        def llm_pipeline():
            r_out = run_risk_model(bundle.formatted_context)
            c_out = run_crosscheck_model(bundle.formatted_context, r_out["risks"])
            evaluated = compute_confidence_scores(r_out["risks"], c_out["checks"])
            return {
                "risks": [e.to_dict() for e in evaluated],
                "is_degraded": False
            }

        res = execute_with_degrade_protection(llm_pipeline, changed_funcs, cg_slices)
        return res.to_dict()
    else:
        # Mock mode allowed strictly for eval harness verification when offline
        gt_file = os.path.join(case_dir, "ground_truth.json")
        has_gt_regression = False
        gt_expected = []
        if os.path.exists(gt_file):
            with open(gt_file, "r", encoding="utf-8") as f:
                gt_data = json.load(f)
                has_gt_regression = gt_data.get("has_contract_break", gt_data.get("has_regression", False))
                gt_expected = gt_data.get("expected_risks", [])

        mock_risks = []
        if has_gt_regression:
            mock_risks.append({
                "description": f"Regression break detected in test case",
                "affected_function": "target_function",
                "severity": "high",
                "reasoning": "Mocked eval ground truth match",
                "confidence_score": 90,
                "crosscheck_status": "agreed",
                "needs_human_review": False,
                "status": "CONFIRMED RISK"
            })

        return {
            "is_degraded": False,
            "risks": mock_risks,
            "changed_functions": [cf.to_dict() for cf in changed_funcs]
        }


def evaluate_benchmark(test_cases_dir: str = "eval/test_cases", use_live: bool = False) -> EvalMetrics:
    """
    Evaluates all labeled benchmark cases under test_cases_dir.
    Default (use_live=False) runs the mocked pipeline whose risks are derived from
    ground_truth.json, so metrics are structural only. use_live=True invokes the
    real LLM pipeline and requires at least one API key in env/.env.
    """
    case_dirs = [d for d in glob.glob(os.path.join(test_cases_dir, "*")) if os.path.isdir(d)]
    case_dirs.sort()

    tp, fp, fn, tn = 0, 0, 0, 0

    print(f"\n[+] Running Evaluation Harness across {len(case_dirs)} benchmark cases...\n")
    print(f"{'TEST CASE':<30} | {'GROUND TRUTH':<15} | {'PREDICTED':<15} | {'RESULT':<10}")
    print("-" * 78)

    for cdir in case_dirs:
        case_name = os.path.basename(cdir)
        gt_file = os.path.join(cdir, "ground_truth.json")
        
        has_gt_regression = False
        if os.path.exists(gt_file):
            with open(gt_file, "r", encoding="utf-8") as f:
                gt_data = json.load(f)
                has_gt_regression = gt_data.get("has_contract_break", gt_data.get("has_regression", False))

        pipeline_out = run_pipeline_for_case(cdir, use_mock_if_offline=not use_live)
        predicted_risks = [r for r in pipeline_out.get("risks", []) if r.get("confidence_score", 0) >= 50]
        has_pred_regression = len(predicted_risks) > 0

        gt_str = "BREAKAGE" if has_gt_regression else "SAFE"
        pred_str = f"RISKS ({len(predicted_risks)})" if has_pred_regression else "SAFE"

        if has_gt_regression and has_pred_regression:
            tp += 1
            res_str = "TP [PASS]"
        elif not has_gt_regression and not has_pred_regression:
            tn += 1
            res_str = "TN [PASS]"
        elif not has_gt_regression and has_pred_regression:
            fp += 1
            res_str = "FP [FAIL]"
        else:
            fn += 1
            res_str = "FN [FAIL]"

        print(f"{case_name:<30} | {gt_str:<15} | {pred_str:<15} | {res_str:<10}")

    total = len(case_dirs)
    precision = tp / (tp + fp) if (tp + fp) > 0 else 1.0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 1.0
    f1 = 2 * (precision * recall) / (precision + recall) if (precision + recall) > 0 else 0.0

    print("-" * 78)
    print(f"Summary: TP={tp}, TN={tn}, FP={fp}, FN={fn}")
    print(f"Precision: {precision:.4f} | Recall: {recall:.4f} | F1 Score: {f1:.4f}\n")

    return EvalMetrics(
        total_cases=total,
        true_positives=tp,
        false_positives=fp,
        false_negatives=fn,
        true_negatives=tn,
        precision=precision,
        recall=recall,
        f1_score=f1
    )


if __name__ == "__main__":
    live = "--live" in sys.argv
    api_available = any(k in os.environ for k in ("ANTHROPIC_API_KEY", "GEMINI_API_KEY", "OPENAI_API_KEY", "NVIDIA_API_KEY", "NEMOTRON_API_KEY", "NVIDIA_NIM_API_KEY"))
    if live:
        if not api_available:
            print("[!] --live requested but no LLM API key found in env/.env (ANTHROPIC_API_KEY / GEMINI_API_KEY / OPENAI_API_KEY / NVIDIA_API_KEY / NEMOTRON_API_KEY / NVIDIA_NIM_API_KEY).")
            sys.exit(1)
        print("[+] LIVE LLM MODE: benchmark will call real models (costs tokens).")
    else:
        print("[+] MOCK MODE: risks are derived from ground_truth.json; metrics are structural only. Use --live with an API key for real model metrics.")
    print("[+] Testing eval/eval_harness.py standalone...")
    metrics = evaluate_benchmark("eval/test_cases", use_live=live)
    assert metrics.total_cases >= 2
    assert metrics.f1_score > 0.0
    print("[+] eval/eval_harness.py tests completed successfully!")
