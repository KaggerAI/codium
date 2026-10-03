"""
Token-usage logging for Gemini calls.

Every call records one JSON line in llm_usage.log (repo root, gitignored by
*.log) and one short stderr line. The point is attribution: which step of
which agent spends the tokens, so model choices can be made on measured cost
rather than guesses.

Thinking tokens are kept separate from output tokens even though Gemini bills
them at the output rate — a HIGH-thinking call can spend far more on thoughts
than on the answer, and that is invisible if the two are summed. Audio input
is split out for the same reason: it is priced differently from text.

Logging must never break a model call, so every failure here is swallowed.
"""
import json
import os
import sys
import threading
import time

USAGE_LOG_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                              'llm_usage.log')
_WRITE_LOCK = threading.Lock()


def _modality_counts(details) -> dict:
    """[ModalityTokenCount(modality=AUDIO, token_count=n), ...] -> {'audio': n, ...}"""
    out = {}
    for d in details or []:
        modality = getattr(d, 'modality', None)
        name = str(getattr(modality, 'value', modality) or 'unknown').lower()
        out[name] = out.get(name, 0) + (getattr(d, 'token_count', 0) or 0)
    return out


def _search_count(response) -> int:
    """
    Google Search queries the model ran for this response.

    This is the billing unit for grounding on Gemini 3.x (each query is billed),
    whereas Gemini 2.5 bills once per grounded prompt however many queries ran —
    so both numbers matter: calls with searches > 0 are grounded prompts.
    """
    total = 0
    for cand in getattr(response, 'candidates', None) or []:
        gm = getattr(cand, 'grounding_metadata', None)
        total += len(getattr(gm, 'web_search_queries', None) or [])
    return total


def log_gemini_usage(response, model: str, tag: str = 'untagged', **context) -> None:
    """
    Record usage_metadata from a google-genai response.

    tag: which step made the call, e.g. 'concall.analysis'.
    context: optional extras worth grouping by later (ticker, job_id, ...).
    """
    try:
        um = getattr(response, 'usage_metadata', None)
        if um is None:
            return
        record = {
            'ts': time.strftime('%Y-%m-%dT%H:%M:%S'),
            'tag': tag or 'untagged',
            'model': model,
            'input': getattr(um, 'prompt_token_count', 0) or 0,
            'input_by_modality': _modality_counts(getattr(um, 'prompt_tokens_details', None)),
            'cached_input': getattr(um, 'cached_content_token_count', 0) or 0,
            'output': getattr(um, 'candidates_token_count', 0) or 0,
            'thinking': getattr(um, 'thoughts_token_count', 0) or 0,
            'tool_prompt': getattr(um, 'tool_use_prompt_token_count', 0) or 0,
            'total': getattr(um, 'total_token_count', 0) or 0,
            'searches': _search_count(response),
            # Background work runs on named threads (wl-warm-worker,
            # wl-refresh-*), so this separates warmer spend from user clicks.
            'thread': threading.current_thread().name,
        }
        record.update({k: v for k, v in context.items() if v not in (None, '')})

        print(f"LLM_USAGE: {record['tag']} {model} in={record['input']} "
              f"out={record['output']} think={record['thinking']} "
              f"total={record['total']} searches={record['searches']}", file=sys.stderr)
        with _WRITE_LOCK:
            with open(USAGE_LOG_PATH, 'a', encoding='utf-8') as f:
                f.write(json.dumps(record) + '\n')
    except Exception as e:
        try:
            print(f"LLM_USAGE: logging failed: {e}", file=sys.stderr)
        except Exception:
            pass


def summarize(path: str = USAGE_LOG_PATH, prefix: str = '') -> None:
    """Print token totals grouped by tag and model. Run: python -m agents.llm_usage [prefix]"""
    groups = {}
    with open(path, encoding='utf-8') as f:
        for line in f:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if prefix and not r.get('tag', '').startswith(prefix):
                continue
            bg = str(r.get('thread', '')).startswith('wl-')
            key = (r.get('tag', '?') + (' [bg]' if bg else ''), r.get('model', '?'))
            g = groups.setdefault(key, {'calls': 0, 'input': 0, 'audio': 0,
                                        'output': 0, 'thinking': 0,
                                        'grounded': 0, 'searches': 0})
            g['calls'] += 1
            g['grounded'] += 1 if r.get('searches', 0) else 0
            g['searches'] += r.get('searches', 0)
            g['input'] += r.get('input', 0)
            g['audio'] += r.get('input_by_modality', {}).get('audio', 0)
            g['output'] += r.get('output', 0)
            g['thinking'] += r.get('thinking', 0)

    cols = ('calls', 'input', 'audio', 'output', 'thinking', 'grounded', 'searches')
    print(f"{'tag':<36} {'model':<26}" + ''.join(f"{c:>11}" for c in cols))
    for (tag, model), g in sorted(groups.items(), key=lambda kv: -kv[1]['input']):
        print(f"{tag:<36} {model:<26}" + ''.join(f"{g[c]:>11,}" for c in cols))


if __name__ == '__main__':
    summarize(prefix=sys.argv[1] if len(sys.argv) > 1 else '')
