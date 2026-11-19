"""POST /api/module-model — a core module calling the model (Module API 2.7, hubmod.model): the page identifies the
module by its launch secret, checks the model capability and the owner's permission (modules.model) and the limits,
then makes the call itself (core/modelcall.py) on the active pool account: the token never reaches the module.
Spending is recorded as a module.model event in the journal ("Expenses", core/usage.py)."""
import functools
import hmac
import json
import time

from core import modelcall

MAX_BODY = 1 << 20   # 100,000 characters in UTF-8 and JSON — with a margin
LOCAL = ("127.0.0.1", "::1", "::ffff:127.0.0.1")


def caller(state_dir, headers, peer):
    """Name of the module that made the request, or modelcall.Error denied: it must come from this machine and be
    started by the hub (launch file with the same secret)."""
    from core import modrun, modules
    name, secret = headers.get("X-Hub-Module") or "", headers.get("X-Hub-Module-Secret") or ""
    if peer not in LOCAL or not modules.NAME.fullmatch(name) or not secret:
        raise modelcall.Error("denied", "a model call is for a module process started by the hub")
    run = modrun.run_of(state_dir, name) or {}
    if not isinstance(run.get("secret"), str) or not hmac.compare_digest(run["secret"].encode(), secret.encode()):
        raise modelcall.Error("denied", f"module {name} is not running under the hub with this secret")
    return name


def allowed(state_dir, cfg, name):
    """The model capability in the module's module.json and its name in modules.model — or Error denied/off."""
    from core import modules
    if not modelcall.has_right(cfg, state_dir, name):
        raise modelcall.Error("denied", f"module {name} has no {modelcall.CAP} capability in module.json")
    if name not in (modules.settings(cfg).get("model") or []):
        raise modelcall.Error("off", f"the owner has not allowed the model to module {name} (Modules → Allow the model)")


def answer(state_dir, cfg, account, headers, peer, raw, limits, put, call=None, clock=time.time):
    """(HTTP code, response). put(type, entity, data) reports a fact to the core; module.model is written for every call
    that reached claude."""
    try:
        name = caller(state_dir, headers, peer)
        allowed(state_dir, cfg, name)
        try:
            body = json.loads(raw or b"{}")
            args = modelcall.checked(body)
        except (ValueError, RecursionError):   # not JSON, or nested deeper than Python can parse
            raise modelcall.Error("bad", "the body is not JSON of a sane depth") from None
        limits.take(name)
    except modelcall.Error as e:
        return modelcall.STATUS[e.code], {"code": e.code, "error": str(e)}
    if call is None:
        from core import codexmodel
        if codexmodel.on_codex(cfg=cfg):   # Codex (experimental): a hub on Codex has no claude — the hub's Codex
            call = functools.partial(modelcall.call_codex, state_dir=state_dir, by=f"mod:{name}")
            account = None                 # the Claude pool is not what answered
    start = clock()
    try:
        got = (call or modelcall.call)(*args, account=account)
        out, fact = (200, got), {"ok": True, "code": None, "cost_usd": got.get("usd"), "model": got.get("model")}
    except modelcall.Error as e:
        out = (modelcall.STATUS[e.code], {"code": e.code, "error": str(e)})
        fact = {"ok": False, "code": e.code, "cost_usd": e.usd, "model": e.model or args[2]} if e.ran else None
    finally:
        limits.give(name)
    if fact is not None:   # claude never ran (HUB_NO_MODEL, no claude) — nothing was spent
        put("module.model", name, {"module": name, "account": account, "ms": int((clock() - start) * 1000), **fact})
    return out
