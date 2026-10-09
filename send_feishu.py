# -*- coding: utf-8 -*-
"""飞书应用机器人：上传 ZIP 到群，或按店把表格发到个人。"""
from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path

import requests

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

HERE = Path(__file__).resolve().parent
DEFAULT_CFG = HERE / "config.json"
OPEN = "https://open.feishu.cn/open-apis"
MAX_BYTES = 30 * 1024 * 1024

try:
    from run_pipeline import log, load_cfg
except ImportError:
    def log(*args):
        print(datetime.now().strftime("%Y-%m-%d %H:%M:%S"), *args)

    def load_cfg(path: Path) -> dict:
        return json.loads(path.read_text(encoding="utf-8"))


class FeishuError(RuntimeError):
    pass


def _http() -> requests.Session:
    s = requests.Session()
    s.trust_env = False
    return s


def feishu_cfg(cfg: dict) -> tuple[str, str, str]:
    app_id = str(cfg.get("feishu_app_id") or "").strip()
    secret = str(cfg.get("feishu_app_secret") or "").strip()
    chat_id = str(cfg.get("feishu_chat_id") or "").strip()
    return app_id, secret, chat_id


def configured(cfg: dict) -> bool:
    app_id, secret, _ = feishu_cfg(cfg)
    return bool(app_id and secret)


def tenant_token(http: requests.Session, app_id: str, secret: str) -> str:
    r = http.post(
        f"{OPEN}/auth/v3/tenant_access_token/internal",
        json={"app_id": app_id, "app_secret": secret},
        timeout=30,
    )
    try:
        data = r.json()
    except Exception:
        raise FeishuError(f"取 token 失败 HTTP {r.status_code}: {r.text[:200]}")
    if data.get("code") != 0 or not data.get("tenant_access_token"):
        raise FeishuError(f"取 token 失败: {data.get('msg') or data}")
    return str(data["tenant_access_token"])


def _api(http: requests.Session, token: str, method: str, url: str, **kw) -> dict:
    headers = kw.pop("headers", {})
    headers["Authorization"] = f"Bearer {token}"
    r = http.request(method, url, headers=headers, timeout=kw.pop("timeout", 60), **kw)
    try:
        data = r.json()
    except Exception:
        raise FeishuError(f"飞书 HTTP {r.status_code}: {r.text[:200]}")
    if data.get("code") != 0:
        msg = str(data.get("msg") or data)
        code = data.get("code")
        if code not in (None, 0) and str(code) not in msg:
            msg = f"{msg} ({code})"
        raise FeishuError(msg)
    return data


def list_chats(http: requests.Session, token: str) -> list[dict]:
    items: list[dict] = []
    page = ""
    while True:
        params = {"page_size": 50}
        if page:
            params["page_token"] = page
        data = _api(http, token, "GET", f"{OPEN}/im/v1/chats", params=params, timeout=30)
        chunk = (data.get("data") or {}).get("items") or []
        items.extend(chunk)
        more = (data.get("data") or {}).get("page_token") or ""
        if not more:
            break
        page = more
    return items


def send_text(http: requests.Session, token: str, receive_id: str, text: str,
              receive_id_type: str = "chat_id") -> None:
    _api(
        http, token, "POST", f"{OPEN}/im/v1/messages",
        params={"receive_id_type": receive_id_type},
        headers={"Content-Type": "application/json; charset=utf-8"},
        json={
            "receive_id": receive_id,
            "msg_type": "text",
            "content": json.dumps({"text": text}, ensure_ascii=False),
        },
        timeout=30,
    )


def upload_file(http: requests.Session, token: str, path: Path) -> str:
    if path.stat().st_size > MAX_BYTES:
        raise FeishuError(f"超过飞书 30MB 上限: {path.name} {path.stat().st_size} 字节")
    with path.open("rb") as fh:
        data = _api(
            http, token, "POST", f"{OPEN}/im/v1/files",
            data={"file_type": "stream", "file_name": path.name},
            files={"file": (path.name, fh, "application/octet-stream")},
            timeout=180,
        )
    key = (data.get("data") or {}).get("file_key")
    if not key:
        raise FeishuError(f"上传未返回 file_key: {data}")
    return str(key)


def send_file(http: requests.Session, token: str, receive_id: str, file_key: str,
              receive_id_type: str = "chat_id") -> None:
    _api(
        http, token, "POST", f"{OPEN}/im/v1/messages",
        params={"receive_id_type": receive_id_type},
        headers={"Content-Type": "application/json; charset=utf-8"},
        json={
            "receive_id": receive_id,
            "msg_type": "file",
            "content": json.dumps({"file_key": file_key}),
        },
        timeout=30,
    )


def merge_disk_cfg(cfg: dict | None) -> dict:
    data = dict(cfg or {})
    if DEFAULT_CFG.is_file():
        try:
            disk = json.loads(DEFAULT_CFG.read_text(encoding="utf-8"))
            if isinstance(disk, dict):
                data.update(disk)
        except Exception:
            pass
    return data


def names_of(cfg: dict, key: str) -> list[str]:
    raw = cfg.get(key)
    if isinstance(raw, str) and raw.strip():
        return [raw.strip()]
    if isinstance(raw, list):
        return [str(x).strip() for x in raw if str(x).strip()]
    return []


def copy_user_names(cfg: dict) -> list[str]:
    return names_of(cfg, "feishu_copy_to")


def alert_recipients(cfg: dict) -> list[str]:
    return names_of(cfg, "feishu_alert_to") or copy_user_names(cfg)


def alert_configured(cfg: dict) -> bool:
    return bool(str(cfg.get("feishu_alert_app_id") or "").strip()
                and str(cfg.get("feishu_alert_app_secret") or "").strip())


def _split_user_names(raw) -> list[str]:
    if isinstance(raw, list):
        return [str(x).strip() for x in raw if str(x).strip()]
    if isinstance(raw, str) and raw.strip():
        return [x.strip() for x in re.split(r"[,，;；]", raw) if x.strip()]
    return []


def shop_user_map(cfg: dict) -> dict[str, list[str]]:
    """店名 → 接收人列表。同一店可多人。兼容旧版「一店一人」字符串。"""
    raw = cfg.get("feishu_shop_users") or {}
    out: dict[str, list[str]] = {}
    if isinstance(raw, list):
        for row in raw:
            if not isinstance(row, dict):
                continue
            shop = str(row.get("shop") or "").strip()
            users = _split_user_names(row.get("user"))
            if not shop or not users:
                continue
            bucket = out.setdefault(shop, [])
            for u in users:
                if u not in bucket:
                    bucket.append(u)
        return out
    if not isinstance(raw, dict):
        return {}
    for k, v in raw.items():
        shop = str(k).strip()
        users = _split_user_names(v)
        if shop and users:
            out[shop] = users
    return out


def extract_shop(filename: str) -> str:
    stem = Path(filename).name
    if stem.lower().endswith(".xlsx"):
        stem = stem[:-5]
    head = stem.split("+", 1)[0]
    return re.sub(r"\d{8}$", "", head).strip()


def match_shop_users(cfg: dict, filename: str) -> list[tuple[str, str]]:
    """返回 [(店名键, 飞书姓名), ...]，一店可多行。"""
    shop = extract_shop(filename)
    if not shop:
        return []
    mapping = shop_user_map(cfg)
    aliases = [shop]
    if shop.endswith("店"):
        aliases.append(shop[:-1])
    else:
        aliases.append(shop + "店")
    for key in aliases:
        users = mapping.get(key) or []
        if users:
            return [(key, u) for u in users]
    return []


def match_shop_user(cfg: dict, filename: str) -> tuple[str, str] | None:
    hits = match_shop_users(cfg, filename)
    return hits[0] if hits else None


_DIR_CACHE: dict[str, tuple[dict[str, str], dict[str, str]]] = {}


def _placeholder_name(name: str) -> bool:
    return bool(re.fullmatch(r"用户\d+", str(name or "").strip()))


def _unavailable(err: object) -> bool:
    text = str(err).lower()
    return "no availability" in text or "230013" in text or "no user authority" in text


def _forget_directory(token: str | None = None) -> None:
    if token is None:
        _DIR_CACHE.clear()
        return
    suffix = token[-24:]
    for key in [k for k in _DIR_CACHE if k.endswith(suffix)]:
        _DIR_CACHE.pop(key, None)


def _iter_scope_ids(http: requests.Session, token: str) -> list[str]:
    uids: list[str] = []
    page = ""
    while True:
        params = {"user_id_type": "open_id", "page_size": 50}
        if page:
            params["page_token"] = page
        try:
            sc = _api(http, token, "GET", f"{OPEN}/contact/v3/scopes", params=params)
        except Exception:
            break
        data = sc.get("data") or {}
        uids.extend(str(x).strip() for x in (data.get("user_ids") or []) if str(x).strip())
        page = str(data.get("page_token") or "")
        if not page:
            break
    return uids


def _fetch_directory(http: requests.Session, token: str) -> tuple[dict[str, str], dict[str, str]]:
    """应用可用范围：姓名→open_id（重名不收录）、open_id→姓名。"""
    by_oid: dict[str, str] = {}
    name_to_oids: dict[str, list[str]] = {}
    for uid in _iter_scope_ids(http, token):
        name = ""
        oid = uid
        try:
            one = _api(http, token, "GET", f"{OPEN}/contact/v3/users/{uid}",
                       params={"user_id_type": "open_id"})
            user = (one.get("data") or {}).get("user") or {}
            oid = str(user.get("open_id") or uid).strip()
            name = str(user.get("name") or user.get("nickname") or "").strip()
        except Exception:
            pass
        if oid:
            by_oid[oid] = name
        if name and oid:
            name_to_oids.setdefault(name, []).append(oid)
    by_name: dict[str, str] = {}
    for name, oids in name_to_oids.items():
        uniq = list(dict.fromkeys(oids))
        if len(uniq) == 1:
            by_name[name] = uniq[0]
    return by_name, by_oid


def _directory(http: requests.Session, token: str, force: bool = False
               ) -> tuple[dict[str, str], dict[str, str]]:
    key = f"{id(http)}:{token[-24:]}"
    if force:
        _DIR_CACHE.pop(key, None)
    hit = _DIR_CACHE.get(key)
    if hit is None:
        hit = _fetch_directory(http, token)
        _DIR_CACHE[key] = hit
    return hit


def _contact_users_by_name(http: requests.Session, token: str) -> dict[str, str]:
    by_name, _by_oid = _directory(http, token)
    return dict(by_name)


def _live_oid_for_name(cfg: dict, feishu_name: str, by_name: dict[str, str]) -> str:
    for alias in lookup_name_aliases(cfg, feishu_name):
        oid = str(by_name.get(alias) or "").strip()
        if oid:
            return oid
    return ""


def _oid_name_ok(cfg: dict, want: str, live_name: str) -> bool:
    live = str(live_name or "").strip()
    if not live:
        return True
    if live == want or live in lookup_name_aliases(cfg, want):
        return True
    return _placeholder_name(live)


def lookup_name_aliases(cfg: dict, feishu_name: str) -> list[str]:
    names = [feishu_name]
    raw = cfg.get("feishu_name_aliases") or {}
    extra = raw.get(feishu_name) if isinstance(raw, dict) else None
    if isinstance(extra, str) and extra.strip():
        names.append(extra.strip())
    elif isinstance(extra, list):
        names.extend(str(x).strip() for x in extra if str(x).strip())
    if isinstance(raw, dict):
        for k, v in raw.items():
            vals = [v] if isinstance(v, str) else (v or [])
            if feishu_name in [str(x).strip() for x in vals]:
                names.append(str(k).strip())
    for n in _name_aliases(feishu_name):
        if n not in names:
            names.append(n)
    out: list[str] = []
    for n in names:
        if n and n not in out:
            out.append(n)
    return out


def resolve_open_id(http: requests.Session, token: str, cfg: dict, feishu_name: str,
                    cache_key: str = "feishu_open_ids", use_chat: bool = True,
                    skip_cache: bool = False) -> str:
    who = str(feishu_name or "").strip()
    if who.startswith("ou_") or who.startswith("on_"):
        return who
    by_name, by_oid = _directory(http, token)
    live = _live_oid_for_name(cfg, feishu_name, by_name)
    if live:
        save_open_id(cache_key, feishu_name, live, cfg)
        return live
    chat_id = str(cfg.get("feishu_chat_id") or "").strip()
    if use_chat and chat_id:
        try:
            page = ""
            want = set(lookup_name_aliases(cfg, feishu_name))
            while True:
                params = {"member_id_type": "open_id", "page_size": 100}
                if page:
                    params["page_token"] = page
                data = _api(http, token, "GET", f"{OPEN}/im/v1/chats/{chat_id}/members", params=params)
                for it in (data.get("data") or {}).get("items") or []:
                    if str(it.get("name") or "").strip() not in want:
                        continue
                    member_id = str(it.get("member_id") or "").strip()
                    if member_id and (not by_oid or member_id in by_oid):
                        save_open_id(cache_key, feishu_name, member_id, cfg)
                        return member_id
                page = (data.get("data") or {}).get("page_token") or ""
                if not page:
                    break
        except Exception:
            pass
    if not skip_cache:
        cache = cfg.get(cache_key) or {}
        for alias in lookup_name_aliases(cfg, feishu_name):
            cached = str(cache.get(alias) or "").strip()
            if not cached:
                continue
            if by_oid and cached not in by_oid:
                continue
            live_name = by_oid.get(cached, "")
            if live_name and not _oid_name_ok(cfg, feishu_name, live_name):
                continue
            return cached
    raise FeishuError(f"找不到飞书用户 {feishu_name}，对方需在企业通讯录且已加入应用可用范围")


def save_open_id(cache_key: str, name: str, oid: str, cfg: dict | None = None) -> None:
    if not name or not oid:
        return
    if cfg is not None:
        bucket = cfg.get(cache_key)
        if not isinstance(bucket, dict):
            bucket = {}
            cfg[cache_key] = bucket
        bucket[name] = oid
    if not DEFAULT_CFG.is_file():
        return
    try:
        data = json.loads(DEFAULT_CFG.read_text(encoding="utf-8"))
        cache = data.get(cache_key) or {}
        if not isinstance(cache, dict):
            cache = {}
        if cache.get(name) == oid:
            return
        cache[name] = oid
        data[cache_key] = cache
        DEFAULT_CFG.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except Exception:
        pass


def drop_open_id(cache_key: str, name: str, cfg: dict | None = None) -> None:
    if cfg is not None:
        bucket = cfg.get(cache_key)
        if isinstance(bucket, dict):
            bucket.pop(name, None)
    if not DEFAULT_CFG.is_file() or not name:
        return
    try:
        data = json.loads(DEFAULT_CFG.read_text(encoding="utf-8"))
        cache = data.get(cache_key) or {}
        if not isinstance(cache, dict) or name not in cache:
            return
        cache.pop(name, None)
        data[cache_key] = cache
        DEFAULT_CFG.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except Exception:
        pass


def _wanted_push_names(cfg: dict) -> list[str]:
    names: list[str] = []
    for users in shop_user_map(cfg).values():
        names.extend(users)
    names.extend(copy_user_names(cfg))
    for key in ("feishu_summary_a_to", "feishu_enhance_to"):
        names.extend(names_of(cfg, key))
    out: list[str] = []
    for name in names:
        who = str(name or "").strip()
        if who and who not in out and not who.startswith("ou_") and not who.startswith("on_"):
            out.append(who)
    return out


def sync_open_ids_from_scope(http: requests.Session, token: str, cfg: dict,
                             cache_key: str = "feishu_open_ids") -> None:
    """每次发送前用可用名单校正对照，丢掉失效/绑错的 open_id。"""
    by_name, by_oid = _directory(http, token)
    if not by_oid:
        log("飞书可用名单为空，跳过 open_id 校对")
        return
    cache = cfg.get(cache_key)
    if not isinstance(cache, dict):
        cache = {}
        cfg[cache_key] = cache
    for name in _wanted_push_names(cfg):
        live = _live_oid_for_name(cfg, name, by_name)
        old = str(cache.get(name) or "").strip()
        if live and live != old:
            if old:
                log("飞书 open_id 已按可用名单纠正", name)
            save_open_id(cache_key, name, live, cfg)
        elif old and old not in by_oid:
            log("飞书 open_id 已不在可用范围，已清除", name)
            drop_open_id(cache_key, name, cfg)
    for name, oid in list(cache.items()):
        oid = str(oid or "").strip()
        if oid and oid not in by_oid:
            log("飞书 open_id 已不在可用范围，已清除", name)
            drop_open_id(cache_key, name, cfg)


def send_to_person(http: requests.Session, token: str, cfg: dict, name: str,
                   oid_cache: dict[str, str], send_fn, cache_key: str = "feishu_open_ids") -> str:
    """按可用名单取 open_id 再发送；NO availability 时丢掉旧 ID 重查一次。"""
    oid = oid_cache.get(name) or resolve_open_id(http, token, cfg, name, cache_key=cache_key)
    oid_cache[name] = oid
    try:
        send_fn(oid)
        save_open_id(cache_key, name, oid, cfg)
        return oid
    except Exception as e:
        if not _unavailable(e):
            raise
        log("飞书 open_id 失效，按可用名单重查", name)
        drop_open_id(cache_key, name, cfg)
        oid_cache.pop(name, None)
        _forget_directory(token)
        new_oid = resolve_open_id(
            http, token, cfg, name, cache_key=cache_key, skip_cache=True,
        )
        if not new_oid or new_oid == oid:
            raise FeishuError(f"{name} 不在应用可用范围（{e}）") from e
        oid_cache[name] = new_oid
        send_fn(new_oid)
        save_open_id(cache_key, name, new_oid, cfg)
        return new_oid


def resolve_alert_open_id(http: requests.Session, token: str, cfg: dict, feishu_name: str) -> str:
    """告警应用的 open_id 不能和表格机器人混用。"""
    oid = resolve_open_id(
        http, token, cfg, feishu_name,
        cache_key="feishu_alert_open_ids", use_chat=False,
    )
    save_open_id("feishu_alert_open_ids", feishu_name, oid, cfg)
    return oid


def _name_aliases(name: str) -> list[str]:
    aliases = [name]
    # 改名前后都认
    if name == "DZ":
        aliases.append("用户703027")
    elif name == "用户703027":
        aliases.append("DZ")
    return aliases


def resolve_alert_receiver(http: requests.Session, alert_token: str, cfg: dict,
                           feishu_name: str) -> tuple[str, str]:
    """返回 (receive_id, receive_id_type)。优先告警可用名单，其次跨应用 union_id。"""
    try:
        oid = resolve_alert_open_id(http, alert_token, cfg, feishu_name)
        return oid, "open_id"
    except Exception:
        pass
    union_cache = cfg.get("feishu_union_ids") or {}
    push_cache = cfg.get("feishu_open_ids") or {}
    for alias in lookup_name_aliases(cfg, feishu_name):
        uid = str(union_cache.get(alias) or "").strip()
        if uid:
            return uid, "union_id"
    push_oid = ""
    for alias in lookup_name_aliases(cfg, feishu_name):
        push_oid = str(push_cache.get(alias) or "").strip()
        if push_oid:
            break
    if push_oid and configured(cfg):
        try:
            app_id, secret, _ = feishu_cfg(cfg)
            push_token = tenant_token(http, app_id, secret)
            one = _api(
                http, push_token, "GET", f"{OPEN}/contact/v3/users/{push_oid}",
                params={"user_id_type": "open_id"},
            )
            user = (one.get("data") or {}).get("user") or {}
            union = str(user.get("union_id") or "").strip()
            if union:
                save_open_id("feishu_union_ids", feishu_name, union, cfg)
                return union, "union_id"
        except Exception:
            pass
    for alias in lookup_name_aliases(cfg, feishu_name):
        if alias == feishu_name:
            continue
        try:
            oid = resolve_alert_open_id(http, alert_token, cfg, alias)
            save_open_id("feishu_alert_open_ids", feishu_name, oid, cfg)
            return oid, "open_id"
        except Exception:
            continue
    raise FeishuError(
        f"找不到飞书用户 {feishu_name}。"
        "告警应用需开通通讯录只读，或先用表格机器人缓存过该用户。"
    )


def _zip_xlsx_bytes(zip_path: Path) -> list[tuple[str, bytes]]:
    out: list[tuple[str, bytes]] = []
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            if info.is_dir() or info.filename.startswith("__MACOSX/"):
                continue
            name = Path(info.filename).name
            if name.startswith(".") or name.startswith("~"):
                continue
            if not name.lower().endswith(".xlsx"):
                continue
            out.append((name, zf.read(info)))
    return out


def notify_summary_a_zip(cfg: dict, zip_paths: list[Path]) -> None:
    """核验汇总 A 整包发给指定人，并按 feishu_copy_to 抄送；不进群。"""
    cfg = merge_disk_cfg(cfg)
    who = str(cfg.get("feishu_summary_a_to") or "").strip()
    if not who:
        return
    paths = [Path(p) for p in zip_paths if p and Path(p).is_file() and "汇总A" in Path(p).name]
    if not paths:
        alert_skip(cfg, "汇总A推送已跳过", f"没有汇总A ZIP，未发给 {who}")
        return
    for path in paths:
        notify_zip(
            cfg, path,
            text=f"核验汇总A：{path.name}",
            log_ok="飞书已发送汇总A",
            fail_log="飞书发送汇总A失败:",
            to=who,
        )


def _safe_zip_stem(name: str) -> str:
    text = re.sub(r'[\\/:*?"<>|\s]+', "_", (name or "").strip())
    return (text or "收件人")[:40]


def notify_shop_zips(cfg: dict, zip_paths: list[Path],
                     only_shops: list[str] | None = None) -> None:
    """拆开精简包，按接收人把多张表打成一个 ZIP 再私聊发送。不进群。"""
    cfg = merge_disk_cfg(cfg)
    if not configured(cfg):
        log("飞书未配置 App ID/Secret，跳过按店发送")
        return
    if not shop_user_map(cfg):
        log("未配置 feishu_shop_users，跳过按店发送")
        return
    paths = [Path(p) for p in zip_paths if p and Path(p).is_file()]
    if not paths:
        return
    allow: set[str] = set()
    for raw in only_shops or []:
        key = str(raw).strip()
        if not key:
            continue
        allow.add(key)
        allow.add(key[:-1] if key.endswith("店") else key + "店")

    # user -> {filename: (bytes, shop)}
    per_user: dict[str, dict[str, tuple[bytes, str]]] = {}
    skipped = 0
    skipped_names: list[str] = []
    send_fails: list[str] = []
    for zp in paths:
        for name, raw in _zip_xlsx_bytes(zp):
            shop_name = extract_shop(name)
            if allow and shop_name not in allow and (shop_name + "店") not in allow:
                continue
            hits = match_shop_users(cfg, name)
            if not hits:
                skipped += 1
                skipped_names.append(name)
                log("按店发送跳过（无对照）", name)
                continue
            for shop, user in hits:
                bucket = per_user.setdefault(user, {})
                if name not in bucket:
                    bucket[name] = (raw, shop)

    if skipped_names:
        shown = "、".join(skipped_names[:20])
        more = f" 等 {len(skipped_names)} 张" if len(skipped_names) > 20 else ""
        alert_skip(
            cfg, "按店推送已跳过（无对照）",
            shown + more,
            extra="这些表没有店名对照，未发给任何人；有对照的店仍会发。",
        )
    if not per_user:
        log("按店发送结束", "已发 0", f"无对照 {skipped}")
        return

    try:
        from run_pipeline import cfg_bool
        as_zip = cfg_bool(cfg, "feishu_shop_as_zip", True)
    except Exception:
        as_zip = True

    app_id, secret, _ = feishu_cfg(cfg)
    http = _http()
    token = tenant_token(http, app_id, secret)
    sync_open_ids_from_scope(http, token, cfg)
    oid_cache: dict[str, str] = {}
    sent = 0
    day = datetime.now().strftime("%Y%m%d")
    with tempfile.TemporaryDirectory(prefix="feishu_shop_") as tmp:
        tmp_dir = Path(tmp)
        for user, files in per_user.items():
            shops = sorted({shop for _, shop in files.values()})
            shop_txt = "、".join(shops)
            try:
                send_path = None
                copy_prefix = ""
                if as_zip:
                    zip_name = f"{_safe_zip_stem(user)}_{day}_{'+'.join(shops[:6])}核验表.zip"
                    if len(shops) > 6:
                        zip_name = f"{_safe_zip_stem(user)}_{day}_{len(shops)}店核验表.zip"
                    zip_path = tmp_dir / zip_name
                    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                        for fname, (raw, _shop) in sorted(files.items()):
                            zf.writestr(fname, raw)
                    if zip_path.stat().st_size > MAX_BYTES:
                        log("按人压缩包超过 30MB，跳过", user, zip_path.name, zip_path.stat().st_size)
                        send_fails.append(f"{user} 压缩包超过 30MB")
                        continue
                    text = f"核验表 ZIP（{shop_txt}）共 {len(files)} 张：{zip_path.name}"
                    send_path = zip_path
                    copy_prefix = f"【抄送】{user}｜{text}"

                    def _send_main(oid, _text=text, _path=zip_path):
                        send_text(http, token, oid, _text, receive_id_type="open_id")
                        key = upload_file(http, token, _path)
                        send_file(http, token, oid, key, receive_id_type="open_id")

                    send_to_person(http, token, cfg, user, oid_cache, _send_main)
                    sent += 1
                    log("按人已发送ZIP", user, f"{len(files)}张", shop_txt, zip_path.name)
                else:
                    items: list[tuple[str, str, Path]] = []
                    for fname, (raw, shop) in sorted(files.items()):
                        local = tmp_dir / fname
                        local.write_bytes(raw)
                        items.append((fname, shop, local))

                    def _send_main(oid, _items=items):
                        for fname, shop, local in _items:
                            send_text(
                                http, token, oid, f"{shop} 核验表：{fname}",
                                receive_id_type="open_id",
                            )
                            key = upload_file(http, token, local)
                            send_file(http, token, oid, key, receive_id_type="open_id")

                    send_to_person(http, token, cfg, user, oid_cache, _send_main)
                    for fname, shop, _local in items:
                        sent += 1
                        log("按店已发送", shop, "→", user, fname)
                oid = oid_cache.get(user) or ""
                for copy_name in copy_user_names(cfg):
                    try:
                        def _send_copy(
                            copy_oid, _oid=oid, _as_zip=as_zip, _send_path=send_path,
                            _prefix=copy_prefix, _files=files, _tmp=tmp_dir, _user=user,
                        ):
                            if copy_oid == _oid:
                                return
                            if _as_zip and _send_path is not None:
                                send_text(http, token, copy_oid, _prefix, receive_id_type="open_id")
                                copy_key = upload_file(http, token, _send_path)
                                send_file(http, token, copy_oid, copy_key, receive_id_type="open_id")
                                return
                            for fname, (raw, shop) in sorted(_files.items()):
                                local = _tmp / fname
                                if not local.is_file():
                                    local.write_bytes(raw)
                                send_text(
                                    http, token, copy_oid,
                                    f"【抄送】{shop} → {_user}：{fname}",
                                    receive_id_type="open_id",
                                )
                                copy_key = upload_file(http, token, local)
                                send_file(http, token, copy_oid, copy_key, receive_id_type="open_id")

                        send_to_person(http, token, cfg, copy_name, oid_cache, _send_copy)
                        if oid_cache.get(copy_name) == oid:
                            continue
                        if as_zip and send_path is not None:
                            log("按人已抄送ZIP", user, "→", copy_name, send_path.name)
                        else:
                            for fname, (_raw, shop) in sorted(files.items()):
                                log("按店已抄送", shop, "→", copy_name, fname)
                    except Exception as e:
                        log("按人抄送失败", user, copy_name, e)
            except Exception as e:
                log("按人发送失败", user, e)
                send_fails.append(f"{user}：{e}")
    log("按店发送结束", f"已发 {sent}" + (" 人ZIP" if as_zip else " 份表"), f"无对照 {skipped}")
    if send_fails:
        alert_skip(
            cfg, "按店推送部分失败",
            "；".join(send_fails[:8]),
            extra="失败的人未发出，其余已发的不受影响。",
        )


def resolve_chat(http: requests.Session, token: str, chat_id: str) -> str:
    if chat_id:
        return chat_id
    chats = list_chats(http, token)
    if len(chats) == 1:
        cid = chats[0].get("chat_id") or ""
        name = chats[0].get("name") or cid
        log("飞书未填群 ID，使用唯一群:", name, cid)
        return str(cid)
    if not chats:
        raise FeishuError("机器人还不在任何群里。在飞书群设置里添加这个应用机器人")
    names = "、".join(f"{c.get('name')}({c.get('chat_id')})" for c in chats[:8])
    raise FeishuError("有多个群，请把其中一个 chat_id 写入 config.json 的 feishu_chat_id。" + names)


def send_zip(cfg: dict, path: Path, text: str = "", log_ok: str = "飞书已发送",
             to: str = "") -> bool:
    """只发个人，禁止发群。"""
    if not configured(cfg):
        log("飞书未配置 App ID/Secret，跳过发送")
        return False
    path = Path(path)
    if not path.is_file():
        raise FeishuError(f"找不到文件: {path}")
    who = (to or "").strip()
    if not who:
        raise FeishuError("未指定接收人，已禁止发群")
    app_id, secret, _ = feishu_cfg(cfg)
    http = _http()
    token = tenant_token(http, app_id, secret)
    sync_open_ids_from_scope(http, token, cfg)
    if not text:
        text = f"店表汇总已完成：{path.name}"
    log("飞书上传", path.name, path.stat().st_size, "字节", "→", who)

    def _send(oid, _text=text, _path=path):
        send_text(http, token, oid, _text, receive_id_type="open_id")
        key = upload_file(http, token, _path)
        send_file(http, token, oid, key, receive_id_type="open_id")

    send_to_person(http, token, cfg, who, {}, _send)
    log(log_ok, who)
    return True


def send_text_message(cfg: dict, text: str, log_ok: str = "飞书已发送说明",
                      to: str = "") -> bool:
    """只发个人，禁止发群。报错请用 send_alert_text。"""
    who = (to or "").strip()
    if not who:
        log("未指定接收人，已禁止发群，跳过文字发送")
        return False
    if not configured(cfg):
        log("飞书未配置 App ID/Secret，跳过发送")
        return False
    app_id, secret, _ = feishu_cfg(cfg)
    http = _http()
    token = tenant_token(http, app_id, secret)
    sync_open_ids_from_scope(http, token, cfg)

    def _send(oid, _text=text):
        send_text(http, token, oid, _text, receive_id_type="open_id")

    send_to_person(http, token, cfg, who, {}, _send)
    log(log_ok, who)
    return True


def send_alert_text(cfg: dict, text: str, log_ok: str = "飞书已发送告警") -> bool:
    """报错只走告警机器人私聊，不进业务群。"""
    cfg = merge_disk_cfg(cfg)
    if not alert_configured(cfg):
        log("未配置告警机器人，报错不进群，只记日志")
        return False
    app_id = str(cfg.get("feishu_alert_app_id") or "").strip()
    secret = str(cfg.get("feishu_alert_app_secret") or "").strip()
    http = _http()
    token = tenant_token(http, app_id, secret)
    sent = False
    for name in alert_recipients(cfg):
        try:
            rid, rtype = resolve_alert_receiver(http, token, cfg, name)
            send_text(http, token, rid, text, receive_id_type=rtype)
            log(log_ok, name)
            sent = True
        except Exception as e:
            log("飞书告警发送失败", name, e)
    return sent


def send_alert_file(cfg: dict, path: Path, text: str, log_ok: str = "飞书已发送告警") -> bool:
    cfg = merge_disk_cfg(cfg)
    if not alert_configured(cfg):
        log("未配置告警机器人，报错不进群，只记日志")
        return False
    app_id = str(cfg.get("feishu_alert_app_id") or "").strip()
    secret = str(cfg.get("feishu_alert_app_secret") or "").strip()
    http = _http()
    token = tenant_token(http, app_id, secret)
    sent = False
    for name in alert_recipients(cfg):
        try:
            rid, rtype = resolve_alert_receiver(http, token, cfg, name)
            send_text(http, token, rid, text, receive_id_type=rtype)
            key = upload_file(http, token, path)
            send_file(http, token, rid, key, receive_id_type=rtype)
            log(log_ok, name)
            sent = True
        except Exception as e:
            log("飞书告警发送失败", name, e)
    return sent


def notify_zip(cfg: dict, path: Path | None, text: str = "",
               log_ok: str = "飞书已发送",
               fail_log: str = "飞书发送失败（本地 ZIP 已保存）:",
               to: str = "") -> None:
    if path is None:
        return
    who = (to or "").strip()
    if not who:
        log(fail_log, "未指定接收人，已禁止发群")
        return
    try:
        send_zip(cfg, path, text=text, log_ok=log_ok, to=who)
    except Exception as e:
        log(fail_log, e)
        notify_alert(
            cfg, "文件推送已跳过", str(e),
            filename=Path(path).name,
            extra="本地文件已留下，链路继续。",
        )
        return
    copy_text = f"【抄送】{text}" if text else f"【抄送】{Path(path).name}"
    for copy_name in copy_user_names(cfg):
        if copy_name == who:
            continue
        try:
            send_zip(cfg, path, text=copy_text, log_ok="飞书已抄送ZIP", to=copy_name)
        except Exception as e:
            log("飞书抄送失败", copy_name, e)


def notify_alert(cfg: dict | None, title: str, reason: str,
                 filename: str = "", extra: str = "",
                 path: Path | None = None) -> None:
    """报错只发告警机器人私聊，不进业务群。"""
    cfg = merge_disk_cfg(cfg)
    stamp = datetime.now().strftime("%m.%d %H:%M")
    lines = [f"{title}（{stamp}）"]
    if filename:
        lines.append(f"文件：{filename}")
    if reason:
        lines.append(f"原因：{reason}")
    if extra:
        lines.append(extra)
    text = "\n".join(lines)
    try:
        p = Path(path) if path else None
        if p and p.is_file():
            send_alert_file(cfg, p, text)
            return
        send_alert_text(cfg, text)
    except Exception as e:
        log("飞书告警发送失败:", e)


def alert_skip(cfg: dict | None, title: str, reason: str, extra: str = "") -> None:
    """已经跳过的项一律补飞书，不进群。"""
    log(title + ":", reason)
    notify_alert(cfg, title, reason, extra=extra)


def notify_skipped(cfg: dict | None, stage: str, items: list[tuple[str, str]],
                   extra: str = "") -> None:
    """多张跳过表合成一条，走告警机器人，不进群。"""
    if not items:
        return
    cfg = merge_disk_cfg(cfg)
    stamp = datetime.now().strftime("%m.%d %H:%M")
    lines = [f"{stage}已跳过（{stamp}）共 {len(items)} 张"]
    for name, reason in items:
        lines.append(f"· {name}")
        if reason:
            lines.append(f"  原因：{reason}")
    if extra:
        lines.append(extra)
    try:
        send_alert_text(cfg, "\n".join(lines), log_ok="飞书已发送跳过说明")
    except Exception as e:
        log("飞书跳过说明发送失败:", e)


def latest_summary_zip(cfg: dict) -> Path | None:
    root = Path(cfg.get("match_dest_root") or r"D:\桌面\地址匹配源文件\匹配数据")
    if not root.is_dir():
        return None
    zips = [p for p in root.rglob("*.zip") if p.is_file()]
    if not zips:
        return None
    prefer = [p for p in zips if "汇总" in p.name]
    pool = prefer or zips
    return max(pool, key=lambda p: p.stat().st_mtime)


def setup_hint() -> str:
    return (
        "在 config.json 填这三项后即可发送：\n"
        "  feishu_app_id      开放平台企业自建应用\n"
        "  feishu_app_secret  同上，凭证与基础信息\n"
        "  feishu_chat_id     群 ID，oc_ 开头；也可先留空，群里只有这一个机器人时会自动用\n"
        "开放平台 https://open.feishu.cn/app\n"
        "权限：im:resource、im:message:send_as_bot、im:chat:read\n"
        "应用能力打开机器人，发布后把机器人拉进「店表汇总」群"
    )


def write_chat_id(cfg_path: Path, chat_id: str) -> None:
    data = json.loads(cfg_path.read_text(encoding="utf-8"))
    data["feishu_chat_id"] = chat_id
    cfg_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main():
    ap = argparse.ArgumentParser(description="飞书发送汇总 ZIP")
    ap.add_argument("--config", default=str(DEFAULT_CFG))
    ap.add_argument("--list", action="store_true", help="列出机器人所在群")
    ap.add_argument("--save", action="store_true", help="只有一个群时写入 feishu_chat_id")
    ap.add_argument("--send", default="", help="发送指定文件")
    ap.add_argument("--latest", action="store_true", help="发送匹配数据里最新的汇总 ZIP")
    ap.add_argument("--shops", action="store_true", help="拆开指定或当天汇总 ZIP，按店发到个人")
    ap.add_argument("--to", default="", help="接收人飞书姓名（必填，禁止发群）")
    args = ap.parse_args()
    cfg_path = Path(args.config)
    cfg = load_cfg(cfg_path) if load_cfg else json.loads(cfg_path.read_text(encoding="utf-8"))
    if not configured(cfg):
        log("飞书凭证还没填")
        print(setup_hint())
        sys.exit(2)
    http = _http()
    app_id, secret, chat_id = feishu_cfg(cfg)
    token = tenant_token(http, app_id, secret)
    log("飞书 token 已拿到")
    if args.list or args.save:
        chats = list_chats(http, token)
        if not chats:
            log("机器人还不在任何群。飞书群设置 → 群机器人 → 添加这个应用")
            sys.exit(1)
        for c in chats:
            log(" ", c.get("name"), c.get("chat_id"))
        if args.save:
            if len(chats) != 1:
                log("群不止一个，请手动把 chat_id 写入 config.json")
                sys.exit(1)
            cid = chats[0].get("chat_id") or ""
            write_chat_id(cfg_path, cid)
            log("已写入 feishu_chat_id", cid)
        if not args.send and not args.latest:
            return
    path = Path(args.send) if args.send else None
    if args.latest:
        path = latest_summary_zip(cfg)
        if path is None:
            log("匹配数据里还没有 ZIP")
            sys.exit(1)
        log("将发送", path)
    if args.shops:
        shop_paths = [path] if path else []
        if not shop_paths:
            dest = Path(cfg.get("match_dest_root") or r"D:\桌面\地址匹配源文件\匹配数据")
            now = datetime.now()
            day_dir = dest / f"{now.month}月" / f"{now.month}.{now.day:02d}"
            shop_paths = sorted(day_dir.glob("*汇总A.zip")) + sorted(day_dir.glob("*汇总B.zip"))
            if not shop_paths:
                prev = dest / f"{now.month}月" / f"{now.month}.{now.day - 1:02d}"
                shop_paths = sorted(prev.glob("*汇总A.zip")) + sorted(prev.glob("*汇总B.zip"))
        if not shop_paths:
            log("没有可按店发送的汇总 ZIP")
            sys.exit(1)
        notify_shop_zips(cfg, shop_paths)
        return
    if path:
        if not str(args.to or "").strip():
            log("请加 --to 姓名，已禁止发群")
            sys.exit(1)
        send_zip(cfg, path, to=args.to)
        return
    if not args.list:
        print("用法: python send_feishu.py --list --save")
        print("      python send_feishu.py --latest --to 姓名")
        print("      python send_feishu.py --send 文件.zip --to 姓名")
        print("      python send_feishu.py --shops")


if __name__ == "__main__":
    main()
