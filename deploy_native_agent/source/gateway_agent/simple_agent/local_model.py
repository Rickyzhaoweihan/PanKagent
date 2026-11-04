"""Native local MLX tool calling, adapted to the existing experiment loop.

Only a local pinned model directory is loaded. Dataset semantics and the system
prompt remain owned by core.py and the declarative schema pack.
"""
import json
import os
import re
import threading
import time
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
MODEL = BASE / "models" / "Qwen3.5-4B-4bit"
_RUNTIME = None
_LOCK = threading.Lock()


def openai_messages(payload):
    """Preserve tool identities and results while translating API envelopes."""
    system = payload.get("system", "")
    if isinstance(system, list):
        system = "\n".join(block.get("text", "") for block in system)
    messages = [{"role": "system", "content": system}]
    names = {}
    for message in payload["messages"]:
        content = message.get("content", "")
        if isinstance(content, str):
            messages.append({"role": message["role"], "content": content})
            continue
        text = "\n".join(block.get("text", "") for block in content if block.get("type") == "text")
        if message["role"] == "assistant":
            item = {"role": "assistant", "content": text}
            calls = []
            for block in content:
                if block.get("type") != "tool_use":
                    continue
                names[block["id"]] = block["name"]
                calls.append({"id": block["id"], "type": "function",
                              "function": {"name": block["name"], "arguments": block.get("input", {})}})
            if calls:
                item["tool_calls"] = calls
            messages.append(item)
        else:
            for block in content:
                if block.get("type") == "tool_result":
                    value = block.get("content", "")
                    messages.append({"role": "tool", "tool_call_id": block["tool_use_id"],
                                     "name": names.get(block["tool_use_id"], ""),
                                     "content": value if isinstance(value, str) else json.dumps(value)})
            if text:
                messages.append({"role": "user", "content": text})
    return messages


def openai_tools(tools):
    return [{"type": "function", "function": {"name": tool["name"],
             "description": tool.get("description", ""), "parameters": tool["input_schema"]}} for tool in tools]


def _load():
    global _RUNTIME
    if _RUNTIME is not None:
        return _RUNTIME, 0.0
    if not (MODEL / "model.safetensors").exists():
        raise RuntimeError("Local model is not downloaded; run download_small_model.py first")
    os.environ.setdefault("HF_HOME", str(BASE / ".cache-hf"))
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    from mlx_lm import load
    start = time.monotonic()
    _RUNTIME = load(str(MODEL), tokenizer_config={"trust_remote_code": False, "local_files_only": True})
    return _RUNTIME, time.monotonic() - start


def call_model(payload):
    """Synchronous drop-in for model_remote; root dispatches via to_thread.

    Malformed tool syntax is returned as model feedback, allowing the existing
    agent loop to continue. No schema-aware correction or condition rewriting
    happens in this adapter.
    """
    with _LOCK:
        (model, tokenizer), load_seconds = _load()
        import mlx.core as mx
        from mlx_lm import stream_generate
        from mlx_lm.sample_utils import make_sampler
        tools = openai_tools(payload.get("tools", []))
        prompt = tokenizer.apply_chat_template(openai_messages(payload), tools=tools,
                    tokenize=True, add_generation_prompt=True, enable_thinking=False)
        mx.random.seed(0)
        started = time.monotonic()
        pieces = []
        last = None
        for item in stream_generate(model, tokenizer, prompt,
                    max_tokens=min(2400, payload.get("max_tokens", 2400)),
                    sampler=make_sampler(temp=0.7, top_p=0.8, top_k=20), prefill_step_size=512):
            pieces.append(item.text)
            last = item
        raw = "".join(pieces)
        content, errors = [], []
        start_tag, end_tag = tokenizer.tool_call_start, tokenizer.tool_call_end
        if tokenizer.tool_parser and start_tag and end_tag:
            pattern = re.escape(start_tag) + r"(.*?)" + re.escape(end_tag)
            found = re.findall(pattern, raw, flags=re.DOTALL)
            for index, text in enumerate(found):
                try:
                    call = tokenizer.tool_parser(text.strip(), tools)
                    content.append({"type": "tool_use", "id": f"local_{time.monotonic_ns()}_{index}",
                                    "name": call["name"], "input": call["arguments"]})
                except (ValueError, TypeError, KeyError) as error:
                    errors.append(str(error))
            if start_tag in raw and not found:
                errors.append("Incomplete native tool call; response ended before the closing tool tag")
            plain = re.sub(pattern, "", raw, flags=re.DOTALL).strip()
        else:
            plain = raw
            errors.append("The installed MLX tokenizer has no native parser for this model template")
        plain = re.sub(r"<think>.*?</think>", "", plain, flags=re.DOTALL).strip()
        if plain:
            content.insert(0, {"type": "text", "text": plain})
        if errors:
            content.append({"type": "text", "text": "Local tool-call parsing feedback: " + "; ".join(errors)})
        if not content:
            content.append({"type": "text", "text": "The local model returned no tool call or text."})
        return {"content": content,
                "usage": {"input_tokens": last.prompt_tokens if last else len(prompt),
                          "output_tokens": last.generation_tokens if last else 0},
                "local": {"model": "local:qwen3.5-4b", "load_s": round(load_seconds, 3),
                          "inference_s": round(time.monotonic() - started, 3),
                          "generation_tps": round(last.generation_tps, 3) if last else None,
                          "prompt_tps": round(last.prompt_tps, 3) if last else None,
                          "peak_memory_gb": round(last.peak_memory, 3) if last else None,
                          "finish_reason": last.finish_reason if last else None,
                          "parser_errors": errors, "raw_text": raw,
                          "settings": {"thinking": False, "temperature": 0.7, "top_p": 0.8,
                                       "top_k": 20, "seed": 0, "prefill_step_size": 512}}}
