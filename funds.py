# -*- coding: utf-8 -*-
"""开跑前只读查余额。不够：飞书说明，停在该步等充值，不往后跳。"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
LOG_FILE = HERE / "pipeline.log"
WAIT_FILE = HERE / "funds-wait.json"
POLL_SEC = 90

RERUN_STEPS = (
    ("enhance", "详细地址匹配"),
    ("contact", "联系方式匹配"),
    ("email", "邮箱匹配"),
    ("tracerfy", "剩余补全"),
    ("tri", "三源合并"),
    ("detect", "核验"),
)


def log(*args):
    line = " ".join(str(a) for a in args)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    text = f"{stamp} {line}"
    try:
        sys.stdout.write(text + "\n")
        sys.stdout.flush()
    except Exception:
        pass
    try:
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(text + "\n")
    except OSError:
        pass


def _num(v):
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _alert(cfg, title: str, reason: str, extra: str = "") -> None:
    from send_feishu import notify_alert
    try:
        notify_alert(cfg, title, reason, extra=extra)
    except Exception as e:
        log("飞书欠费说明失败:", e)


def match_funds(http, base: str) -> dict:
    try:
        r = http.get(f"{base.rstrip('/')}/api/funds", timeout=40)
        if r.status_code == 404:
            return {}
        data = r.json() if r.content else {}
        return data if isinstance(data, dict) else {}
    except Exception as e:
        log("查询匹配侧余额失败，按够用继续:", e)
        return {}


def detect_funds(http, base: str) -> dict:
    try:
        r = http.get(f"{base.rstrip('/')}/api/funds", timeout=40)
        if r.status_code == 404:
            return {}
        data = r.json() if r.content else {}
        return data if isinstance(data, dict) else {}
    except Exception as e:
        log("查询核验侧余额失败，按够用继续:", e)
        return {}


def _have(block, *keys: str):
    if not isinstance(block, dict) or not block.get("ok"):
        return None
    for k in keys:
        n = _num(block.get(k))
        if n is not None:
            return n
    return None


def write_wait(step: str, have, need) -> None:
    try:
        WAIT_FILE.write_text(json.dumps({
            "step": step,
            "have": have,
            "need": need,
            "since": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def clear_wait() -> None:
    try:
        WAIT_FILE.unlink(missing_ok=True)
    except OSError:
        pass


def read_wait() -> dict | None:
    if not WAIT_FILE.is_file():
        return None
    try:
        data = json.loads(WAIT_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def wait_enough(cfg, query, need, step: str, detail: str) -> bool:
    """查不到不挡；不够飞书一次并等到够，不跳过。返回是否等过充值。"""
    alerted = False
    while True:
        have = query()
        if have is None:
            clear_wait()
            log(step, "查不到余额，按够用继续")
            return False
        need_n = float(need or 0)
        if need_n <= 0 or have >= need_n:
            if alerted:
                log(step, f"已到账 剩余 {have:g}，继续")
                _alert(cfg, f"{step}已到账", f"剩余 {have:g}，约需 {need_n:g}，继续跑。")
            else:
                log(step, f"余额 {have:g}，约需 {need_n:g}，够用")
            clear_wait()
            return alerted
        write_wait(step, have, need_n)
        if not alerted:
            _alert(
                cfg, f"{step}余额不足",
                detail or f"剩余 {have:g}，约需 {need_n:g}。",
                extra="这一步先不跑，充值后自动继续（约 90 秒查一次）。也可在调度系统 → 链路设置 选从这步重跑。",
            )
            alerted = True
        log(step, f"余额不足 剩余{have:g} 约需{need_n:g}，{POLL_SEC}秒后再查（不往后跑）")
        time.sleep(POLL_SEC)


def wait_easymail(cfg, http, base: str, rows: int) -> bool:
    need = max(int(rows or 0), 1) * 0.25

    def q():
        return _have(match_funds(http, base).get("easymail") or {}, "balance")

    return wait_enough(
        cfg, q, need, "详细地址匹配",
        f"剩余不够下详细地址（本批约 {rows} 行，按 0.25 元/行估需 {need:.0f}）。",
    )


def wait_easymail_contact(cfg, http, base: str) -> bool:
    def q():
        return _have(match_funds(http, base).get("easymail") or {}, "balance")

    return wait_enough(
        cfg, q, 0.25, "联系方式匹配",
        "剩余不够继续下联系方式单。",
    )


def wait_sd(cfg, http, base: str, rows: int) -> bool:
    need = max(int(rows or 0), 1) * 40

    def q():
        return _have(match_funds(http, base).get("sd") or {}, "remaining")

    return wait_enough(
        cfg, q, need, "邮箱匹配",
        f"点数不够跑邮箱匹配（本批约 {rows} 行，按 40 点/行估需 {need:.0f}）。",
    )


def wait_tracerfy(cfg, http, base: str, rows: int) -> bool:
    need = max(int(rows or 0), 1)

    def q():
        return _have(match_funds(http, base).get("tracerfy") or {}, "balance")

    return wait_enough(
        cfg, q, need, "剩余补全",
        f"额度不够跑剩余补全（本批约 {rows} 行）。",
    )


def wait_geeksend(cfg, http, base: str, fresh: int) -> bool:
    need = max(int(fresh or 0), 0)

    def q():
        return _have(detect_funds(http, base).get("geeksend") or {}, "balance")

    return wait_enough(
        cfg, q, need, "邮箱预检",
        f"额度不够做邮箱预检（待送检约 {fresh} 封）。",
    )


def wait_checknumber(cfg, http, base: str) -> bool:
    data = detect_funds(http, base)
    cn = data.get("checknumber") or {}
    if cn.get("skip") or cn.get("engine") != "api":
        return False

    def q():
        return _have(detect_funds(http, base).get("checknumber") or {}, "balance")

    return wait_enough(cfg, q, 0.01, "号码检测", "余额不够跑 iMessage / WhatsApp / Apple ID。")
