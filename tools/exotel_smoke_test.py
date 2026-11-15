"""
exotel_smoke_test.py - check Kagger's Exotel setup without the UI.

Stages 0-2 of docs/EXOTEL_SETUP_AND_TESTING.md:

    python tools/exotel_smoke_test.py check [--host kagger-app.azurewebsites.net]
    python tools/exotel_smoke_test.py call 98XXXXXXXX
    python tools/exotel_smoke_test.py call <zoom-india-number> --digits "<meeting-id>#,,#,,<passcode>#"
    python tools/exotel_smoke_test.py status <CallSid> [--wait]
    python tools/exotel_smoke_test.py recording <CallSid> [--out call.mp3]
    python tools/exotel_smoke_test.py hangup <CallSid>

Credentials come from the environment (or the repo's .env): EXOTEL_API_KEY,
EXOTEL_API_TOKEN, EXOTEL_SID, EXOTEL_SUBDOMAIN, EXOTEL_CALLER_ID,
EXOTEL_FLOW_APP_ID (or EXOTEL_FLOW_URL), and for --host checks
LIVE_CONCALL_WEBHOOK_SECRET.

Calls placed here use the same flow, caller ID and dial-string rules as
live_concall.place_exotel_call, but are not tied to a Kagger session: they test
telephony (dialing, passcode digits, recording, call status). The live
transcript in the UI needs a call started from Kagger's Dial In button.

Every call costs real minutes, so `call` asks before dialing (skip with --yes)
and caps the call at --time-limit seconds (default 600).

ASCII-only output: the Windows console encodes with cp1252.
"""

import argparse
import os
import re
import sys
import time

import httpx

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TERMINAL = ("completed", "failed", "busy", "no-answer", "canceled")


def _load_env():
    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(ROOT, ".env"))
    except Exception:
        pass


def _cfg():
    return {
        "key": os.getenv("EXOTEL_API_KEY", ""),
        "token": os.getenv("EXOTEL_API_TOKEN", ""),
        "sid": os.getenv("EXOTEL_SID", ""),
        "subdomain": os.getenv("EXOTEL_SUBDOMAIN", "") or "api.in.exotel.com",
        "caller_id": os.getenv("EXOTEL_CALLER_ID", ""),
        "app_id": os.getenv("EXOTEL_FLOW_APP_ID", ""),
        "flow_url": os.getenv("EXOTEL_FLOW_URL", ""),
        "secret": os.getenv("LIVE_CONCALL_WEBHOOK_SECRET", ""),
    }


def _base(cfg, version="v1"):
    return f"https://{cfg['subdomain']}/{version}/Accounts/{cfg['sid']}"


def _auth(cfg):
    return (cfg["key"], cfg["token"])


def _flow_url(cfg):
    if cfg["flow_url"]:
        return cfg["flow_url"]
    if cfg["app_id"]:
        return f"http://my.exotel.com/{cfg['sid']}/exoml/start_voice/{cfg['app_id']}"
    return ""


def _mask(s, keep=4):
    s = s or ""
    return s[:keep] + "..." if len(s) > keep else ("(set)" if s else "(missing)")


def _say(ok, label, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f" - {detail}" if detail else ""))
    return ok


def _explain_http(status):
    return {
        401: "wrong API key/token",
        403: "credentials valid but not allowed (KYC pending or feature off?)",
        404: "wrong EXOTEL_SID or EXOTEL_SUBDOMAIN (Mumbai is api.in.exotel.com, "
             "Singapore is api.exotel.com)",
    }.get(status, f"HTTP {status}")


def _digits_only(s):
    return re.sub(r"\D", "", s or "")


def build_dial_string(number, digits=""):
    """Exotel 'From' value: national-format number, then post-dial digits.

    `digits` is passed through verbatim (digits, '#', '*', and ',' pauses) so a
    test can shape the pauses for a specific bridge, e.g. Zoom's
    "<meeting id>#,,#,,<passcode>#".
    """
    sys.path.insert(0, ROOT)
    from agents.live_concall import _format_phone_exotel
    base = _format_phone_exotel(number)
    clean = re.sub(r"[^0-9#*,]", "", digits or "")
    return f"{base},,,{clean.lstrip(',')}" if clean.strip(",") else base


# ---------------------------------------------------------------------------
# check
# ---------------------------------------------------------------------------

def cmd_check(args, cfg):
    print("Exotel configuration")
    ok = True
    for name, env in (("key", "EXOTEL_API_KEY"), ("token", "EXOTEL_API_TOKEN"),
                      ("sid", "EXOTEL_SID"), ("caller_id", "EXOTEL_CALLER_ID")):
        ok &= _say(bool(cfg[name]), env,
                   _mask(cfg[name]) if name in ("key", "token") else (cfg[name] or "(missing)"))
    ok &= _say(bool(_flow_url(cfg)), "call flow (EXOTEL_FLOW_APP_ID / EXOTEL_FLOW_URL)",
               cfg["app_id"] or cfg["flow_url"] or "(missing)")
    print(f"  [INFO] API host: {cfg['subdomain']}")
    if not (cfg["key"] and cfg["token"] and cfg["sid"]):
        print("\nFill in the missing settings first.")
        return 1

    print("\nExotel API")
    numbers = None
    for url in (f"{_base(cfg, 'v2_beta')}/IncomingPhoneNumbers",
                f"{_base(cfg)}/IncomingPhoneNumbers.json"):
        try:
            r = httpx.get(url, auth=_auth(cfg), timeout=20)
        except Exception as e:
            ok &= _say(False, "reach Exotel", str(e)[:120])
            return 1
        if r.status_code == 200:
            numbers = r.text
            break
        if r.status_code in (401, 403):
            ok &= _say(False, "authenticate", _explain_http(r.status_code))
            return 1
    if numbers is None:
        ok &= _say(False, "list ExoPhones", _explain_http(r.status_code))
    else:
        _say(True, "authenticate")
        caller = _digits_only(cfg["caller_id"])[-10:]
        ok &= _say(bool(caller) and caller in _digits_only(numbers),
                   "EXOTEL_CALLER_ID is an ExoPhone on this account",
                   cfg["caller_id"] or "(missing)")

    if args.host:
        ok &= _check_host(args.host, cfg)

    print("\nAll checks passed." if ok else "\nSome checks failed (see above).")
    return 0 if ok else 1


def _check_host(host, cfg):
    host = re.sub(r"^https?://", "", host).rstrip("/")
    print(f"\nKagger endpoints on {host}")
    ok = True
    try:
        r = httpx.get(f"https://{host}/api/live-concall/schedule", timeout=30)
        ok &= _say(r.status_code == 200, "schedule API", f"HTTP {r.status_code}")
    except Exception as e:
        ok &= _say(False, "schedule API", str(e)[:120])

    if not cfg["secret"]:
        print("  [SKIP] media-stream WebSocket - set LIVE_CONCALL_WEBHOOK_SECRET to test it")
        return ok
    try:
        import simple_websocket
        ws = simple_websocket.Client.connect(
            f"wss://{host}/api/live-concall/media-stream?k={cfg['secret']}")
        # An unknown CallSid: the server logs "media-stream started (session=None)"
        # and does nothing else.
        ws.send('{"event": "start", "start": {"call_sid": "SMOKETEST"}}')
        ws.send('{"event": "stop"}')
        ws.close()
        ok &= _say(True, "media-stream WebSocket handshake",
                   "check the server log for 'media-stream started (session=None)'")
    except Exception as e:
        ok &= _say(False, "media-stream WebSocket handshake",
                   f"{str(e)[:120]} (Azure Web sockets on? LIVE_CONCALL_STREAMING=1?)")
    return ok


# ---------------------------------------------------------------------------
# call / status / recording / hangup
# ---------------------------------------------------------------------------

def _get_call(cfg, call_sid):
    r = httpx.get(f"{_base(cfg)}/Calls/{call_sid}.json", auth=_auth(cfg), timeout=20)
    if r.status_code != 200:
        raise RuntimeError(_explain_http(r.status_code))
    return (r.json() or {}).get("Call") or {}


def _print_call(call):
    for k in ("Sid", "Status", "From", "To", "StartTime", "EndTime", "Duration",
              "Price", "RecordingUrl"):
        print(f"  {k:13} {call.get(k) if call.get(k) not in (None, '') else '-'}")


def _wait_for_end(cfg, call_sid, timeout_s):
    last = None
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            call = _get_call(cfg, call_sid)
        except Exception as e:
            print(f"  status check failed: {e}")
            return None
        status = (call.get("Status") or "").lower()
        if status != last:
            print(f"  {time.strftime('%H:%M:%S')} status={status or '?'}")
            last = status
        if status in TERMINAL:
            # Exotel attaches the recording a little after the call ends.
            for _ in range(6):
                if call.get("RecordingUrl") or status != "completed":
                    break
                time.sleep(5)
                call = _get_call(cfg, call_sid)
            return call
        time.sleep(5)
    print("  still running; check later with: status <CallSid>")
    return None


def cmd_call(args, cfg):
    flow = _flow_url(cfg)
    if not (cfg["caller_id"] and flow):
        print("EXOTEL_CALLER_ID and EXOTEL_FLOW_APP_ID (or EXOTEL_FLOW_URL) must be set.")
        return 1
    dial = build_dial_string(args.number, args.digits)
    print(f"Dial string : {dial}")
    print(f"Caller ID   : {cfg['caller_id']}")
    print(f"Flow        : {flow}")
    print(f"Time limit  : {args.time_limit}s")
    if not args.yes:
        if input("Place this call? It costs real minutes. [y/N] ").strip().lower() != "y":
            print("Cancelled.")
            return 1

    form = {
        "From": dial,
        "CallerId": cfg["caller_id"],
        "CallType": "trans",
        "Url": flow,
        "TimeLimit": str(args.time_limit),
        "Record": "true",
        "CustomField": "smoketest",
    }
    if args.status_callback:
        form["StatusCallback"] = args.status_callback
    r = httpx.post(f"{_base(cfg)}/Calls/connect.json", data=form, auth=_auth(cfg), timeout=30)
    if r.status_code >= 400:
        print(f"Exotel rejected the call: {_explain_http(r.status_code)}")
        print(f"  {r.text[:400]}")
        return 1
    call = (r.json() or {}).get("Call") or {}
    call_sid = call.get("Sid", "")
    print(f"Placed. CallSid: {call_sid}")
    if args.no_wait or not call_sid:
        return 0 if call_sid else 1

    print("Waiting for the call to finish (Ctrl+C to stop watching; the call continues)...")
    final = _wait_for_end(cfg, call_sid, args.time_limit + 120)
    if final:
        _print_call(final)
        return _verdict(final)
    return 0


def _verdict(call):
    status = (call.get("Status") or "").lower()
    print()
    _say(status == "completed", "call connected and ended normally", status)
    _say(bool(call.get("RecordingUrl")), "recording available",
         "fetch it with: recording <CallSid>" if call.get("RecordingUrl") else
         "none - is Record on in the Voicebot applet / accepted by the API?")
    return 0 if status == "completed" else 1


def cmd_status(args, cfg):
    if args.wait:
        call = _wait_for_end(cfg, args.call_sid, 3 * 3600)
        if not call:
            return 1
    else:
        call = _get_call(cfg, args.call_sid)
    _print_call(call)
    return _verdict(call) if (call.get("Status") or "").lower() in TERMINAL else 0


def cmd_recording(args, cfg):
    url = _get_call(cfg, args.call_sid).get("RecordingUrl")
    if not url:
        print("No recording on this call (yet).")
        return 1
    r = httpx.get(url, timeout=120, follow_redirects=True)
    if r.status_code in (401, 403):
        r = httpx.get(url, auth=_auth(cfg), timeout=120, follow_redirects=True)
    if r.status_code != 200:
        print(f"Download failed: HTTP {r.status_code}")
        return 1
    out = args.out or f"exotel_{args.call_sid}.mp3"
    with open(out, "wb") as f:
        f.write(r.content)
    print(f"Saved {len(r.content) / 1024:.0f} KB to {out}")
    return 0


def cmd_hangup(args, cfg):
    r = httpx.post(f"{_base(cfg)}/Calls/{args.call_sid}.json", data={"Status": "completed"},
                   auth=_auth(cfg), timeout=20)
    ok = r.status_code < 400
    print("Hung up." if ok else f"Hangup failed: {_explain_http(r.status_code)}")
    return 0 if ok else 1


def main(argv=None):
    _load_env()
    p = argparse.ArgumentParser(description="Smoke-test Kagger's Exotel setup.")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("check", help="verify settings, credentials and the ExoPhone")
    c.add_argument("--host", help="also check Kagger's public endpoints on this host")

    c = sub.add_parser("call", help="place a test call through the Kagger flow")
    c.add_argument("number", help="number to dial (your mobile, or a bridge)")
    c.add_argument("--digits", default="",
                   help="post-dial digits, ',' = pause, e.g. \"12345678#,,4321#\"")
    c.add_argument("--time-limit", type=int, default=600, help="max call length, seconds")
    c.add_argument("--status-callback", help="optional URL for Exotel to POST call status to")
    c.add_argument("--no-wait", action="store_true", help="return after placing the call")
    c.add_argument("--yes", action="store_true", help="don't ask before dialing")

    c = sub.add_parser("status", help="show a call's status, duration and recording")
    c.add_argument("call_sid")
    c.add_argument("--wait", action="store_true", help="poll until the call ends")

    c = sub.add_parser("recording", help="download a call's recording")
    c.add_argument("call_sid")
    c.add_argument("--out", help="output file (default exotel_<CallSid>.mp3)")

    c = sub.add_parser("hangup", help="end a call")
    c.add_argument("call_sid")

    args = p.parse_args(argv)
    cfg = _cfg()
    handler = {"check": cmd_check, "call": cmd_call, "status": cmd_status,
               "recording": cmd_recording, "hangup": cmd_hangup}[args.cmd]
    try:
        return handler(args, cfg)
    except KeyboardInterrupt:
        print("\nStopped watching.")
        return 130
    except Exception as e:
        print(f"Error: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
