import hashlib
import json
import re
import subprocess
import tempfile
import time
from pathlib import Path

import jsonschema

from . import prompts

MODEL_KEYS = {
    "titles": "review",
    "abstracts": "review",
    "papers": "review",
    "brief": "review",
    "hypotheses": "hypothesis",
}
ANSI_STYLE = re.compile(r"\x1b\[[0-9;]*m")
NUL_BEFORE_COMMAND = re.compile("\x00(?=[A-Za-z])")


def fingerprint(value):
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def clean_model_output(value):
    if isinstance(value, str):
        text = ANSI_STYLE.sub("", value)
        return NUL_BEFORE_COMMAND.sub(lambda _: "\\", text)
    if isinstance(value, list):
        return [clean_model_output(item) for item in value]
    if isinstance(value, dict):
        return {key: clean_model_output(item) for key, item in value.items()}
    return value


def call_model(config, phase, packet, run_dir, validate):
    """Reuse only validated results for the exact same prompt, data, schema, and model."""
    schema = prompts.SCHEMAS[phase]
    prompt = prompts.PROMPTS[phase]
    model_key = MODEL_KEYS[phase]
    payload = prompts.validate_input(packet, config["max_input_chars"])
    request = [
        prompt,
        packet,
        schema,
        config[f"{model_key}_model"],
        config[f"{model_key}_effort"],
    ]
    request_hash = fingerprint(request)
    cache_dir = Path(config["state_dir"]).expanduser() / "cache" / phase
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache = cache_dir / f"{request_hash}.validated.json"
    if cache.exists():
        try:
            value = json.loads(cache.read_text())
            prompts.validate_output(phase, value)
            validate(value)
            return value
        except (ValueError, jsonschema.ValidationError):
            cache.rename(cache_dir / f"{cache.stem}-{time.time_ns()}.invalid.json")
    schema_path = run_dir / f"{phase}.schema.json"
    schema_path.write_text(json.dumps(schema))
    command_prefix = [
        config["codex"],
        "exec",
        "--ignore-user-config",
        "--ignore-rules",
        "--ephemeral",
        "--skip-git-repo-check",
        "--sandbox",
        "read-only",
        "--color",
        "never",
        "-c",
        'approval_policy="never"',
        "-c",
        'web_search="disabled"',
        "-c",
        "project_doc_max_bytes=0",
        "--model",
        config[f"{model_key}_model"],
        "-c",
        f'model_reasoning_effort="{config[f"{model_key}_effort"]}"',
        "--output-schema",
        str(schema_path),
    ]
    disabled_features = []
    for feature in [
        "shell_tool",
        "unified_exec",
        "apps",
        "plugins",
        "hooks",
        "multi_agent",
        "multi_agent_v2",
        "browser_use",
        "browser_use_external",
        "in_app_browser",
        "computer_use",
        "image_generation",
    ]:
        disabled_features += ["--disable", feature]
    with tempfile.TemporaryDirectory(prefix="research-brief-model-") as empty_dir:
        for response_attempt in range(2):
            attempt = f"{phase}-{time.time_ns()}"
            output = run_dir / f"{attempt}.raw.json"
            command = [
                *command_prefix,
                "--output-last-message",
                str(output),
                *disabled_features,
                prompt,
            ]
            for model_attempt in range(2):
                try:
                    with (run_dir / f"{attempt}.stderr.log").open("a") as log:
                        subprocess.run(
                            command,
                            input=payload,
                            text=True,
                            cwd=empty_dir,
                            stdout=subprocess.DEVNULL,
                            stderr=log,
                            check=True,
                            timeout=config["model_timeout_seconds"],
                        )
                    break
                except subprocess.TimeoutExpired:
                    if model_attempt == 1:
                        raise
            try:
                value = clean_model_output(json.loads(output.read_text()))
                prompts.validate_output(phase, value)
                validate(value)
            except (ValueError, jsonschema.ValidationError):
                if response_attempt == 1:
                    raise
                continue
            temporary = cache.with_suffix(".tmp")
            temporary.write_text(json.dumps(value, indent=2))
            temporary.replace(cache)
            return value
