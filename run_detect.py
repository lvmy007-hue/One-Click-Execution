# -*- coding: utf-8 -*-
"""三源合并 ZIP → 核验导入 → API预检 → 发信与号码检测。
iMessage / WhatsApp / Apple ID 共用一把 CheckNumber 密钥，必须一项彻底结束
（进程停 + 已扣费结果取回）后才开下一项。发信可与第一项并行。"""
from __future__ import annotations

import argparse
import atexit
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

HERE = Path(__file__).resolve().parent
DEFAULT_CFG = HERE / "config.json"
DETECT_PID = HERE / "detect.pid"

from run_pipeline import PipelineError, _session, keep_one_tri_zip, load_cfg, log  # noqa: E402


def _ds(resp: requests.Response) -> dict:
    try:
        data = resp.json()
    except Exception:
        raise PipelineError(f"核验 HTTP {resp.status_code} 非 JSON: {resp.text[:300]}")
    if not isinstance(data, dict):
        raise PipelineError(f"核验返回异常: {data!r}"[:300])
    if resp.status_code >= 400:
        raise PipelineError(data.get("message") or data.get("error") or f"HTTP {resp.status_code}")
    return data


def ds_ok(resp: requests.Response) -> dict:
    data = _ds(resp)
    if data.get("success") is False:
        raise PipelineError(data.get("message") or "核验接口失败")
    return data


def match_day_dir(cfg: dict, date_str: str = "") -> Path:
    raw = (date_str or "").strip()
    if len(raw) >= 8 and raw[:8].isdigit():
        dt = datetime.strptime(raw[:8], "%Y%m%d")
    elif len(raw) >= 10 and raw[4] == "-":
        dt = datetime.strptime(raw[:10], "%Y-%m-%d")
    else:
        dt = datetime.now()
    root = Path(cfg.get("match_dest_root") or r"D:\桌面\地址匹配源文件\匹配数据")
    folder = root / f"{dt.month}月" / f"{dt.month}.{dt.day:02d}"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def wait_detect(http: requests.Session, base: str, exe: str, timeout: int = 90) -> None:
    def up() -> bool:
        try:
            r = http.get(f"{base}/api/board/summary", timeout=6)
            return r.status_code == 200 and (r.json() or {}).get("success") is True
        except Exception:
            return False

    try:
        from tray_apps import ensure_running
        ensure_running()
    except Exception:
        pass
    if up():
        log("核验系统已在运行", base)
        return
    if not exe or not Path(exe).is_file():
        raise PipelineError(f"核验系统未打开，且找不到 {exe}")
    log("正在启动", exe)
    try:
        from tray_apps import launch_exe
        launch_exe(exe)
    except Exception:
        subprocess.Popen([exe], cwd=str(Path(exe).parent), close_fds=True)
    t0 = time.time()
    while time.time() - t0 < timeout:
        if up():
            log("核验系统已就绪")
            return
        time.sleep(2)
    raise PipelineError("核验系统启动超时")


def today_tri_item(http: requests.Session, matching_base: str) -> dict | None:
    data = http.get(f"{matching_base}/overview/tri-merges", timeout=30).json()
    today = datetime.now().strftime("%Y-%m-%d")
    for it in data.get("items") or []:
        day = (it.get("day") or it.get("date") or "")[:10]
        if day == today and it.get("has_zip") and it.get("id"):
            return it
    return None


def latest_tri(http: requests.Session, matching_base: str, prefer_today: bool = True) -> dict:
    item = today_tri_item(http, matching_base)
    if item:
        return item
    if prefer_today:
        raise PipelineError("没有当天三源合并 ZIP")
    data = http.get(f"{matching_base}/overview/tri-merges", timeout=30).json()
    for it in data.get("items") or []:
        if it.get("has_zip") and it.get("id"):
            return it
    raise PipelineError("没有可下载的三源合并 ZIP")


def download_tri_zip(http: requests.Session, matching_base: str, item: dict, dest: Path) -> Path:
    mid = int(item["id"])
    from run_pipeline import promote_tri_zip, tri_keep_name
    dest.mkdir(parents=True, exist_ok=True)
    name = tri_keep_name(item) if item.get("id") else (item.get("zip_name") or f"tri_{mid}.zip")
    path = dest / Path(name).name
    log("下载三源合并", name, "→", path)
    r = http.get(f"{matching_base}/overview/tri-download/{mid}", timeout=180, stream=True)
    if r.status_code >= 400:
        raise PipelineError(f"下载三源合并失败 HTTP {r.status_code}")
    path.write_bytes(r.content)
    log(f"已保存 {path}  {path.stat().st_size} 字节")
    return promote_tri_zip(path, item)


def filter_tri_zip(cfg: dict, path: Path) -> Path:
    """去掉当天店表里没有的原表日期（例如测店带进来的 9.29）。"""
    from run_pipeline import dest_keep_dates, name_ymd
    import zipfile
    keep = dest_keep_dates(cfg)
    drop = []
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
        for name in names:
            ymd = name_ymd(Path(name).name)
            if ymd and ymd not in keep:
                drop.append(name)
        if not drop:
            return path
        tmp = path.with_name(path.stem + ".tmp.zip")
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as out:
            for name in names:
                if name in drop:
                    log("三源剔除", name)
                    continue
                out.writestr(name, zf.read(name))
        tmp.replace(path)
    log(f"三源已剔除 {len(drop)} 张非当天店表，{path.stat().st_size} 字节")
    try:
        from send_feishu import notify_skipped
        notify_skipped(
            cfg, "三源合并剔除",
            [(Path(n).name, "原表日期不在当天店表里（测店或旧表混入当天合并）") for n in drop],
            extra="已从今日三源包去掉，其余表继续核验。",
        )
    except Exception as e:
        log("三源剔除飞书说明失败:", e)
    return path


def import_zip(http: requests.Session, base: str, zip_path: Path) -> dict:
    return import_zips(http, base, [zip_path])


def _preview_zips(http: requests.Session, base: str, paths: list[Path]) -> dict:
    opened = []
    try:
        payload = []
        for p in paths:
            fh = p.open("rb")
            opened.append(fh)
            payload.append(("files", (p.name, fh, "application/zip")))
        return ds_ok(http.post(f"{base}/api/import/batch-preview", files=payload, timeout=180))
    finally:
        for fh in opened:
            fh.close()


def _zip_without(src: Path, drop_names: set[str], dest: Path) -> Path:
    import zipfile
    kept = 0
    with zipfile.ZipFile(src) as zf, zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as out:
        for name in zf.namelist():
            if Path(name).name in drop_names:
                continue
            out.writestr(name, zf.read(name))
            kept += 1
    if kept <= 0:
        dest.unlink(missing_ok=True)
        raise PipelineError(f"{src.name} 去掉无店铺列表后是空的")
    return dest


def import_zips(http: requests.Session, base: str, zip_paths: list[Path],
                 cfg: dict | None = None) -> dict:
    paths = [p for p in zip_paths if p and p.is_file()]
    if not paths:
        raise PipelineError("没有可导入的 ZIP")
    log("导入核验系统", " + ".join(p.name for p in paths))
    prev = _preview_zips(http, base, paths)
    drop_items: list[tuple[str, str]] = []
    drop_by_zip: dict[str, set[str]] = {}
    shop_map: dict[str, str] = {}
    for f in prev.get("files") or []:
        key = f"{f.get('archive') or ''}//{f.get('filename') or ''}"
        if f.get("auto_col"):
            shop_map[key] = str(f["auto_col"])
        if f.get("need_map") and not f.get("auto_col"):
            arch = str(f.get("archive") or "")
            fname = Path(str(f.get("filename") or "")).name
            drop_by_zip.setdefault(arch, set()).add(fname)
            reason = str(f.get("reason") or "没有识别到店铺列")
            drop_items.append((fname, reason))
            log("核验跳过无店铺列表", fname, reason)
    temps: list[Path] = []
    try:
        if drop_by_zip:
            new_paths = []
            for p in paths:
                names = drop_by_zip.get(p.name) or set()
                if not names:
                    new_paths.append(p)
                    continue
                tmp = p.with_name(p.stem + ".import.zip")
                _zip_without(p, names, tmp)
                temps.append(tmp)
                new_paths.append(tmp)
            paths = new_paths
            log("导入核验系统", " + ".join(p.name for p in paths), "(已去掉无店铺列表)")
            try:
                from send_feishu import notify_skipped
                notify_skipped(
                    cfg, "核验导入跳过",
                    drop_items,
                    extra="已从导入包去掉，其余表继续核验。",
                )
            except Exception as e:
                log("核验跳过飞书说明失败:", e)
            prev = _preview_zips(http, base, paths)
            shop_map = {}
            for f in prev.get("files") or []:
                if f.get("auto_col"):
                    shop_map[f"{f.get('archive') or ''}//{f.get('filename') or ''}"] = str(f["auto_col"])
        still = [f.get("filename") for f in (prev.get("files") or [])
                 if f.get("need_map") and not f.get("auto_col")]
        if still:
            raise PipelineError("有表格店铺识别失败，需要手动指定店铺列: " + "、".join(str(x) for x in still))
        uid = prev.get("upload_id")
        if not uid:
            raise PipelineError("导入预览没有 upload_id")
        st = prev.get("stats") or {}
        log(f"预览 {prev.get('total_rows')} 行 邮箱{st.get('emails')} 号码{st.get('phones')}")
        body: dict = {"upload_id": uid}
        if shop_map:
            body["shop_map"] = shop_map
        committed = ds_ok(http.post(f"{base}/api/import/commit", json=body, timeout=180))
        log(committed.get("message") or "导入完成")
        return committed
    finally:
        for p in temps:
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass


def found_data_zip(dest: Path) -> Path | None:
    hits = [p for p in dest.glob("*数据.zip") if p.is_file() and "汇总" not in p.name]
    if not hits:
        return None
    return max(hits, key=lambda p: p.stat().st_mtime)


_DETECT_ZIP_SKIP = ("汇总", "核验表", "三源合并", "详细地址")


def day_folder_detect_zips(dest: Path) -> list[Path]:
    """当天匹配目录根下的输入 ZIP 都进核验。汇总等核验写回的包不收。"""
    if not dest.is_dir():
        return []
    out: list[Path] = []
    for p in dest.glob("*.zip"):
        if not p.is_file():
            continue
        name = p.name
        if any(k in name for k in _DETECT_ZIP_SKIP):
            continue
        low = name.lower()
        if low.endswith(".import.zip") or low.endswith(".tmp.zip"):
            continue
        out.append(p)
    return sorted(out, key=lambda x: x.name.lower())


def match_folder_day(dest: Path) -> datetime:
    parts = dest.name.split(".")
    month, day = int(parts[0]), int(parts[-1])
    year = datetime.now().year
    return datetime(year, month, day)


def _found_xlsx(dest: Path) -> list[Path]:
    from collect_wechat import gather_found_xlsx
    return gather_found_xlsx(dest)


def found_files(dest: Path) -> list[Path]:
    out = list(_found_xlsx(dest))
    z = found_data_zip(dest)
    if z:
        out.append(z)
    return out


def found_new_before_noon(dest: Path, noon_hour: int) -> bool:
    """只认收集程序当天午间点前从微信/待收集收下的查出。手放到目录的不算。"""
    from collect_wechat import found_auto_copied_before_noon
    return found_auto_copied_before_noon(dest, noon_hour)


def refresh_found_zip(dest: Path) -> Path | None:
    from collect_wechat import rebuild_found_zip
    return rebuild_found_zip(dest, match_folder_day(dest))


def wait_found_gate(cfg: dict, dest: Path) -> None:
    """当天 12 点前有微信收下的新查出 → 立刻核验；手放到目录的等到查出截止。"""
    from collect_wechat import found_cutoff_hm, fmt_hm
    noon_h = int(cfg.get("found_noon_hour") or 12)
    six_h, six_m = found_cutoff_hm(cfg)
    last = ""
    last_beat = 0.0
    while True:
        now = datetime.now()
        six = now.replace(hour=six_h, minute=six_m, second=0, microsecond=0)
        has = bool(found_files(dest))
        early = found_new_before_noon(dest, noon_h)
        if now >= six:
            log(f"已到 {fmt_hm(six_h, six_m)}，开始核验", "有查出ZIP" if has else "无查出ZIP")
            return
        if early:
            log(f"当天 {noon_h:02d}:00 前有微信收下的查出，开始核验")
            return
        if now < now.replace(hour=noon_h, minute=0, second=0, microsecond=0):
            msg = f"{noon_h:02d}:00 前等待微信收下的查出（手放到目录的等到 {fmt_hm(six_h, six_m)}）"
        else:
            msg = f"当天 {noon_h:02d}:00 前无微信新查出，等到 {fmt_hm(six_h, six_m)} 再核验"
        if msg != last:
            log(msg)
            last = msg
            last_beat = time.time()
        elif time.time() - last_beat >= 300:
            mins = max(0, int((six - now).total_seconds() // 60))
            log(msg, f"还剩 {mins} 分钟")
            last_beat = time.time()
        time.sleep(20)


def ensure_smtp_proxy(http: requests.Session, base: str) -> None:
    info = ds_ok(http.get(f"{base}/api/proxy", timeout=20))
    if not info.get("enabled"):
        log("SMTP 代理未启用，按直连继续")
        return
    lst = info.get("list") or []
    current = info.get("current")
    order = []
    for p in lst:
        if p.get("id") == current:
            order.insert(0, p)
        else:
            order.append(p)
    if not order:
        log("代理池为空，按直连继续")
        return
    last_err = ""
    for p in order:
        pid = p.get("id")
        if pid != current:
            ds_ok(http.post(f"{base}/api/proxy/action", json={"action": "set_current", "id": pid}, timeout=15))
            log("切换备用代理", p.get("host"), p.get("port"))
        probed = _ds(http.post(f"{base}/api/proxy/action", json={"action": "test"}, timeout=40))
        if probed.get("success"):
            log("SMTP 出口代理连通", probed.get("message") or pid)
            return
        last_err = probed.get("message") or "不通"
        log("代理不通", p.get("host"), last_err)
    raise PipelineError(f"SMTP 出口代理全部不通: {last_err}")


def email_progress(http: requests.Session, base: str, engine: str) -> dict:
    return _ds(http.get(f"{base}/api/email/progress", params={"engine": engine}, timeout=20))


def wait_email(http: requests.Session, base: str, engine: str, label: str) -> dict:
    from stall import StallWatch
    watch = StallWatch("detect", label)
    last = ""
    while True:
        s = email_progress(http, base, engine)
        running = bool(s.get("running"))
        phase = s.get("phase") or ""
        msg = (f"{phase} {s.get('current', 0)}/{s.get('total', 0)} "
               f"有效{s.get('valid', 0)} 无效{s.get('invalid', 0)} {s.get('log') or ''}")
        watch.tick((running, phase, s.get("current"), s.get("valid"), s.get("invalid"), s.get("done")))
        if msg != last:
            log(label, msg)
            last = msg
        if not running and phase in ("done", "idle", "submitted"):
            return s
        if not running and s.get("done"):
            return s
        time.sleep(8)


def run_api_precheck(http: requests.Session, base: str, cfg: dict | None = None) -> dict:
    log("工作台：API 预检")
    from funds import wait_geeksend
    while True:
        pf = _ds(http.get(f"{base}/api/email/api-preview", timeout=30))
        log(f"拆分 {pf.get('total')}  缓存命中 {pf.get('cache_hits')}  待送检 {pf.get('fresh')}")
        fresh = int(pf.get("fresh") or 0)
        if wait_geeksend(cfg or {}, http, base, fresh):
            log("充值后重新预览 API 预检")
            continue
        break
    started = _ds(http.post(f"{base}/api/email/start-diff", json={"engine": "geeksend"}, timeout=60))
    if not started.get("success"):
        raise PipelineError(started.get("message") or "API 预检启动失败")
    log(started.get("message") or "已提交", "共", started.get("total"))
    wait_email(http, base, "geeksend", "邮箱预检")
    t0 = time.time()
    empty_rounds = 0
    last_pending = 0
    leftover_alerted = False
    from stall import StallWatch
    watch = StallWatch("detect", "邮箱预检取回")
    while True:
        s = email_progress(http, base, "geeksend")
        if s.get("done") or (not s.get("pending_addresses") and not s.get("pending_batches")
                             and s.get("phase") == "done"):
            log("API 预检完成", f"有效{s.get('valid', 0)} 无效{s.get('invalid', 0)} 未知{s.get('unknown', 0)}")
            return s
        if s.get("running"):
            watch.tick(("running", s.get("current"), s.get("valid"), s.get("invalid")))
            time.sleep(8)
            continue
        if s.get("pending_addresses") or s.get("pending_batches") or s.get("phase") == "submitted":
            pending_n = int(s.get("pending_addresses") or last_pending or 0)
            watch.tick(("pending", pending_n, empty_rounds))
            stale_small = empty_rounds >= 3 and 0 < pending_n < 5
            finalize = time.time() - t0 > 12 * 60 or stale_small
            if stale_small:
                log(f"预检连续 {empty_rounds} 次取回 0 且剩余 {pending_n}＜5，收尾记未知")
                if not leftover_alerted:
                    leftover_alerted = True
                    from send_feishu import alert_skip
                    alert_skip(
                        cfg or {}, "邮箱预检已跳过收尾",
                        f"连续 {empty_rounds} 次取回 0 且剩余 {pending_n} 条，记未知后继续后面检测",
                    )
            got = _ds(http.post(
                f"{base}/api/email/geeksend/collect",
                json={"finalize": finalize},
                timeout=120,
            ))
            still = int(got.get("pending") or 0)
            took = int(got.get("got") or 0)
            last_pending = still
            log("取回预检", got.get("message") or got)
            if took > 0:
                empty_rounds = 0
            elif still > 0:
                empty_rounds += 1
            if got.get("success") and (finalize or still <= 0):
                s = email_progress(http, base, "geeksend")
                log("API 预检完成", f"有效{s.get('valid', 0)} 无效{s.get('invalid', 0)} 未知{s.get('unknown', 0)}")
                return s
            time.sleep(20)
            continue
        return s


def start_smtp_send(http: requests.Session, base: str, cfg: dict | None = None) -> bool:
    """启动发信，不等待。已在跑则视为已启动。"""
    cur = email_progress(http, base, "smtp")
    if cur.get("running"):
        log("发信检测已在进行", f"{cur.get('current', 0)}/{cur.get('total', 0)}")
        return True
    ensure_smtp_proxy(http, base)
    prev = _ds(http.get(f"{base}/api/email/send-preview", timeout=30))
    log(f"发信预览 待发 {prev.get('to_send')}  API有效已跳过 {prev.get('excluded_valid')} "
        f"无效已跳过 {prev.get('excluded_invalid')}")
    started = _ds(http.post(f"{base}/api/email/start-diff", json={"engine": "smtp"}, timeout=60))
    if not started.get("success"):
        msg = started.get("message") or ""
        if "没有需要发信" in msg or "没有格式有效" in msg:
            from send_feishu import alert_skip
            alert_skip(cfg or {}, "发信检测已跳过", msg)
            return False
        raise PipelineError(msg or "发信检测启动失败")
    log("发信检测已启动", "共", started.get("total"), "（与即时消息等并行）")
    return True


def finish_smtp_send(http: requests.Session, base: str) -> dict | None:
    s = email_progress(http, base, "smtp")
    if s.get("running") or s.get("phase") in ("sending", "bounce", "recheck"):
        s = wait_email(http, base, "smtp", "发信检测")
    elif not s.get("total") and not s.get("done"):
        return None
    fails = s.get("bounce_fail_accounts") or []
    bounce_err = s.get("bounce_error") or ""
    if fails or (bounce_err and "跳过" not in bounce_err):
        log("SMTP 退信检测失败，开始退信复扫", bounce_err, fails)
        rec = _ds(http.post(f"{base}/api/monitor/recheck", json={"failed_only": True}, timeout=30))
        if rec.get("success"):
            log(rec.get("message") or "复扫已启动")
            wait_email(http, base, "smtp", "退信复扫")
            s = email_progress(http, base, "smtp")
        else:
            log("复扫未启动:", rec.get("message") or "")
    log("发信检测完成", f"有效{s.get('valid', 0)} 无效{s.get('invalid', 0)}")
    return s


def wait_engine(http: requests.Session, base: str, progress_path: str, label: str,
                drain_pending: bool = False) -> dict:
    from stall import StallWatch
    watch = StallWatch("detect", label)
    last = ""
    smtp_last = ""
    seen_run = False
    t0 = time.time()
    while True:
        s = _ds(http.get(f"{base}{progress_path}", timeout=20))
        running = bool(s.get("running"))
        pending = int(s.get("pending_items") or 0)
        if running:
            seen_run = True
        msg = (f"{s.get('progress') or s.get('phase') or ''} "
               f"{s.get('current', 0)}/{s.get('total', 0)} "
               f"待取回{pending} {s.get('log') or ''}")
        watch.tick((running, s.get("current"), s.get("total"), pending, s.get("done")))
        if msg != last:
            log(label, msg)
            last = msg
        smtp = email_progress(http, base, "smtp")
        if smtp.get("running") or smtp.get("phase") in ("sending", "bounce", "recheck"):
            sm = (f"{smtp.get('phase')} {smtp.get('current', 0)}/{smtp.get('total', 0)} "
                  f"有效{smtp.get('valid', 0)}")
            if sm != smtp_last:
                log("发信(并行)", sm)
                smtp_last = sm
        finished = bool(s.get("done") or (seen_run and not running))
        if finished and (not drain_pending or pending <= 0):
            return s
        if (not running) and (not seen_run) and time.time() - t0 > 90:
            return s
        time.sleep(8)


def wait_key_idle(http: requests.Session, base: str) -> None:
    """共用 CheckNumber 密钥：三项都不再跑、也没有已扣费未取回，才允许开下一项。"""
    paths = (
        ("/api/imessage/progress", "即时消息"),
        ("/api/whatsapp/progress", "会话号码"),
        ("/api/apple-email/progress", "账号邮箱"),
    )
    last = ""
    t0 = time.time()
    while True:
        busy = []
        for path, name in paths:
            s = _ds(http.get(f"{base}{path}", timeout=20))
            n = int(s.get("pending_items") or 0)
            if s.get("running") or n:
                busy.append(f"{name}{' 运行中' if s.get('running') else ''}{(' 待取回' + str(n)) if n else ''}")
        if not busy:
            return
        if time.time() - t0 > 3 * 3600:
            raise PipelineError("共用密钥等待超过 3 小时仍占用：" + "；".join(busy) + "。已停在该步，不往后跑")
        msg = "；".join(busy)
        if msg != last:
            log("共用密钥占用，等当前项取回并更新后再继续:", msg)
            last = msg
        time.sleep(10)


def drain_checknumber(http: requests.Session, base: str, progress_path: str,
                      collect_path: str, label: str) -> dict:
    from stall import StallWatch
    s = wait_engine(http, base, progress_path, label, drain_pending=True)
    watch = StallWatch("detect", label + "未取回")
    tries = 0
    while int(s.get("pending_items") or 0) > 0:
        pending = int(s.get("pending_items") or 0)
        watch.tick(("pending", pending))
        log(label, "检测进程已停，还有", pending, "条已扣费未取回，先取回再开下一项")
        got = _ds(http.post(f"{base}{collect_path}", json={}, timeout=60))
        log(label, got.get("message") or "已请求取回")
        s = wait_engine(http, base, progress_path, label + " 取回", drain_pending=True)
        tries += 1
        if tries >= 30 and int(s.get("pending_items") or 0) > 0:
            raise PipelineError(
                f"{label}已扣费未取回 {s.get('pending_items')} 条，取回 {tries} 次仍未清完。"
                "已停在该步，不往后跑"
            )
    return s


def start_split_wait(http: requests.Session, base: str, start_path: str, progress_path: str,
                     label: str, body: dict | None = None,
                     collect_path: str = "") -> dict:
    wait_key_idle(http, base)
    log(label)
    started = _ds(http.post(f"{base}{start_path}", json=body or {}, timeout=60))
    if not started.get("success"):
        raise PipelineError(started.get("message") or f"{label}启动失败")
    log(started.get("message") or "已启动", "共", started.get("total"))
    if collect_path:
        return drain_checknumber(http, base, progress_path, collect_path, label)
    return wait_engine(http, base, progress_path, label)


def _zip_download_name(resp: requests.Response, fallback: str) -> str:
    cd = resp.headers.get("Content-Disposition") or resp.headers.get("content-disposition") or ""
    m = re.search(r"filename\*=(?:UTF-8'')?([^;]+)", cd, re.I)
    if m:
        from urllib.parse import unquote
        return Path(unquote(m.group(1).strip().strip('"'))).name
    m = re.search(r'filename="?([^";]+)"?', cd, re.I)
    if m:
        return Path(m.group(1).strip()).name
    return fallback


def _dest_day_stamp(dest: Path) -> str:
    try:
        month, day = dest.name.split(".")
        year = datetime.now().year
        return f"{year}{int(month):02d}{int(day):02d}"
    except Exception:
        return datetime.now().strftime("%Y%m%d")


def export_summary_zips(http: requests.Session, base: str, dest: Path,
                        cfg: dict | None = None) -> list[Path]:
    """核验页「直接下载 ZIP」：精简口径，A=表名不含「店」，B=表名含「店」。"""
    dest.mkdir(parents=True, exist_ok=True)
    day = _dest_day_stamp(dest)
    out: list[Path] = []
    for group, tag, hint in (
        ("plain", "A", "表名不含店"),
        ("shop", "B", "表名含店"),
    ):
        name = f"{day}_汇总{tag}.zip"
        log("直接下载", name, hint, "精简口径")
        r = http.post(
            f"{base}/api/export/summary/zip",
            json={"variant": "clean", "filename": name, "name_group": group},
            timeout=180,
            headers={"Accept": "*/*"},
        )
        ctype = (r.headers.get("Content-Type") or "").lower()
        if r.status_code >= 400 or "application/json" in ctype:
            try:
                err = (r.json() or {}).get("message") or (r.json() or {}).get("error")
            except Exception:
                err = r.text[:200]
            if err and "没有符合表名规则" in str(err):
                from send_feishu import alert_skip
                alert_skip(cfg or {}, "汇总导出已跳过", f"{name}：{err}")
                continue
            raise PipelineError(err or f"汇总导出失败 HTTP {r.status_code}")
        fname = _zip_download_name(r, name)
        if "汇总" not in fname:
            fname = name
        path = dest / Path(fname).name
        path.write_bytes(r.content)
        log("汇总 ZIP 已下载", path, path.stat().st_size, "字节")
        out.append(path)
    if not out:
        raise PipelineError("汇总 A/B 都没有可导出的表")
    return out


def export_summary_zip(http: requests.Session, base: str, dest: Path) -> Path:
    paths = export_summary_zips(http, base, dest)
    return paths[0]


def run_detect(cfg: dict, zip_path: Path | None = None, tri_item: dict | None = None,
               skip_found_wait: bool = False) -> Path | None:
    matching_base = (cfg.get("matching_url") or "http://127.0.0.1:5000").rstrip("/")
    detect_base = (cfg.get("detect_url") or "http://127.0.0.1:8848").rstrip("/")
    mhttp = _session()
    dhttp = _session()
    dest = match_day_dir(cfg)

    if zip_path is None:
        existing = keep_one_tri_zip(dest)
        if existing:
            log("当天成功包已在，跳过下载", existing.name)
            zip_path = existing

    if zip_path is None and tri_item is None:
        tri_item = today_tri_item(mhttp, matching_base)
        if tri_item:
            log("使用当天三源", tri_item.get("zip_name") or tri_item.get("id"))
        else:
            log("没有当天三源，不使用历史包")

    if zip_path is None and tri_item is not None:
        day = (tri_item.get("day") or tri_item.get("date") or "")[:10]
        dest = match_day_dir(cfg, day.replace("-", "") or "")
        existing = keep_one_tri_zip(dest)
        if existing:
            log("当天成功包已在，跳过下载", existing.name)
            zip_path = existing
        else:
            zip_path = download_tri_zip(mhttp, matching_base, tri_item, dest)
    elif zip_path is not None:
        dest = zip_path.parent
    if zip_path:
        zip_path = filter_tri_zip(cfg, zip_path)

    if not skip_found_wait:
        wait_found_gate(cfg, dest)
    refresh_found_zip(dest)

    packs = day_folder_detect_zips(dest)
    if packs:
        log("核验导入当天目录 ZIP", " + ".join(p.name for p in packs))
    else:
        from send_feishu import alert_skip
        alert_skip(cfg, "核验已跳过", "当天目录没有可导入的 ZIP")
        return None

    wait_detect(dhttp, detect_base, cfg.get("detect_exe") or "")
    import_zips(dhttp, detect_base, packs, cfg=cfg)
    run_api_precheck(dhttp, detect_base, cfg)
    start_smtp_send(dhttp, detect_base, cfg)
    from funds import wait_checknumber
    wait_checknumber(cfg, dhttp, detect_base)
    start_split_wait(dhttp, detect_base, "/api/imessage/start-split",
                     "/api/imessage/progress", "即时消息检测",
                     collect_path="/api/imessage/collect")
    start_split_wait(dhttp, detect_base, "/api/whatsapp/start-split",
                     "/api/whatsapp/progress", "会话号码检测",
                     collect_path="/api/whatsapp/collect")
    start_split_wait(dhttp, detect_base, "/api/apple-email/start-split",
                     "/api/apple-email/progress", "账号邮箱检测",
                     collect_path="/api/apple-email/collect")
    finish_smtp_send(dhttp, detect_base)
    outs = export_summary_zips(dhttp, detect_base, dest, cfg=cfg)
    from run_pipeline import cfg_bool
    from send_feishu import notify_shop_zips, notify_summary_a_zip
    if cfg_bool(cfg, "feishu_push_after_detect", True):
        notify_summary_a_zip(cfg, outs)
        notify_shop_zips(cfg, outs)
        log("核验链路完成（精简包已落盘，已推飞书）")
    else:
        log("核验链路完成（精简包已落盘，已关飞书推送）")
    return outs[-1]


def main():
    ap = argparse.ArgumentParser(description="核验链路：导入三源合并并跑完全部检测")
    ap.add_argument("--config", default=str(DEFAULT_CFG))
    ap.add_argument("--zip", dest="zip_file", default="", help="已下载的三源合并 ZIP")
    ap.add_argument("--push-summary", action="store_true",
                    help="只导出核验汇总 A/B 并推飞书，不重跑检测")
    ap.add_argument("--now", action="store_true", help="不等查出窗口，立刻核验")
    args = ap.parse_args()
    cfg_path = Path(args.config)
    cfg = load_cfg(cfg_path) if load_cfg else json.loads(cfg_path.read_text(encoding="utf-8"))
    os.chdir(HERE)
    try:
        if args.push_summary:
            detect_base = (cfg.get("detect_url") or "http://127.0.0.1:8848").rstrip("/")
            dhttp = _session()
            wait_detect(dhttp, detect_base, cfg.get("detect_exe") or "")
            dest = match_day_dir(cfg)
            outs = export_summary_zips(dhttp, detect_base, dest, cfg=cfg)
            from run_pipeline import cfg_bool
            from send_feishu import notify_shop_zips, notify_summary_a_zip
            if cfg_bool(cfg, "feishu_push_after_detect", True):
                notify_summary_a_zip(cfg, outs)
                notify_shop_zips(cfg, outs)
            return
        zp = Path(args.zip_file) if args.zip_file else None
        from collect_wechat import write_pid, clear_pid
        write_pid(DETECT_PID)
        atexit.register(lambda: clear_pid(DETECT_PID, os.getpid()))
        run_detect(cfg, zip_path=zp, skip_found_wait=args.now)
    except PipelineError as e:
        log("失败:", e)
        try:
            from send_feishu import notify_alert
            notify_alert(cfg, "核验链路报错", str(e))
        except Exception:
            pass
        sys.exit(1)
    except Exception as e:
        log("失败:", e)
        try:
            from send_feishu import notify_alert
            notify_alert(cfg, "核验链路报错", str(e))
        except Exception:
            pass
        raise


if __name__ == "__main__":
    main()
