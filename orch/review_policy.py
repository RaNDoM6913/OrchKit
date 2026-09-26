from __future__ import annotations

from fnmatch import fnmatch
from typing import Any, Dict, Iterable, List

MODES = {"off", "risk_based", "required"}
REVIEWERS = {"none", "codex"}
PLACEMENTS = {"post_verify", "pre_publish"}

DEFAULT_RISK_TAGS = {
    "architecture", "security", "auth", "database", "migration",
    "git_workflow", "ci", "tests_changed", "external_integration",
}
DEFAULT_SENSITIVE_PATTERNS = (
    "**/auth/**", "**/security/**", "**/migrations/**", "**/.github/**",
    "**/auth.*", "**/security.*",
    "**/workflows/**", "workflows/**", "**/tests/**", "tests/**",
    "**/test_*", "test_*", "**/*_test.*", "*_test.*",
    "AGENTS.md", ".codex/**", ".github/**",
)


def normalize_review_policy(payload: Dict[str, Any]) -> Dict[str, Any]:
    raw = payload.get("review")
    if raw is None:
        required = bool(payload.get("required_review", False))
        raw = {"mode": "required" if required else "off"}
    if not isinstance(raw, dict):
        raise ValueError("invalid_review_policy")
    for field in ("risk_tags", "trigger_tags", "sensitive_patterns"):
        if field in raw and not isinstance(raw[field], list):
            raise ValueError("invalid_review_" + field)
    if "review_on_retry" in raw and not isinstance(raw["review_on_retry"], bool):
        raise ValueError("invalid_review_review_on_retry")
    mode = raw.get("mode", "off")
    if mode not in MODES:
        raise ValueError("invalid_review_mode")
    reviewer = raw.get("reviewer", "codex" if mode != "off" else "none")
    if reviewer not in REVIEWERS:
        raise ValueError("invalid_reviewer")
    if mode != "off" and reviewer == "none":
        raise ValueError("reviewer_required_for_enabled_review")
    placement = raw.get("placement", "post_verify")
    if placement not in PLACEMENTS:
        raise ValueError("unsupported_review_placement")
    return {
        "mode": mode,
        "reviewer": reviewer,
        "placement": placement,
        "risk_tags": list(raw.get("risk_tags", [])),
        "trigger_tags": list(raw.get("trigger_tags", sorted(DEFAULT_RISK_TAGS))),
        "sensitive_patterns": list(raw.get("sensitive_patterns", DEFAULT_SENSITIVE_PATTERNS)),
        "large_diff_files": int(raw.get("large_diff_files", 8)),
        "large_diff_bytes": int(raw.get("large_diff_bytes", 20_000)),
        "review_on_retry": bool(raw.get("review_on_retry", True)),
    }


def _path_matches(path: str, patterns: Iterable[str]) -> bool:
    normalized = path.replace("\\", "/")
    return any(fnmatch(normalized, pattern) or fnmatch("/" + normalized, pattern) for pattern in patterns)


def decide_review(payload: Dict[str, Any], manifest: Dict[str, Any], *, attempt: int = 1) -> Dict[str, Any]:
    policy = normalize_review_policy(payload)
    signals: List[str] = []
    files = manifest.get("files", {}) if isinstance(manifest, dict) else {}
    changed_paths = sorted(files)
    total_bytes = sum(int(item.get("bytes", 0)) for item in files.values() if isinstance(item, dict))
    tags = set(policy["risk_tags"])
    triggered_tags = sorted(tags.intersection(policy["trigger_tags"]))
    signals.extend(f"risk_tag:{tag}" for tag in triggered_tags)
    sensitive = [path for path in changed_paths if _path_matches(path, policy["sensitive_patterns"])]
    signals.extend(f"sensitive_path:{path}" for path in sensitive[:20])
    if len(changed_paths) >= policy["large_diff_files"]:
        signals.append(f"large_diff_files:{len(changed_paths)}")
    if total_bytes >= policy["large_diff_bytes"]:
        signals.append(f"large_diff_bytes:{total_bytes}")
    if attempt > 1 and policy["review_on_retry"]:
        signals.append(f"retry_attempt:{attempt}")

    if policy["mode"] == "off":
        required = False
    elif policy["mode"] == "required":
        required = True
        signals.insert(0, "mode:required")
    else:
        required = bool(signals)

    return {
        "required": required,
        "reviewer": policy["reviewer"] if required else "none",
        "placement": policy["placement"],
        "mode": policy["mode"],
        "signals": signals,
        "changed_files": len(changed_paths),
        "changed_bytes": total_bytes,
    }
