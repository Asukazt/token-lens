#!/usr/bin/env python3
"""API balance MCP server.

Two balance sources:
  1. relay   - an OpenAI-compatible relay (New-API / one-api style). Reads the
               Codex model provider config automatically, so it works with the
               key already on this machine.
  2. aliyun  - Alibaba Cloud BSS OpenAPI (cash balance + cash coupons).

Standard library only: no pip install required.

MCP mode:   python server.py
CLI mode:   python server.py balance|aliyun|coupons|diagnose|selftest
"""

from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import hmac
import json
import math
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone

import pricing_catalog

SERVER_NAME = "api-balance"
SERVER_VERSION = "2.0.1"
PROTOCOL_VERSION = "2024-11-05"
BSS_VERSION = "2017-12-14"
BSS_ENDPOINT = "https://business.aliyuncs.com/"
VOLC_HOST = "billing.volcengineapi.com"
VOLC_REGION = "cn-north-1"
VOLC_SERVICE = "billing"
VOLC_VERSION = "2022-01-01"

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_PATH = os.path.join(HERE, ".relay_cache.json")
USAGE_CACHE_PATH = os.path.join(HERE, ".usage_cache.json")
BALANCE_HISTORY_PATH = os.path.join(HERE, ".balance_history.jsonl")
CODEX_CONFIG = os.path.join(os.path.expanduser("~"), ".codex", "config.toml")
SESSION_ROOT = os.path.join(os.path.expanduser("~"), ".codex", "sessions")
SESSION_INDEX = os.path.join(os.path.expanduser("~"), ".codex", "session_index.jsonl")
WIDGET_PORT = 49983
WIDGET_SCRIPT = os.path.join(HERE, "widget_v2.py")
WIDGET_STATE = os.path.join(HERE, ".widget_state.json")

# New-API / one-api quote 500000 quota units per unit of currency by default.
DEFAULT_QUOTA_PER_UNIT = 500000.0
DEFAULT_USD_RATE = 7.3

# Reference list prices, CNY per million tokens. Relays often add a group
# multiplier, so these values are only used when the user has not set a price.
REFERENCE_PRICES = {
    "deepseek-v4.1-flash-1017": {
        "input": 2.0,
        "cached": 0.04,
        "output": 8.0,
    },
    "deepseek-v4.1-flash": {
        "input": 2.0,
        "cached": 0.04,
        "output": 8.0,
    },
}

MODEL_NAMES = {
    "deepseek-v4.1-flash-1017": "DeepSeek V4.1 Flash 1017",
    "deepseek-v4.1-flash": "DeepSeek V4.1 Flash",
    "deepseek-v4-flash": "DeepSeek V4 Flash",
    "deepseek-v4-pro": "DeepSeek V4 Pro",
    "kimi-k3.1-pro": "Kimi K3.1 Pro",
    "kimi/kimi-k3.1-pro": "Kimi K3.1 Pro",
    "kimi/kimi-k3.1-pro-highspeed": "Kimi K3.1 Pro Highspeed",
    "glm-5.3": "GLM 5.3",
    "glm-5.3-prime": "GLM 5.3 Prime",
    "qwen3.8-max": "Qwen 3.8 Max",
    "qwen3.8-flash": "Qwen 3.8 Flash",
}

# MCP speaks UTF-8; Windows pipes default to the local code page.
for _stream in (sys.stdin, sys.stdout):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


def log(message: str) -> None:
    sys.stderr.write("[api-balance] %s\n" % message)
    sys.stderr.flush()


class BalanceError(Exception):
    def __init__(self, code: str, message: str, detail=None):
        super().__init__("%s: %s" % (code, message))
        self.code = code
        self.message = message
        self.detail = detail or {}

    def as_dict(self):
        data = {"code": self.code, "message": self.message}
        data.update(self.detail)
        return data


# --------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------


def _read_json(path):
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError) as exc:
        log("cannot read %s: %s" % (path, exc))
        return {}


def _save_config(config):
    path = os.path.join(HERE, "config.json")
    temp = path + ".tmp"
    with open(temp, "w", encoding="utf-8") as handle:
        json.dump(config, handle, ensure_ascii=False, indent=2)
    os.replace(temp, path)


def _codex_provider():
    """Pull base_url + token from Codex's model provider config."""
    try:
        with open(CODEX_CONFIG, "r", encoding="utf-8") as handle:
            text = handle.read()
    except OSError:
        return {}

    base = re.search(r'(?m)^\s*base_url\s*=\s*["\']([^"\']+)["\']', text)
    token = re.search(r'(?m)^\s*experimental_bearer_token\s*=\s*["\']([^"\']+)["\']', text)
    if not token:
        token = re.search(r'(?m)^\s*api_key\s*=\s*["\']([^"\']+)["\']', text)
    if not (base and token):
        return {}
    name = re.search(r'(?m)^\[model_providers\.(.+?)\]\s*$', text)
    return {
        "base_url": base.group(1),
        "api_key": token.group(1),
        "name": name.group(1) if name else "codex-provider",
    }


def relay_config():
    config = _read_json(os.path.join(HERE, "config.json"))
    relay = dict(config.get("relay") or {})

    base = os.environ.get("RELAY_BASE_URL") or relay.get("base_url")
    key = os.environ.get("RELAY_API_KEY") or relay.get("api_key")
    name = os.environ.get("RELAY_NAME") or relay.get("name")
    endpoint = os.environ.get("RELAY_ENDPOINT") or relay.get("endpoint")

    # Codex 换了 provider 就跟随实际在用的地址和 token；旧中转站只留作配置备份。
    provider = _codex_provider()
    provider_base = provider.get("base_url") or ""
    if base and provider_base:
        if _url_host(base) != _url_host(provider_base):
            base = provider_base
            key = provider.get("api_key") or key
            name = provider.get("name") or name
        else:
            key = key or provider.get("api_key")

    # inherit=False means the user cleared the relay on purpose; never fall back.
    if relay.get("inherit", True) and not (base and key):
        base = base or provider_base
        key = key or provider.get("api_key")
        name = name or provider.get("name")
    if not base or not key:
        return {}

    root = base.rstrip("/")
    if root.endswith("/v1"):
        root = root[: -len("/v1")]
    parsed = urllib.parse.urlsplit(root)
    origin = "%s://%s" % (parsed.scheme, parsed.netloc)
    return {
        "base_url": base,
        "root": root,
        "origin": origin,
        "api_key": key,
        "name": name or "relay",
        "endpoint": endpoint,
    }


def aliyun_config():
    access_key_id = os.environ.get("ALIYUN_ACCESS_KEY_ID") or os.environ.get(
        "ALIBABA_CLOUD_ACCESS_KEY_ID"
    )
    access_key_secret = os.environ.get("ALIYUN_ACCESS_KEY_SECRET") or os.environ.get(
        "ALIBABA_CLOUD_ACCESS_KEY_SECRET"
    )
    if not (access_key_id and access_key_secret):
        stored = _read_json(os.path.join(HERE, "config.json")).get("aliyun") or {}
        access_key_id = access_key_id or stored.get("access_key_id")
        access_key_secret = access_key_secret or stored.get("access_key_secret")
    if not (access_key_id and access_key_secret):
        return {}
    return {"access_key_id": access_key_id, "access_key_secret": access_key_secret}


def volcengine_config():
    access_key_id = os.environ.get("VOLC_ACCESSKEY") or os.environ.get("VOLCENGINE_ACCESS_KEY_ID")
    access_key_secret = os.environ.get("VOLC_SECRETKEY") or os.environ.get(
        "VOLCENGINE_ACCESS_KEY_SECRET"
    )
    if not (access_key_id and access_key_secret):
        stored = _read_json(os.path.join(HERE, "config.json")).get("volcengine") or {}
        access_key_id = access_key_id or stored.get("access_key_id")
        access_key_secret = access_key_secret or stored.get("access_key_secret")
    if not (access_key_id and access_key_secret):
        return {}
    return {"access_key_id": access_key_id, "access_key_secret": access_key_secret}


# --------------------------------------------------------------------------
# HTTP helper
# --------------------------------------------------------------------------


def http_get(url, headers=None, timeout=15):
    request = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", "replace")
            return response.status, body
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        raise BalanceError("NetworkError", "无法连接 %s：%s" % (url, reason)) from exc
    except TimeoutError as exc:
        raise BalanceError("Timeout", "请求超时：%s" % url) from exc


def http_json(url, headers=None, timeout=15):
    status, body = http_get(url, headers, timeout)
    try:
        payload = json.loads(body) if body.strip() else {}
    except ValueError:
        payload = None
    return status, payload, body


def http_post_json(url, headers=None, body=b"", timeout=15):
    request = urllib.request.Request(url, data=body, headers=headers or {}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", "replace")
            status = response.status
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        status = exc.code
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        raise BalanceError("NetworkError", "无法连接 %s：%s" % (url, reason)) from exc
    except TimeoutError as exc:
        raise BalanceError("Timeout", "请求超时：%s" % url) from exc
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except ValueError:
        payload = None
    return status, payload, raw


# --------------------------------------------------------------------------
# relay source
# --------------------------------------------------------------------------


def _relay_headers(cfg):
    return {
        "Authorization": "Bearer " + cfg["api_key"],
        "Accept": "application/json",
        "User-Agent": "%s/%s" % (SERVER_NAME, SERVER_VERSION),
    }


def _candidates(cfg):
    """(label, url, needs_auth) in the order we try them."""
    root, base = cfg["root"], cfg["base_url"].rstrip("/")
    origin = cfg.get("origin") or root
    candidates = [
        ("usage_v1", base + "/usage", True),
        ("newapi_self", origin + "/api/user/self", True),
        ("newapi_status", origin + "/api/status", False),
        ("openai_subscription", origin + "/dashboard/billing/subscription", True),
        ("openai_credit_grants", origin + "/v1/dashboard/billing/credit_grants", True),
        ("newapi_self_scoped", root + "/api/user/self", True),
        ("openai_subscription_scoped", root + "/dashboard/billing/subscription", True),
        (
            "openai_subscription_compat",
            base + "/dashboard/billing/subscription",
            True,
        ),
    ]
    cached = _read_cache().get("endpoint")
    if cached:
        candidates.sort(key=lambda item: 0 if item[0] == cached else 1)
    return candidates


def _num(value, default=None):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def parse_newapi(user_payload, status_payload):
    data = (user_payload or {}).get("data")
    if not isinstance(data, dict) or "quota" not in data:
        raise BalanceError("UnsupportedResponse", "不是 New-API 的 /api/user/self 格式")

    status_data = (status_payload or {}).get("data") or {}
    per_unit = _num(status_data.get("quota_per_unit"), DEFAULT_QUOTA_PER_UNIT) or DEFAULT_QUOTA_PER_UNIT
    rate = _num(status_data.get("usd_exchange_rate"), DEFAULT_USD_RATE) or DEFAULT_USD_RATE

    remaining = _num(data.get("quota"), 0.0) / per_unit
    used = _num(data.get("used_quota"), 0.0) / per_unit
    granted = remaining + used

    return {
        "source": "relay",
        "format": "new-api",
        "currency": "USD",
        "granted": round(granted, 4),
        "used": round(used, 4),
        "remaining": round(remaining, 4),
        "cny": round(remaining * rate, 2),
        "usd_rate": rate,
        "request_count": data.get("request_count"),
    }


def parse_openai_billing(subscription, usage):
    sub = subscription or {}
    granted = _num(sub.get("hard_limit_usd")) or _num(sub.get("system_hard_limit_usd"))
    if granted is None:
        raise BalanceError("UnsupportedResponse", "不是 OpenAI 计费格式")
    used_cents = _num((usage or {}).get("total_usage"), 0.0) or 0.0
    used = used_cents / 100.0
    remaining = granted - used
    return {
        "source": "relay",
        "format": "openai-billing",
        "currency": "USD",
        "granted": round(granted, 4),
        "used": round(used, 4),
        "remaining": round(remaining, 4),
        "cny": None,
        "usd_rate": None,
    }


def parse_credit_grants(payload):
    data = payload or {}
    granted = _num(data.get("total_granted"))
    available = _num(data.get("total_available"))
    if granted is None or available is None:
        raise BalanceError("UnsupportedResponse", "不是 credit_grants 格式")
    used = _num(data.get("total_used"), max(granted - available, 0.0))
    return {
        "source": "relay",
        "format": "credit-grants",
        "currency": "USD",
        "granted": round(granted, 4),
        "used": round(used, 4),
        "remaining": round(available, 4),
        "cny": None,
        "usd_rate": None,
    }


def parse_usage_v1(payload):
    data = payload or {}
    remaining = _num(data.get("remaining"), _num(data.get("balance")))
    if remaining is None:
        raise BalanceError("UnsupportedResponse", "不是 /v1/usage 余额格式")
    unit = str(data.get("unit") or "USD").upper()
    currency = "CNY" if unit == "CNY" else "USD"
    usage = data.get("usage") or {}
    today = usage.get("today") or {}
    return {
        "source": "relay",
        "format": "usage-v1",
        "currency": currency,
        "granted": None,
        "used": None,
        "remaining": round(remaining, 6),
        "cny": round(remaining, 6) if currency == "CNY" else None,
        "usd_rate": None,
        "today_cost": _num(today.get("actual_cost"), _num(today.get("cost"))),
        "today_tokens": today.get("total_tokens"),
        "today_requests": today.get("requests"),
        "plan": data.get("planName") or "",
        "valid": data.get("is_active", data.get("isValid", True)),
    }


def parse_deepseek_balance(payload):
    infos = (payload or {}).get("balance_infos")
    if not isinstance(infos, list) or not infos:
        raise BalanceError("UnsupportedResponse", "不是 DeepSeek 余额格式")
    info = next((item for item in infos if item.get("currency") == "CNY"), infos[0])
    remaining = _num(info.get("total_balance"))
    if remaining is None:
        raise BalanceError("UnsupportedResponse", "DeepSeek 余额字段缺失")
    return {
        "source": "official",
        "format": "deepseek",
        "provider": "DeepSeek 官方",
        "currency": info.get("currency") or "CNY",
        "remaining": round(remaining, 4),
        "granted": round(remaining, 4),
        "used": None,
        "gift": round(_num(info.get("granted_balance"), 0.0) or 0.0, 4),
        "topped_up": round(_num(info.get("topped_up_balance"), 0.0) or 0.0, 4),
        "available": bool((payload or {}).get("is_available", True)),
    }


def deepseek_balance():
    cfg = relay_config()
    if not cfg:
        raise BalanceError("NoKey", "未找到 DeepSeek API Key")
    status, payload, body = http_json(
        "https://api.deepseek.com/user/balance", _relay_headers(cfg)
    )
    if status != 200 or not isinstance(payload, dict):
        raise BalanceError(
            "DeepSeekError",
            "DeepSeek 余额接口返回 %s" % status,
            {"snippet": (body or "")[:160]},
        )
    return parse_deepseek_balance(payload)


def _volc_quote(value):
    return urllib.parse.quote(str(value), safe="-_.~")


def volc_signed_request(action, config, now=None):
    now = now or datetime.now(timezone.utc)
    x_date = now.strftime("%Y%m%dT%H%M%SZ")
    date = x_date[:8]
    query_text = "&".join(
        "%s=%s" % (_volc_quote(key), _volc_quote(value))
        for key, value in sorted({"Action": action, "Version": VOLC_VERSION}.items())
    )
    body_hash = hashlib.sha256(b"").hexdigest()
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded",
        "Host": VOLC_HOST,
        "User-Agent": "%s/%s" % (SERVER_NAME, SERVER_VERSION),
        "X-Content-Sha256": body_hash,
        "X-Date": x_date,
    }
    signed_headers = "content-type;host;x-content-sha256;x-date"
    canonical_headers = "".join(
        "%s:%s\n" % (name.lower(), headers[name])
        for name in ("Content-Type", "Host", "X-Content-Sha256", "X-Date")
    )
    canonical_request = "\n".join(
        ["POST", "/", query_text, canonical_headers, signed_headers, body_hash]
    )
    scope = "%s/%s/%s/request" % (date, VOLC_REGION, VOLC_SERVICE)
    string_to_sign = "\n".join(
        [
            "HMAC-SHA256",
            x_date,
            scope,
            hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
        ]
    )
    key = config["access_key_secret"].encode("utf-8")
    for part in (date, VOLC_REGION, VOLC_SERVICE, "request"):
        key = hmac.new(key, part.encode("utf-8"), hashlib.sha256).digest()
    signature = hmac.new(key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    headers["Authorization"] = (
        "HMAC-SHA256 Credential=%s/%s, SignedHeaders=%s, Signature=%s"
        % (config["access_key_id"], scope, signed_headers, signature)
    )
    return "https://%s/?%s" % (VOLC_HOST, query_text), headers


def parse_volc_balance(payload):
    result = (payload or {}).get("Result")
    if not isinstance(result, dict):
        raise BalanceError("UnsupportedResponse", "不是火山方舟余额格式")
    available = _num(result.get("AvailableBalance"))
    if available is None:
        raise BalanceError("UnsupportedResponse", "火山方舟余额字段缺失")
    return {
        "source": "official",
        "format": "volcengine",
        "provider": "火山方舟",
        "currency": "CNY",
        "remaining": round(available, 4),
        "granted": round(available, 4),
        "used": None,
        "cash": round(_num(result.get("CashBalance"), 0.0) or 0.0, 4),
        "credit_limit": round(_num(result.get("CreditLimit"), 0.0) or 0.0, 4),
        "freeze": round(_num(result.get("FreezeAmount"), 0.0) or 0.0, 4),
        "arrears": round(_num(result.get("ArrearsBalance"), 0.0) or 0.0, 4),
    }


def volcengine_balance():
    config = volcengine_config()
    if not config:
        raise BalanceError("NoKey", "未配置火山 AccessKey / SecretKey")
    url, headers = volc_signed_request("QueryBalanceAcct", config)
    status, payload, body = http_post_json(url, headers)
    if status != 200 or not isinstance(payload, dict):
        error = ((payload or {}).get("ResponseMetadata") or {}).get("Error") or {}
        raise BalanceError(
            "VolcengineError",
            error.get("Message") or "火山方舟余额接口返回 %s" % status,
            {"snippet": (body or "")[:160]},
        )
    return parse_volc_balance(payload)


def probe_relay(cfg, verbose=False):
    """Try known billing endpoints and return (result, diagnostics)."""
    diagnostics = []
    status_payload = None
    endpoint = cfg.get("endpoint")

    for label, url, needs_auth in _candidates(cfg):
        if endpoint and label != endpoint:
            continue
        headers = _relay_headers(cfg) if needs_auth else {"Accept": "application/json"}
        try:
            status, payload, body = http_json(url, headers)
        except BalanceError as exc:
            diagnostics.append({"endpoint": label, "url": url, "error": str(exc)})
            continue

        diagnostics.append(
            {
                "endpoint": label,
                "url": url,
                "status": status,
                "snippet": (body or "")[:160],
            }
        )

        if status in (401, 403):
            if payload and (
                "INVALID_TOKEN" in str(payload)
                or "Invalid token" in str(payload)
            ):
                diagnostics[-1]["auth_required"] = True
            continue
        if payload is None:
            continue

        try:
            if label in ("newapi_self", "newapi_self_scoped"):
                if status_payload is None:
                    _, status_payload, _ = http_json(
                        cfg.get("origin", cfg["root"]) + "/api/status",
                        {"Accept": "application/json"},
                    )
                result = parse_newapi(payload, status_payload)
            elif label.startswith("openai_subscription"):
                base_part = url.split("/dashboard/billing/subscription")[0]
                usage_url = base_part + "/dashboard/billing/usage"
                now = datetime.now()
                usage_url += "?start_date=%s&end_date=%s" % (
                    now.replace(day=1).strftime("%Y-%m-%d"),
                    now.strftime("%Y-%m-%d"),
                )
                _, usage_payload, _ = http_json(usage_url, _relay_headers(cfg))
                result = parse_openai_billing(payload, usage_payload)
            elif label == "openai_credit_grants":
                result = parse_credit_grants(payload)
            elif label == "usage_v1":
                result = parse_usage_v1(payload)
            else:
                continue

            result["endpoint"] = label
            result["provider"] = cfg.get("name")
            _write_cache({"endpoint": label, "at": int(time.time())})
            return result, diagnostics
        except BalanceError as exc:
            diagnostics.append({"endpoint": label, "parse_error": str(exc)})
            continue
        except Exception as exc:  # noqa: BLE001
            diagnostics.append({"endpoint": label, "parse_error": repr(exc)})
            continue

    raise BalanceError(
        "NoBillingEndpoint",
        "中转站没有返回账户余额：当前 key 可能只能调用模型，"
        "或站点要求登录令牌才能查询余额。",
        {"diagnostics": diagnostics},
    )


def _write_cache(data):
    try:
        with open(CACHE_PATH, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
    except OSError:
        pass


def _read_cache():
    data = _read_json(CACHE_PATH)
    return data if isinstance(data, dict) else {}


# --------------------------------------------------------------------------
# aliyun BSS source
# --------------------------------------------------------------------------


def percent_encode(value) -> str:
    return urllib.parse.quote(str(value), safe="").replace("*", "%2A").replace("%7E", "~")


def rpc_sign(params, secret: str) -> str:
    items = sorted(
        ((key, "true" if value is True else "false" if value is False else str(value))
         for key, value in params.items() if value is not None),
        key=lambda item: item[0],
    )
    canonical = "&".join(
        "%s=%s" % (percent_encode(key), percent_encode(value)) for key, value in items
    )
    string_to_sign = "GET&%2F&" + percent_encode(canonical)
    digest = hmac.new(
        (secret + "&").encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha1
    ).digest()
    return base64.b64encode(digest).decode("ascii")


def bss_call(action, params=None):
    cfg = aliyun_config()
    if not cfg:
        raise BalanceError(
            "AliyunNotConfigured",
            "未配置阿里云 AccessKey（config.json 的 aliyun 段，或环境变量 "
            "ALIYUN_ACCESS_KEY_ID / ALIYUN_ACCESS_KEY_SECRET）。",
        )

    query = {
        "Action": action,
        "Version": BSS_VERSION,
        "Format": "JSON",
        "AccessKeyId": cfg["access_key_id"],
        "SignatureMethod": "HMAC-SHA1",
        "SignatureVersion": "1.0",
        "SignatureNonce": uuid.uuid4().hex,
        "Timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    for key, value in (params or {}).items():
        if value is not None:
            query[key] = value
    query["Signature"] = rpc_sign(query, cfg["access_key_secret"])

    url = BSS_ENDPOINT + "?" + urllib.parse.urlencode(
        query, quote_via=urllib.parse.quote, safe=""
    )
    status, payload, body = http_json(url, {"Accept": "application/json"})

    if payload is None:
        raise BalanceError(
            "BadResponse", "BSS 返回非 JSON（HTTP %s）" % status, {"body": body[:300]}
        )
    code = payload.get("Code")
    if payload.get("Success") is False or code not in (None, "Success", "200"):
        hint = ""
        if code in ("Forbidden.RAM", "NoPermission", "Forbidden"):
            hint = " 请给 RAM 用户授予 AliyunBSSReadOnlyAccess。"
        raise BalanceError(
            code or "UnknownError",
            (payload.get("Message") or "BSS 调用失败") + hint,
            {"request_id": payload.get("RequestId")},
        )
    return payload


def aliyun_balance():
    last = None
    for action in ("QueryAccountBalance", "DescribeAccountBalance"):
        try:
            payload = bss_call(action)
            break
        except BalanceError as exc:
            if exc.code in ("AliyunNotConfigured", "NetworkError", "Timeout"):
                raise
            last = exc
    else:
        raise last

    data = payload.get("Data") or {}
    return {
        "source": "aliyun",
        "currency": data.get("Currency") or "CNY",
        "cash": _num(data.get("AvailableCashAmount"), 0.0),
        "available": _num(data.get("AvailableAmount"), 0.0),
        "credit": _num(data.get("CreditAmount"), 0.0),
        "request_id": payload.get("RequestId"),
    }


LAST_COUPON_QUERY = {}


def _coupon_items(data):
    """Pull the coupon array out of the several shapes BSS returns."""
    box = data.get("CashCoupons")
    if box is None:
        box = data.get("CashCoupon")
    if isinstance(box, list):
        return box
    if isinstance(box, dict):
        inner = box.get("CashCoupon")
        if inner is None:
            inner = box.get("CashCoupons")
        if inner is None:
            inner = box if box.get("CashCouponId") else []
        if isinstance(inner, dict):
            return [inner]
        return list(inner or [])
    return []


def _query_cash_coupons(status=None):
    coupons, page, total = [], 1, None
    now = datetime.now(timezone.utc)
    window = {
        "ExpiryTimeStart": (now - timedelta(days=1825)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ExpiryTimeEnd": (now + timedelta(days=1825)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    while page <= 10:
        params = {"PageNo": page, "PageSize": 50}
        if status:
            params["Status"] = status
        try:
            data = bss_call("QueryCashCoupons", params).get("Data") or {}
            LAST_COUPON_QUERY.update({
                "status": status,
                "top_keys": sorted(data.keys()),
                "total": data.get("TotalCount"),
                "request_id": data.get("RequestId"),
            })
        except BalanceError as exc:
            if "ExpiryTime" not in (exc.message or ""):
                raise
            params.update(window)
            data = bss_call("QueryCashCoupons", params).get("Data") or {}
        if not (data.get("CashCoupons") or {}):
            # Some accounts only answer when the expiry window is explicit.
            widened = dict(params)
            widened.update(window)
            try:
                data = bss_call("QueryCashCoupons", widened).get("Data") or {}
            except BalanceError:
                pass

        items = _coupon_items(data)
        LAST_COUPON_QUERY["items"] = len(items)
        if not items:
            break
        for item in items:
            coupons.append(
                {
                    "id": item.get("CashCouponId"),
                    "name": (item.get("Description") or item.get("CashCouponName")
                             or item.get("CashCouponNo")),
                    "amount": _num(item.get("NominalValue") or item.get("Amount"), 0.0),
                    "balance": _num(item.get("Balance"), 0.0),
                    "status": item.get("Status"),
                    "effective": str(item.get("EffectiveTime") or "")[:19],
                    "expiry": str(item.get("ExpiryTime") or "")[:19],
                    "applicable": item.get("ApplicableProducts") or item.get("ApplicableItems"),
                }
            )
        total = total or int(_num(data.get("TotalCount"), 0) or 0)
        if not total or page * 50 >= total:
            break
        page += 1
    return coupons


def aliyun_coupons(status="Available"):
    """Cash coupons on the account; some accounts only list them without a status filter."""
    coupons = _query_cash_coupons(status)
    if coupons or not status:
        return coupons
    try:
        return _query_cash_coupons(None)
    except BalanceError:
        return coupons


# --------------------------------------------------------------------------
# reporting
# --------------------------------------------------------------------------


def _widget_command(command, timeout=3.0):
    with socket.create_connection(("127.0.0.1", WIDGET_PORT), timeout=timeout) as sock:
        sock.sendall(command.encode("utf-8"))
        return sock.recv(4096).decode("utf-8", "replace")


def widget_running():
    try:
        return _widget_command("ping", 1.5).strip().lower() == "pong"
    except OSError:
        return False


def widget_state():
    running = widget_running()
    state = _read_json(WIDGET_STATE) if os.path.exists(WIDGET_STATE) else {}
    return running, state


def open_widget(plugin_config=None):
    if widget_running():
        return True, "悬浮窗已经在运行了。"

    pythonw = sys.executable
    if pythonw.lower().endswith("python.exe"):
        pythonw = pythonw[: -len("python.exe")] + "pythonw.exe"
    if not os.path.exists(pythonw):
        pythonw = sys.executable

    args = [WIDGET_SCRIPT]
    if plugin_config:
        args.extend(["--plugin-config", plugin_config])
    flags = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    subprocess.Popen([pythonw] + args, cwd=HERE, creationflags=flags, close_fds=True)

    deadline = time.time() + 12
    while time.time() < deadline:
        if widget_running():
            return True, "悬浮窗已打开，贴在 ChatGPT 窗口右上角。"
        time.sleep(0.25)
    return False, "悬浮窗启动超时，检查目录下的 widget.log。"


def _autostart_widget():
    """Show the widget whenever the plugin's MCP server is loaded."""
    if os.environ.get("API_BALANCE_NO_WIDGET") == "1":
        return
    time.sleep(0.6)
    try:
        okay, message = open_widget(plugin_config=CODEX_CONFIG)
        log("widget autostart: %s" % message)
        if not okay:
            return
    except Exception as exc:  # noqa: BLE001 - MCP must keep working
        log("widget autostart failed: %r" % exc)


def close_widget():
    try:
        _widget_command("quit", 3.0)
        return True, "悬浮窗已关闭。"
    except OSError:
        return False, "悬浮窗本来就没有在运行。"


def _fmt_money(amount, currency):
    if amount is None:
        return "未知"
    symbol = "¥" if currency == "CNY" else "$" if currency == "USD" else ""
    return "%s%.2f" % (symbol, amount)


def _days_left(text):
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(str(text).replace("Z", "+00:00").replace(" ", "T"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return (parsed - datetime.now(timezone.utc)).days


def report_balance():
    """Aggregate every configured source into one readable answer."""
    lines, data, errors = [], {}, []
    configured = False

    config = _read_json(os.path.join(HERE, "config.json"))
    detected = pricing_catalog.detect(_active_base_url(config))
    _price_config(config)

    if detected == "deepseek":
        configured = True
        try:
            relay = deepseek_balance()
            data["relay"] = relay
            lines.append("【DeepSeek 官方】")
            lines.append("- 剩余额度：%s" % _fmt_money(relay["remaining"], relay["currency"]))
            lines.append("- 赠送余额：%s" % _fmt_money(relay["gift"], relay["currency"]))
            lines.append("- 充值余额：%s" % _fmt_money(relay["topped_up"], relay["currency"]))
        except BalanceError as exc:
            errors.append("DeepSeek 官方：%s" % exc.message)
    elif detected == "ark":
        configured = True
        if not volcengine_config():
            errors.append("火山方舟：请在设置里填写火山 AccessKey / SecretKey，推理 API Key 不能查余额。")
        else:
            try:
                relay = volcengine_balance()
                data["relay"] = relay
                lines.append("【火山方舟】")
                lines.append("- 可用余额：%s" % _fmt_money(relay["remaining"], relay["currency"]))
                lines.append("- 现金余额：%s" % _fmt_money(relay["cash"], relay["currency"]))
                if relay.get("freeze"):
                    lines.append("- 冻结金额：%s" % _fmt_money(relay["freeze"], relay["currency"]))
                if relay.get("arrears"):
                    lines.append("- 欠费金额：%s" % _fmt_money(relay["arrears"], relay["currency"]))
            except BalanceError as exc:
                errors.append("火山方舟：%s" % exc.message)
    else:
        cfg = relay_config()
        if cfg:
            configured = True
            try:
                relay, _ = probe_relay(cfg)
                data["relay"] = relay
                lines.append("【API 中转站】%s" % relay.get("provider"))
                lines.append("- 剩余额度：%s" % _fmt_money(relay["remaining"], relay["currency"]))
                if relay.get("cny"):
                    lines.append("- 折合人民币：¥%.2f" % relay["cny"])
                if relay.get("used") is not None:
                    lines.append("- 已使用：%s" % _fmt_money(relay["used"], relay["currency"]))
                elif relay.get("today_cost") is not None:
                    lines.append("- 今日消耗：%s" % _fmt_money(
                        relay["today_cost"], relay["currency"]
                    ))
                    lines.append("- 今日请求：%s 次" % (relay.get("today_requests") or 0))
                if relay.get("granted"):
                    lines.append("- 总额度：%s" % _fmt_money(relay["granted"], relay["currency"]))
            except BalanceError as exc:
                errors.append("中转站：%s" % exc.message)
                if exc.detail.get("diagnostics"):
                    data["relay_diagnostics"] = exc.detail["diagnostics"]

        cfg = aliyun_config()
        if cfg:
            configured = True
            try:
                balance = aliyun_balance()
                data["aliyun"] = balance
                lines.append("")
                lines.append("【阿里云账户】")
                lines.append("- 现金余额：%s" % _fmt_money(balance["cash"], balance["currency"]))
                lines.append("- 可用额度：%s" % _fmt_money(balance["available"], balance["currency"]))
            except BalanceError as exc:
                errors.append("阿里云余额：%s" % exc.message)
            try:
                coupons = aliyun_coupons()
                data["coupons"] = coupons
                if not coupons:
                    data["coupon_probe"] = dict(LAST_COUPON_QUERY)
                total = sum(coupon["balance"] for coupon in coupons)
                lines.append("- 代金券余额：¥%.2f（%d 张）" % (total, len(coupons)))
                for coupon in coupons:
                    days = _days_left(coupon["expiry"])
                    suffix = "，剩 %d 天" % days if days is not None else ""
                    lines.append(
                        "  · %s 剩 ¥%.2f / 面额 ¥%.2f，%s 到期%s"
                        % (coupon["name"] or "代金券", coupon["balance"], coupon["amount"],
                           coupon["expiry"][:10], suffix)
                    )
            except BalanceError as exc:
                errors.append("代金券：%s" % exc.message)

    if not configured:
        lines.append("没有配置任何数据源。")
    if errors:
        lines.append("")
        lines.extend("! " + error for error in errors)
    data["errors"] = errors
    return "\n".join(lines), data


# --------------------------------------------------------------------------
# local token ledger
# --------------------------------------------------------------------------


def _empty_usage():
    return {
        "input_tokens": 0,
        "cached_input_tokens": 0,
        "cache_write_input_tokens": 0,
        "output_tokens": 0,
        "reasoning_output_tokens": 0,
        "total_tokens": 0,
    }


def _add_usage(total, usage):
    result = dict(total)
    for key in result:
        result[key] += int(_num((usage or {}).get(key), 0) or 0)
    return result


def _parse_time(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone()


def _model_label(model):
    if not model:
        return "未知模型"
    return MODEL_NAMES.get(model, model)


def _active_base_url(config):
    relay = config.get("relay") or {}
    base = (relay.get("base_url") or "").strip()
    provider_base = _codex_provider().get("base_url") or ""
    follow = (not base and relay.get("inherit", True)) or (
        base and provider_base and _url_host(base) != _url_host(provider_base)
    )
    if provider_base and follow:
        return provider_base
    return base


def _url_host(url):
    return (url or "").split("//")[-1].split("/")[0].split(":")[0].lower()


def _pricing_profiles(pricing, base_url):
    profiles = pricing.get("profiles") or {}
    if not isinstance(profiles, dict):
        return {}
    host = _url_host(base_url)
    return {
        key: profile for key, profile in profiles.items()
        if isinstance(profile, dict) and host
        and _url_host(profile.get("base_url") or key) == host
    }


def _pricing_key_fingerprint(base_url, api_key):
    if not api_key:
        return ""
    return hashlib.sha256((_url_host(base_url) + "\0" + api_key).encode("utf-8")).hexdigest()


def _active_api_key(config):
    relay = config.get("relay") or {}
    key = os.environ.get("RELAY_API_KEY") or relay.get("api_key") or ""
    provider = _codex_provider()
    base = _active_base_url(config)
    if _url_host(base) != _url_host(relay.get("base_url")) or (
        not key and relay.get("inherit", True)
    ):
        key = provider.get("api_key") or key
    return key


def _pricing_profile_key(pricing, base_url, api_key=""):
    profiles = _pricing_profiles(pricing, base_url)
    bindings = pricing.get("profile_bindings") or {}
    fingerprint = _pricing_key_fingerprint(base_url, api_key)
    bound = bindings.get(fingerprint)
    if bound in profiles:
        return bound
    if len(profiles) == 1:
        return next(iter(profiles))
    selected = str(pricing.get("profile") or "")
    # Old configurations have no key bindings; keep their explicit selection.
    if selected in profiles and (
        not fingerprint or not any(key in profiles for key in bindings.values())
    ):
        return selected
    return ""


def _bind_pricing_profile(pricing, profile_key, base_url, api_key):
    if profile_key not in _pricing_profiles(pricing, base_url):
        raise ValueError("Pricing profile does not match the API address")
    pricing.update(provider="custom", profile=profile_key, provider_base=base_url)
    fingerprint = _pricing_key_fingerprint(base_url, api_key)
    if fingerprint:
        bindings = dict(pricing.get("profile_bindings") or {})
        bindings[fingerprint] = profile_key
        pricing["profile_bindings"] = bindings


def _price_config(config=None):
    from_disk = not isinstance(config, dict)
    config = config if not from_disk else _read_json(os.path.join(HERE, "config.json"))
    pricing = dict(config.get("pricing") or {})
    active_base = _active_base_url(config)
    candidates = _pricing_profiles(pricing, active_base)
    catalog = pricing_catalog.load()
    detected = pricing_catalog.detect(active_base)
    if active_base and str(pricing.get("provider_base") or "") != active_base:
        previous = (pricing.get("provider"), pricing.get("provider_base"), pricing.get("profile"))
        pricing["provider"] = detected or ("custom" if candidates else "")
        pricing["provider_base"] = active_base
        # 换站后旧站的自定义档不再适用，价格回到新站的官方表。
        if pricing.get("profile") not in candidates:
            pricing["profile"] = ""
        config["pricing"] = pricing
        if from_disk:
            try:
                _save_config(config)
            except OSError as exc:
                pricing["provider"], pricing["provider_base"], pricing["profile"] = previous
                log("cannot save detected provider: %s" % exc)
    selected = str(pricing.get("provider") or "").strip()
    if selected not in pricing_catalog.PROVIDERS:
        selected = ""
    provider = (
        selected
        or pricing_catalog.detect(_active_base_url(config))
        or pricing_catalog.detect_hint(_active_base_url(config))
    )
    if not provider and candidates:
        provider = "custom"
    profile_key = (
        _pricing_profile_key(pricing, active_base, _active_api_key(config))
        if provider == "custom" else ""
    )
    profile = candidates.get(profile_key) or {}
    ambiguous = provider == "custom" and len(candidates) > 1 and not profile_key
    # 官方平台按官网价，不复用中转站的分组倍率。
    default_multiplier = (
        1.0 if pricing.get("profiles") or provider in ("dashscope", "deepseek", "ark")
        else _num(pricing.get("multiplier"), 1.0)
    )
    multiplier = _num(profile.get("multiplier"), default_multiplier)
    if multiplier is None or not math.isfinite(multiplier) or multiplier < 0:
        multiplier = 1.0
    meta = dict(pricing_catalog.PROVIDERS.get(provider) or {})
    if profile_key:
        models = dict(profile.get("models") or {})
    elif not pricing.get("profiles") or provider in ("dashscope", "deepseek", "ark"):
        models = dict(pricing.get("models") or {})
    else:
        models = {}
    official = pricing_catalog.models_for(catalog, provider) if provider else {}
    for model, manual in models.items():
        if model in official and isinstance(manual.get("idle"), dict):
            official[model] = dict(official[model], idle=manual["idle"])
    return {
        "profile": profile,
        "profile_requested": str(pricing.get("profile") or ""),
        "profile_key": profile_key,
        "profile_name": profile.get("name") or profile_key,
        "profile_ambiguous": ambiguous,
        "reference": {} if ambiguous else REFERENCE_PRICES,
        "multiplier": multiplier,
        "models": models,
        "provider": provider,
        "official": official,
        "catalog": {
            "label": meta.get("label") or "",
            "source": meta.get("source") or "",
            "schedule": meta.get("schedule") or "",
            "fetched_at": catalog.get("fetched_at") or "",
            "error": catalog.get("error") or [],
        },
    }


def _clock_minutes(value):
    try:
        hour, minute = str(value).strip().split(":", 1)
        hour, minute = int(hour), int(minute)
    except (TypeError, ValueError):
        return None
    if minute == 0 and hour == 24:
        return 24 * 60
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        return None
    return hour * 60 + minute


def _idle_windows(idle):
    if not isinstance(idle, dict):
        return []
    windows = idle.get("windows")
    if not isinstance(windows, list):
        windows = [{"start": idle.get("start"), "end": idle.get("end")}]
    result = []
    for window in windows:
        if not isinstance(window, dict):
            continue
        start = _clock_minutes(window.get("start"))
        end = _clock_minutes(window.get("end"))
        if end == 0:
            end = 24 * 60
        if start is None or end is None or start >= 24 * 60 or start == end:
            continue
        result.append((start, end))
    return result


def _idle_intervals(idle, at):
    if not isinstance(idle, dict) or at is None:
        return []
    if idle.get("enabled", True) is False:
        return []
    windows = _idle_windows(idle)
    weekend_all_day = bool(idle.get("weekend_all_day"))
    holidays = set(idle.get("days") or ())
    if not windows and not weekend_all_day and not holidays:
        return []
    local = at.astimezone()
    intervals = []
    for offset in range(-1, 10):
        day = local.date() + timedelta(days=offset)
        midnight = datetime.combine(day, datetime.min.time(), tzinfo=local.tzinfo)
        if (weekend_all_day and day.weekday() >= 5) or day.isoformat() in holidays:
            intervals.append((midnight, midnight + timedelta(days=1)))
        for start, end in windows:
            start_at = midnight + timedelta(minutes=start)
            end_at = midnight + timedelta(minutes=end)
            if end_at <= start_at:
                end_at += timedelta(days=1)
            intervals.append((start_at, end_at))
    merged = []
    for start_at, end_at in sorted(intervals):
        if merged and start_at <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end_at))
        else:
            merged.append((start_at, end_at))
    return merged


def _idle_active(idle, at):
    if at is None:
        return False
    return any(start <= at < end for start, end in _idle_intervals(idle, at))


def _idle_transition(idle, at):
    if at is None:
        return None
    intervals = _idle_intervals(idle, at)
    if not intervals:
        return None
    active_interval = next(
        ((start, end) for start, end in intervals if start <= at < end),
        None,
    )
    active = active_interval is not None
    if active:
        target = active_interval[1]
    else:
        target = min(start for start, _end in intervals if start > at)
    return {
        "to": "standard" if active else "idle",
        "at": target.isoformat(timespec="seconds"),
        "seconds": max(0, int((target - at).total_seconds())),
    }


def _tier(entry, tokens):
    tiers = entry.get("tiers") or []
    chosen = tiers[0] if tiers else None
    for tier in tiers:
        if tier.get("upto") and (tokens or 0) <= tier["upto"]:
            chosen = tier
            break
    if not chosen:
        return entry
    resolved = dict(entry)
    for key in ("input", "cached", "output"):
        if chosen.get(key) is not None:
            resolved[key] = chosen[key]
    return resolved


def _price_for(model, price_config, at=None, tokens=None):
    configured = (price_config.get("official") or {}).get(model) or {}
    source = "official"
    if not configured:
        configured = (price_config.get("models") or {}).get(model) or {}
        source = "configured"
    if configured.get("tiers"):
        configured = _tier(configured, tokens)
    values = {}
    for key in ("input", "cached", "output"):
        value = _num(configured.get(key))
        if value is not None and value >= 0:
            values[key] = value
    if len(values) != 3:
        reference = price_config.get("reference", REFERENCE_PRICES).get(model)
        values = dict(reference) if reference else None
        source = "reference" if reference else "missing"
    if values and _idle_active(configured.get("idle"), at):
        idle = configured.get("idle") or {}
        idle_prices = {}
        for key in ("input", "cached", "output"):
            value = _num(idle.get(key))
            if value is not None and value >= 0:
                idle_prices[key] = value
        if len(idle_prices) == 3:
            return idle_prices, "idle"
    return values, source


def _estimate_cost(usage, model, price_config, at=None):
    usage = usage or {}
    tokens = (
        int(_num(usage.get("input_tokens"), 0) or 0)
        + int(_num(usage.get("cache_write_input_tokens"), 0) or 0)
    )
    prices, source = _price_for(model, price_config, at, tokens)
    if not prices:
        return None, source
    cached = int(_num(usage.get("cached_input_tokens"), 0) or 0)
    raw_input = int(_num(usage.get("input_tokens"), 0) or 0)
    cache_write = int(_num(usage.get("cache_write_input_tokens"), 0) or 0)
    billable = max(raw_input - cached, 0) + max(cache_write, 0)
    output = int(_num(usage.get("output_tokens"), 0) or 0)
    cost = (
        billable * prices["input"]
        + cached * prices["cached"]
        + output * prices["output"]
    ) / 1_000_000.0
    return round(cost * price_config["multiplier"], 6), source


def _read_session_titles():
    titles, updated = {}, {}
    try:
        with open(SESSION_INDEX, "r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    item = json.loads(line)
                except ValueError:
                    continue
                thread_id = item.get("id")
                if thread_id:
                    titles[thread_id] = item.get("thread_name") or ""
                    updated[thread_id] = item.get("updated_at") or ""
    except OSError:
        pass
    return titles, updated


def _new_file_entry():
    return {
        "offset": 0,
        "size": 0,
        "mtime": 0.0,
        "thread_id": "",
        "started_at": "",
        "updated_at": "",
        "thread_source": "",
        "cwd": "",
        "last_model": "",
        "model_changed_at": "",
        "turns": {},
    }


def _consume_session_bytes(entry, raw):
    for line in raw.splitlines(keepends=True):
        if not line.endswith(b"\n"):
            break
        try:
            text = line.decode("utf-8", "replace").strip()
        except Exception:  # noqa: BLE001
            continue
        if not text:
            continue
        if not any(
            marker in text
            for marker in (
                '"session_meta"',
                '"turn_context"',
                '"token_usage_record"',
                '"thread_settings_applied"',
            )
        ):
            continue
        try:
            record = json.loads(text)
        except ValueError:
            continue
        kind = record.get("type")
        payload = record.get("payload") or {}
        stamp = record.get("timestamp") or ""
        if kind == "session_meta":
            entry["thread_id"] = payload.get("id") or payload.get("session_id") or ""
            entry["started_at"] = payload.get("timestamp") or stamp
            entry["cwd"] = payload.get("cwd") or ""
            entry["thread_source"] = payload.get("thread_source") or ""
            entry["last_model"] = payload.get("model") or entry.get("last_model") or ""
        elif kind == "turn_context":
            turn_id = payload.get("turn_id") or ""
            if turn_id:
                turn = entry["turns"].setdefault(turn_id, {})
                turn["at"] = stamp or turn.get("at") or ""
                turn["model"] = payload.get("model") or turn.get("model") or entry["last_model"]
                entry["last_model"] = turn["model"] or entry["last_model"]
                entry["updated_at"] = stamp or entry["updated_at"]
        elif kind == "event_msg" and payload.get("type") == "thread_settings_applied":
            settings = payload.get("thread_settings") or {}
            model = settings.get("model")
            if model:
                entry["last_model"] = model
                entry["model_changed_at"] = stamp or entry.get("model_changed_at") or ""
        elif kind == "token_usage_record":
            turn_id = payload.get("turn_id") or ""
            if not turn_id:
                continue
            turn = entry["turns"].setdefault(turn_id, {})
            usage = payload.get("turn_token_usage") or payload.get("usage") or {}
            # Every token_usage_record carries a cumulative turn total. Keeping
            # the latest one avoids counting each tool-loop request twice.
            turn["usage"] = dict(usage)
            turn["at"] = stamp or turn.get("at") or entry["updated_at"]
            turn["model"] = turn.get("model") or entry["last_model"] or ""
            entry["updated_at"] = stamp or entry["updated_at"]


def _turn_pricing_snapshot(turn, model, price_config):
    if turn.get("pricing_snapshot"):
        return False
    prices, _source = _price_for(model, price_config, _parse_time(turn.get("at")))
    if not prices:
        return False
    turn["pricing_snapshot"] = copy.deepcopy({
        "multiplier": price_config["multiplier"],
        "models": {model: price_config["models"][model]} if model in price_config["models"] else {},
        "official": {model: price_config["official"][model]} if model in price_config["official"] else {},
        "reference": {model: price_config.get("reference", REFERENCE_PRICES).get(model)}
                     if model in price_config.get("reference", REFERENCE_PRICES) else {},
        "profile_key": price_config.get("profile_key") or "",
        "profile_name": price_config.get("profile_name") or "",
    })
    return True


def _scan_sessions(max_days=45, price_config=None):
    cache = _read_json(USAGE_CACHE_PATH)
    if not isinstance(cache, dict) or cache.get("version") != 1:
        cache = {"version": 1, "files": {}}
    files = cache.setdefault("files", {})
    cutoff = time.time() - max_days * 86400
    seen = set()
    changed = False

    for base, _dirs, names in os.walk(SESSION_ROOT):
        for name in names:
            if not name.endswith(".jsonl"):
                continue
            path = os.path.join(base, name)
            try:
                stat = os.stat(path)
            except OSError:
                continue
            if stat.st_mtime < cutoff:
                continue
            seen.add(path)
            entry = files.get(path) or _new_file_entry()
            size = int(stat.st_size)
            if size < int(entry.get("offset") or 0):
                entry = _new_file_entry()
                changed = True
            offset = int(entry.get("offset") or 0)
            if size > offset:
                try:
                    with open(path, "rb") as handle:
                        handle.seek(offset)
                        raw = handle.read()
                except OSError:
                    continue
                _consume_session_bytes(entry, raw)
                consumed = raw.rfind(b"\n") + 1
                entry["offset"] = offset + consumed
                changed = changed or consumed > 0
            if not files.get(path):
                changed = True
            entry["size"] = size
            entry["mtime"] = stat.st_mtime
            if price_config:
                for turn in (entry.get("turns") or {}).values():
                    model = turn.get("model") or entry.get("last_model") or ""
                    if turn.get("usage") and _turn_pricing_snapshot(turn, model, price_config):
                        changed = True
            files[path] = entry

    for path in list(files):
        if path not in seen and (
            not files[path].get("mtime") or files[path]["mtime"] < cutoff
        ):
            files.pop(path, None)
            changed = True

    if changed:
        try:
            temp = USAGE_CACHE_PATH + ".tmp"
            with open(temp, "w", encoding="utf-8") as handle:
                json.dump(cache, handle, ensure_ascii=False)
            os.replace(temp, USAGE_CACHE_PATH)
        except OSError:
            pass
    return files


def _apply_model_change(thread, entry):
    changed_at = _parse_time(entry.get("model_changed_at"))
    if changed_at is None:
        return
    updated_at = _parse_time(thread.get("updated_at"))
    if updated_at is None or changed_at > updated_at:
        thread["updated_at"] = changed_at.isoformat(timespec="seconds")
        thread["model"] = entry.get("last_model") or thread.get("model") or ""


def _pricing_summary(price_config, model, at):
    entry = (price_config["official"].get(model) or price_config["models"].get(model) or {})
    return {
        "profile_id": price_config.get("profile_key") or "",
        "profile_name": price_config.get("profile_name") or "",
        "profile_ambiguous": price_config.get("profile_ambiguous", False),
        "multiplier": price_config["multiplier"],
        "models": price_config["models"],
        "reference": price_config.get("reference", REFERENCE_PRICES),
        "provider": price_config["provider"],
        "catalog": price_config["catalog"],
        "current_entry": {key: value for key, value in entry.items() if key != "tiers"} or None,
        "current_source": _price_for(model, price_config, at)[1],
        "idle_transition": _idle_transition(entry.get("idle"), at),
    }


def usage_report(max_days=45):
    """Aggregate local Codex usage without needing a relay log endpoint."""
    now = datetime.now().astimezone()
    cutoff = now - timedelta(days=max_days)
    titles, index_updated = _read_session_titles()
    price_config = _price_config()
    files = _scan_sessions(max_days=max_days, price_config=price_config)
    threads = {}
    all_turns = []

    for path, entry in files.items():
        thread_id = entry.get("thread_id") or ""
        if not thread_id:
            continue
        title = (titles.get(thread_id) or "").strip()
        if not title:
            title = os.path.basename(entry.get("cwd") or "") or "未命名对话"
        if len(title) > 64:
            title = title[:63] + "…"
        thread = threads.setdefault(
            thread_id,
            {
                "id": thread_id,
                "title": title,
                "cwd": entry.get("cwd") or "",
                "thread_source": entry.get("thread_source") or "",
                "updated_at": entry.get("updated_at") or "",
                "model": "",
                "tokens": _empty_usage(),
                "cost": 0.0,
                "cost_known": True,
                "turns": 0,
            },
        )
        if title and thread["title"] in ("未命名对话", ""):
            thread["title"] = title
        for turn_id, turn in (entry.get("turns") or {}).items():
            at = _parse_time(turn.get("at"))
            if at is None or at < cutoff:
                continue
            usage = turn.get("usage") or {}
            if not usage:
                continue
            model = turn.get("model") or entry.get("last_model") or ""
            turn_pricing = turn.get("pricing_snapshot") or price_config
            cost, source = _estimate_cost(usage, model, turn_pricing, at)
            item = {
                "thread_id": thread_id,
                "title": thread["title"],
                "turn_id": turn_id,
                "at": at.isoformat(timespec="seconds"),
                "model": model,
                "model_name": _model_label(model),
                "usage": dict(usage),
                "cost": cost,
                "price_source": source,
                "profile_id": turn_pricing.get("profile_key") or "",
                "profile_name": turn_pricing.get("profile_name") or "",
                "multiplier": turn_pricing["multiplier"],
            }
            all_turns.append(item)
            thread["tokens"] = _add_usage(thread["tokens"], usage)
            thread["turns"] += 1
            if cost is None:
                thread["cost_known"] = False
            else:
                thread["cost"] += cost
            if at.isoformat(timespec="seconds") >= (thread["updated_at"] or ""):
                thread["updated_at"] = at.isoformat(timespec="seconds")
                thread["model"] = model
        _apply_model_change(thread, entry)

    for thread in threads.values():
        thread["cost"] = round(thread["cost"], 6)
        thread["model_name"] = _model_label(thread.get("model"))
    all_turns.sort(key=lambda item: item["at"], reverse=True)

    user_threads = [
        thread for thread in threads.values()
        if thread.get("title") != "Guardian review"
        and thread.get("thread_source") not in ("subagent", "review")
    ]
    user_threads.sort(
        key=lambda thread: _parse_time(thread.get("updated_at")) or datetime.min.replace(tzinfo=now.tzinfo),
        reverse=True,
    )
    current = user_threads[0] if user_threads else None

    today_start = datetime.combine(now.date(), datetime.min.time()).astimezone()
    week_start = today_start - timedelta(days=6)
    totals = {
        "today": {"tokens": _empty_usage(), "cost": 0.0, "turns": 0, "cost_known": True},
        "week": {"tokens": _empty_usage(), "cost": 0.0, "turns": 0, "cost_known": True},
    }
    daily = {}
    models = {}
    for item in all_turns:
        at = _parse_time(item["at"])
        if at is None:
            continue
        bucket = None
        if at >= today_start:
            bucket = totals["today"]
        if at >= week_start:
            totals["week"]["tokens"] = _add_usage(totals["week"]["tokens"], item["usage"])
            totals["week"]["turns"] += 1
            if item["cost"] is None:
                totals["week"]["cost_known"] = False
            else:
                totals["week"]["cost"] += item["cost"]
        if bucket:
            bucket["tokens"] = _add_usage(bucket["tokens"], item["usage"])
            bucket["turns"] += 1
            if item["cost"] is None:
                bucket["cost_known"] = False
            else:
                bucket["cost"] += item["cost"]
        day_key = at.strftime("%Y-%m-%d")
        day = daily.setdefault(day_key, {"tokens": _empty_usage(), "cost": 0.0, "turns": 0})
        day["tokens"] = _add_usage(day["tokens"], item["usage"])
        day["cost"] += item["cost"] or 0.0
        day["turns"] += 1

        model_key = item["model"] or "unknown"
        model = models.setdefault(
            model_key,
            {
                "model": model_key,
                "model_name": item["model_name"],
                "tokens": _empty_usage(),
                "cost": 0.0,
                "turns": 0,
                "cost_known": True,
            },
        )
        model["tokens"] = _add_usage(model["tokens"], item["usage"])
        model["turns"] += 1
        if item["cost"] is None:
            model["cost_known"] = False
        else:
            model["cost"] += item["cost"]

    for total in totals.values():
        total["cost"] = round(total["cost"], 6)
    for model in models.values():
        model["cost"] = round(model["cost"], 6)

    series = []
    for offset in range(6, -1, -1):
        day = today_start - timedelta(days=offset)
        key = day.strftime("%Y-%m-%d")
        value = daily.get(key) or {"tokens": _empty_usage(), "cost": 0.0, "turns": 0}
        series.append(
            {
                "date": key,
                "label": day.strftime("%m/%d"),
                "weekday": "一二三四五六日"[day.weekday()],
                "total_tokens": value["tokens"]["total_tokens"],
                "cost": round(value["cost"], 6),
                "turns": value["turns"],
            }
        )

    current_model = current.get("model") if current else ""
    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "current": current,
        "today": totals["today"],
        "week": totals["week"],
        "series": series,
        "models": sorted(models.values(), key=lambda item: item["tokens"]["total_tokens"], reverse=True),
        "threads": user_threads[:20],
        "recent_turns": all_turns[:30],
        "pricing": _pricing_summary(price_config, current_model, now),
    }


def model_display_name(model):
    return _model_label(model)


# --------------------------------------------------------------------------
# widget snapshot cache
# --------------------------------------------------------------------------


_BALANCE_CACHE = {"at": 0.0, "text": "", "data": None}
_BALANCE_LOCK = threading.Lock()
_BALANCE_HISTORY_LOCK = threading.Lock()
_BALANCE_LAST_SAMPLE = {"at": 0.0, "total": None, "source": ""}


def _balance_total(data):
    relay = data.get("relay") or {}
    coupons = data.get("coupons") or []
    aliyun = data.get("aliyun") or {}
    if relay and relay.get("remaining") is not None:
        return _num(relay.get("remaining"))
    if coupons:
        return round(sum(_num(item.get("balance"), 0) or 0 for item in coupons), 4)
    if aliyun:
        return _num(aliyun.get("available"), _num(aliyun.get("cash"), 0))
    return None


def _balance_source(data):
    relay = data.get("relay") or {}
    if relay.get("format") == "deepseek":
        return "deepseek"
    if relay.get("format") == "volcengine":
        return "ark"
    if relay:
        return "relay"
    if data.get("coupons") or data.get("aliyun"):
        return "aliyun"
    return ""


def _record_balance_sample(data):
    total = _balance_total(data)
    if total is None:
        return
    source = _balance_source(data)
    now = time.time()
    with _BALANCE_HISTORY_LOCK:
        if (
            _BALANCE_LAST_SAMPLE["total"] is not None
            and _BALANCE_LAST_SAMPLE["source"] == source
            and abs(total - _BALANCE_LAST_SAMPLE["total"]) < 0.0001
            and now - _BALANCE_LAST_SAMPLE["at"] < 300
        ):
            return
        _BALANCE_LAST_SAMPLE.update({"at": now, "total": total, "source": source})
    sample = {
        "at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "total": total,
        "source": source,
    }
    try:
        with _BALANCE_HISTORY_LOCK:
            with open(BALANCE_HISTORY_PATH, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(sample, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _balance_history(hours=24, source=""):
    cutoff = datetime.now().astimezone() - timedelta(hours=hours)
    samples = []
    try:
        with open(BALANCE_HISTORY_PATH, "r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    item = json.loads(line)
                except ValueError:
                    continue
                if source and item.get("source") != source:
                    continue
                stamp = _parse_time(item.get("at"))
                if stamp and stamp >= cutoff:
                    samples.append(
                        {
                            "at": stamp.isoformat(timespec="seconds"),
                            "total": _num(item.get("total"), 0) or 0,
                        }
                    )
    except OSError:
        pass
    if not samples:
        return {"samples": [], "day_delta": 0.0, "last_change": None}
    first = samples[0]["total"]
    last = samples[-1]["total"]
    changes = []
    previous = samples[0]["total"]
    for item in samples[1:]:
        if abs(item["total"] - previous) >= 0.0001:
            changes.append(
                {
                    "at": item["at"],
                    "delta": round(item["total"] - previous, 4),
                    "total": item["total"],
                }
            )
        previous = item["total"]
    return {
        "samples": samples[-200:],
        "day_delta": round(last - first, 4),
        "last_change": changes[-1] if changes else None,
    }


def cached_balance(max_age=8.0, force=False):
    now = time.time()
    with _BALANCE_LOCK:
        if (
            not force
            and _BALANCE_CACHE["data"] is not None
            and now - _BALANCE_CACHE["at"] < max(0.0, max_age)
        ):
            return _BALANCE_CACHE["text"], dict(_BALANCE_CACHE["data"])
        text, data = report_balance()
        _BALANCE_CACHE.update({"at": now, "text": text, "data": data})
        _record_balance_sample(data)
        return text, dict(data)


def widget_snapshot(max_age=8.0, force=False):
    text, data = cached_balance(max_age=max_age, force=force)
    data = dict(data)
    data["text"] = text
    data["usage"] = usage_report()
    data["balance_history"] = _balance_history(source=_balance_source(data))
    data["cache_age"] = round(max(0.0, time.time() - _BALANCE_CACHE["at"]), 1)
    return data


# --------------------------------------------------------------------------
# MCP transport
# --------------------------------------------------------------------------


TOOLS = [
    {
        "name": "get_balance",
        "description": (
            "查询 API 额度余额：中转站（OpenAI 兼容接口）剩余额度，"
            "以及阿里云账户现金余额 + 代金券（含 300 元学生券）剩余。"
            "用户问“我 API 还剩多少钱 / 券还剩多少”时优先调用。"
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_aliyun_coupons",
        "description": "查询阿里云代金券列表：面额、剩余余额、状态、到期时间。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "description": "Available（可用，默认）/ Used / Expired",
                    "default": "Available",
                }
            },
        },
    },
    {
        "name": "get_usage",
        "description": (
            "查询本地 Codex 会话的 token 用量与费用估算：当前对话、今日、近 7 天、"
            "按模型分组和最近每轮明细。费用按配置单价 × 分组倍率估算。"
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "call_aliyun_api",
        "description": "兜底：直接调用任意阿里云 BSS OpenAPI 并返回原始 JSON。",
        "inputSchema": {
            "type": "object",
            "properties": {"action": {"type": "string"}, "params": {"type": "object"}},
            "required": ["action"],
        },
    },
    {
        "name": "diagnose",
        "description": "诊断：逐个尝试余额接口并返回状态码与响应片段，用于排查中转站字段差异。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "open_widget",
        "description": "打开余额悬浮窗（贴在 ChatGPT 窗口右上角，跟随窗口、切走自动隐藏）。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "close_widget",
        "description": "关闭余额悬浮窗。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "widget_status",
        "description": "查询悬浮窗是否在运行，以及它最后一次拿到的余额数据。",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def _text_result(text, is_error=False):
    result = {"content": [{"type": "text", "text": text}]}
    if is_error:
        result["isError"] = True
    return result


def run_tool(name, arguments):
    arguments = arguments or {}
    if name == "get_balance":
        text, data = report_balance()
        return _text_result(
            text + "\n\n```json\n" + json.dumps(data, ensure_ascii=False, indent=2) + "\n```"
        )
    if name == "get_aliyun_coupons":
        coupons = aliyun_coupons(arguments.get("status") or "Available")
        if not coupons:
            return _text_result("没有查到该状态的代金券。")
        return _text_result(json.dumps(coupons, ensure_ascii=False, indent=2))
    if name == "get_usage":
        data = usage_report()
        current = data.get("current") or {}
        today = data.get("today") or {}
        lines = [
            "【当前对话】%s" % (current.get("title") or "未找到"),
            "- 模型：%s" % (current.get("model_name") or "未知"),
            "- Token：%s" % ((current.get("tokens") or {}).get("total_tokens") or 0),
            "- 费用估算：¥%.4f" % (current.get("cost") or 0),
            "",
            "【今日】Token %s，费用估算 ¥%.4f" % (
                (today.get("tokens") or {}).get("total_tokens") or 0,
                today.get("cost") or 0,
            ),
            "",
            "```json",
            json.dumps(data, ensure_ascii=False, indent=2),
            "```",
        ]
        return _text_result("\n".join(lines))
    if name == "call_aliyun_api":
        action = arguments.get("action")
        if not action:
            return _text_result("缺少 action 参数。", True)
        payload = bss_call(action, arguments.get("params") or {})
        return _text_result(json.dumps(payload, ensure_ascii=False, indent=2))
    if name == "diagnose":
        cfg = relay_config()
        if not cfg:
            return _text_result("没有找到中转站配置。", True)
        _, diagnostics = probe_relay(cfg, verbose=True)
        return _text_result(json.dumps(diagnostics, ensure_ascii=False, indent=2))
    if name == "open_widget":
        okay, message = open_widget(plugin_config=CODEX_CONFIG)
        return _text_result(message, not okay)
    if name == "close_widget":
        okay, message = close_widget()
        return _text_result(message, not okay)
    if name == "widget_status":
        running, state = widget_state()
        if not running:
            return _text_result("悬浮窗未运行。用 open_widget 打开。")
        return _text_result(
            "悬浮窗正在运行（pid %s）。\n%s"
            % (state.get("pid"), json.dumps(state, ensure_ascii=False, indent=2))
        )
    return _text_result("未知工具：%s" % name, True)


def handle_message(message):
    method = message.get("method")
    request_id = message.get("id")

    def ok(result):
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    if method == "initialize":
        return ok(
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                "instructions": (
                    "查询 API 额度与阿里云代金券余额，并管理余额悬浮窗。"
                    "用户问余额时优先调用 get_balance；查不到时调用 diagnose；"
                    "用户说打开/关掉悬浮窗时调用 open_widget / close_widget。"
                ),
            }
        )
    if method == "ping":
        return ok({})
    if method == "tools/list":
        return ok({"tools": TOOLS})
    if method == "resources/list":
        return ok({"resources": []})
    if method == "prompts/list":
        return ok({"prompts": []})
    if method == "tools/call":
        params = message.get("params") or {}
        try:
            return ok(run_tool(params.get("name"), params.get("arguments")))
        except BalanceError as exc:
            return ok(
                _text_result(
                    "查询失败：%s\n%s"
                    % (exc.message, json.dumps(exc.as_dict(), ensure_ascii=False, indent=2)),
                    True,
                )
            )
        except Exception as exc:  # noqa: BLE001 - keep the server alive
            log("tool %r crashed: %r" % (params.get("name"), exc))
            return ok(_text_result("插件内部错误：%r" % exc, True))

    if request_id is None:
        return None
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": -32601, "message": "Method not found: %s" % method},
    }


def serve():
    log("MCP server ready (stdio, protocol %s)" % PROTOCOL_VERSION)
    threading.Thread(target=_autostart_widget, daemon=True).start()
    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            log("ignoring non-JSON input")
            continue
        if isinstance(message, list):
            responses = [handle_message(item) for item in message]
            responses = [item for item in responses if item is not None]
            if responses:
                sys.stdout.write(json.dumps(responses, ensure_ascii=False) + "\n")
                sys.stdout.flush()
            continue
        response = handle_message(message)
        if response is not None:
            sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            sys.stdout.flush()
    return 0


# --------------------------------------------------------------------------
# self test / CLI
# --------------------------------------------------------------------------


def selftest():
    checks = []
    checks.append(("percent_encode space", percent_encode("a b") == "a%20b"))
    checks.append(("percent_encode star", percent_encode("a*b") == "a%2Ab"))
    checks.append(("percent_encode slash", percent_encode("/") == "%2F"))
    checks.append(("percent_encode tilde", percent_encode("~") == "~"))

    sig_a = rpc_sign({"Action": "QueryAccountBalance", "Version": BSS_VERSION}, "secret")
    sig_b = rpc_sign({"Version": BSS_VERSION, "Action": "QueryAccountBalance"}, "secret")
    checks.append(("signature stable", sig_a == sig_b))
    checks.append(("signature base64 sha1", len(sig_a) == 28 and sig_a.endswith("=")))

    newapi = {
        "success": True,
        "data": {"quota": 1250000, "used_quota": 750000, "request_count": 12},
    }
    status = {"success": True, "data": {"quota_per_unit": 500000, "usd_exchange_rate": 7.2}}
    parsed = parse_newapi(newapi, status)
    checks.append(("newapi remaining", parsed["remaining"] == 2.5))
    checks.append(("newapi used", parsed["used"] == 1.5))
    checks.append(("newapi cny", parsed["cny"] == 18.0))

    billing = parse_openai_billing({"hard_limit_usd": 300}, {"total_usage": 12345})
    checks.append(("openai remaining", billing["remaining"] == 176.55))

    grants = parse_credit_grants(
        {"total_granted": 300, "total_used": 120, "total_available": 180}
    )
    checks.append(("credit grants", grants["remaining"] == 180))

    usage_v1 = parse_usage_v1({
        "remaining": 12.5,
        "balance": 1.0,
        "unit": "USD",
        "usage": {"today": {"actual_cost": 0.2, "total_tokens": 300}},
    })
    checks.append((
        "usage v1 balance",
        usage_v1["remaining"] == 12.5
        and usage_v1["today_cost"] == 0.2
        and usage_v1["today_tokens"] == 300,
    ))

    deepseek = parse_deepseek_balance({
        "is_available": True,
        "balance_infos": [{
            "currency": "CNY",
            "total_balance": "1.23",
            "granted_balance": "0.23",
            "topped_up_balance": "1.00",
        }],
    })
    checks.append(("deepseek balance", deepseek["remaining"] == 1.23))
    checks.append(("deepseek split", deepseek["gift"] == 0.23 and deepseek["topped_up"] == 1.0))
    checks.append(("deepseek history source", _balance_source({"relay": deepseek}) == "deepseek"))

    volc = parse_volc_balance({"Result": {
        "AvailableBalance": "88.50",
        "CashBalance": "80.00",
        "CreditLimit": "0.00",
        "FreezeAmount": "1.50",
        "ArrearsBalance": "0.00",
    }})
    checks.append(("volc balance", volc["remaining"] == 88.5 and volc["cash"] == 80.0))
    checks.append(("volc history source", _balance_source({"relay": volc}) == "ark"))
    _, signed = volc_signed_request(
        "QueryBalanceAcct",
        {"access_key_id": "AKTEST", "access_key_secret": "SKTEST"},
        datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
    )
    checks.append((
        "volc signer",
        signed["Authorization"].endswith(
            "Signature=f629370d7fa0a6580f947d67bb0b4281846583269f738c628bb8e0c4f1255106"
        ),
    ))
    checks.append((
        "volc body hash",
        signed["X-Content-Sha256"]
        == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    ))

    coupon_payload = {
        "Data": {
            "CashCoupons": {"CashCoupon": [{"CashCouponName": "学生券", "Amount": 300, "Balance": 240}]},
            "TotalCount": 1,
        }
    }
    box = coupon_payload["Data"]["CashCoupons"]
    items = box.get("CashCoupon")
    checks.append(("coupon shape", isinstance(items, list) and items[0]["Balance"] == 240))

    checks.append(("days_left", _days_left("2000-01-01T00:00:00Z") < 0))
    checks.append((
        "tool list",
        {tool["name"] for tool in TOOLS} >= {"get_balance", "get_usage", "diagnose"},
    ))

    priced = _estimate_cost(
        {"input_tokens": 1_000_000, "cached_input_tokens": 200_000, "output_tokens": 100_000},
        "priced-model",
        {
            "multiplier": 1.5,
            "models": {"priced-model": {"input": 2.0, "cached": 0.04, "output": 8.0}},
        },
    )
    checks.append(("group multiplier", priced == (3.612, "configured")))
    idle_config = {
        "multiplier": 1.5,
        "models": {
            "priced-model": {
                "input": 2.0,
                "cached": 0.04,
                "output": 8.0,
                "idle": {
                    "start": "22:00",
                    "end": "08:00",
                    "input": 1.0,
                    "cached": 0.1,
                    "output": 4.0,
                },
            }
        },
    }
    idle_usage = {
        "input_tokens": 1_000_000,
        "cached_input_tokens": 200_000,
        "output_tokens": 100_000,
    }
    at_idle = datetime(2026, 1, 1, 23, 30).astimezone()
    at_day = datetime(2026, 1, 1, 12, 0).astimezone()
    at_end = datetime(2026, 1, 2, 8, 0).astimezone()
    checks.append((
        "overnight idle price",
        _estimate_cost(idle_usage, "priced-model", idle_config, at_idle)
        == (1.83, "idle"),
    ))
    checks.append((
        "standard daytime price",
        _estimate_cost(idle_usage, "priced-model", idle_config, at_day)
        == (3.612, "configured"),
    ))
    checks.append((
        "idle end is exclusive",
        _estimate_cost(idle_usage, "priced-model", idle_config, at_end)
        == (3.612, "configured"),
    ))
    transition = _idle_transition(idle_config["models"]["priced-model"]["idle"], at_idle)
    checks.append((
        "idle countdown",
        transition is not None
        and transition["to"] == "standard"
        and transition["seconds"] == 8 * 3600 + 30 * 60,
    ))
    multi_idle = {
        "windows": [
            {"start": "00:30", "end": "08:30"},
            {"start": "12:00", "end": "14:00"},
            {"start": "18:00", "end": "24:00"},
        ],
        "weekend_all_day": True,
        "input": 1.0,
        "cached": 0.1,
        "output": 4.0,
    }
    at_lunch = datetime(2026, 1, 1, 13, 0).astimezone()
    at_afternoon = datetime(2026, 1, 1, 15, 0).astimezone()
    friday_night = datetime(2026, 1, 2, 23, 30).astimezone()
    saturday = datetime(2026, 1, 3, 15, 0).astimezone()
    checks.append(("multi-window idle", _idle_active(multi_idle, at_lunch)))
    checks.append((
        "next idle window",
        _idle_transition(multi_idle, at_afternoon)["seconds"] == 3 * 3600,
    ))
    checks.append((
        "weekend all day",
        _idle_active(multi_idle, saturday)
        and _idle_transition(multi_idle, saturday)["to"] == "standard",
    ))
    checks.append((
        "weekend merges into Monday",
        _idle_transition(multi_idle, friday_night)["seconds"] == 48 * 3600 + 30 * 60,
    ))
    checks.append((
        "idle pricing can be disabled",
        not _idle_active(dict(multi_idle, enabled=False), at_lunch)
        and _idle_transition(dict(multi_idle, enabled=False), at_lunch) is None,
    ))
    checks.append((
        "midnight end time",
        _idle_active({"windows": [{"start": "18:00", "end": "00:00"}]},
                     datetime(2026, 1, 1, 19, 0).astimezone())
        and not _idle_active({"windows": [{"start": "18:00", "end": "00:00"}]},
                             datetime(2026, 1, 2, 0, 30).astimezone()),
    ))
    official_config = {
        "multiplier": 1.0,
        "models": {},
        "provider": "dashscope",
        "official": {
            "qwen3.8-max": {
                "input": 12.0,
                "cached": 1.2,
                "output": 36.0,
                "idle": {
                    "windows": [{"start": "22:00", "end": "08:00"}],
                    "weekend_all_day": False,
                    "enabled": True,
                    "input": 6.0,
                    "cached": 0.6,
                    "output": 18.0,
                },
            }
        },
    }
    checks.append((
        "official price",
        _estimate_cost(idle_usage, "qwen3.8-max", official_config, at_day)
        == (13.44, "official"),
    ))
    checks.append((
        "official off-peak price",
        _estimate_cost(idle_usage, "qwen3.8-max", official_config, at_idle)
        == (6.72, "idle"),
    ))
    # 这些用例只验证配置本身，隔离本机 Codex provider 的自动跟随。
    real_codex_provider = globals()["_codex_provider"]
    globals()["_codex_provider"] = lambda: {}
    ark_pricing = _price_config({"pricing": {"provider": "ark"}, "relay": {"base_url": ""}})
    custom_pricing = _price_config({
        "pricing": {
            "provider": "custom",
            "models": {"custom-model": {"input": 1.0, "cached": 0.1, "output": 2.0}},
        },
        "relay": {"base_url": ""},
    })
    profile_pricing = _price_config({
        "relay": {"base_url": "https://api.example.com/v1"},
        "pricing": {
            "provider": "custom",
            "profile": "api.example.com",
            "profiles": {
                "api.example.com": {
                    "name": "示例API",
                    "multiplier": 0.06,
                    "models": {"profile-model": {"input": 3.0, "cached": 0.3, "output": 6.0}},
                },
                "api.other.org": {
                    "name": "其他API",
                    "models": {"profile-model": {"input": 9.0, "cached": 0.9, "output": 18.0}},
                },
            },
        },
    })
    host_profile = _price_config({
        "relay": {"base_url": "https://api.example.com/v1"},
        "pricing": {
            "provider": "custom",
            "profiles": {
                "api.example.com": {
                    "name": "示例API",
                    "models": {"host-model": {"input": 4.0, "cached": 0.4, "output": 7.0}},
                },
            },
        },
    })
    globals()["_codex_provider"] = real_codex_provider

    ark_idle = (ark_pricing["official"].get("deepseek-v4.1-flash-1017") or {}).get("idle") or {}
    checks.append((
        "ark idle auto schedule",
        ark_idle.get("enabled") is True and len(ark_idle.get("windows") or []) == 3,
    ))
    custom_price = _price_for("custom-model", custom_pricing, at_day)
    checks.append((
        "custom model price",
        custom_pricing["provider"] == "custom"
        and custom_price == ({"input": 1.0, "cached": 0.1, "output": 2.0}, "configured"),
    ))
    checks.append((
        "custom API profile",
        profile_pricing["profile"].get("name") == "示例API"
        and profile_pricing["multiplier"] == 0.06
        and _price_for("profile-model", profile_pricing) == (
            {"input": 3.0, "cached": 0.3, "output": 6.0}, "configured"
        ),
    ))
    checks.append((
        "custom API domain profile",
        host_profile["profile"].get("name") == "示例API"
        and _price_for("host-model", host_profile)[1] == "configured",
    ))
    tiered = {
        "tiers": [
            {"upto": 32_000, "input": 3.2, "cached": 0.64, "output": 16.0},
            {"upto": 128_000, "input": 4.8, "cached": 0.96, "output": 24.0},
        ],
        "input": 3.2,
        "cached": 0.64,
        "output": 16.0,
    }
    checks.append((
        "token tier",
        _price_for("tiered", {"official": {"tiered": tiered}}, at_day, 100_000)[0]["input"] == 4.8
        and _price_for("tiered", {"official": {"tiered": tiered}}, at_day, 10_000)[0]["input"] == 3.2,
    ))
    holiday_monday = datetime(2026, 1, 5, 15, 0).astimezone()
    holiday_idle = dict(multi_idle, days=["2026-01-05"])
    checks.append((
        "holiday off-peak",
        _idle_active(holiday_idle, holiday_monday)
        and not _idle_active(multi_idle, holiday_monday),
    ))
    checks.append((
        "price cell parse",
        pricing_catalog._prices("忙时 2 元 闲时 1 元") == (2.0, 1.0),
    ))
    checks.append((
        "relay provider hint",
        pricing_catalog.detect_hint("https://maas.qianwenaiapi.com/v1") == "dashscope",
    ))
    checks.append((
        "relay hint no false ark",
        pricing_catalog.detect_hint("https://api.market.example/v1") == "",
    ))
    ali_page = (
        "<table><tr><th>模型 ID</th><th>输入</th><th>输出</th></tr>"
        "<tr><td>cache-less-model</td><td>2元</td><td>8元</td></tr></table>"
    )
    cache_less = pricing_catalog.parse_ali(ali_page).get("cache-less-model") or {}
    checks.append((
        "estimated cache price",
        cache_less.get("cached") == 0.2 and cache_less.get("input") == 2.0,
    ))
    checks.append((
        "tier limit parse",
        pricing_catalog._upto("输入长度 (32, 128]", 1000) == 128_000,
    ))

    ledger = _new_file_entry()
    records = [
        {"type": "session_meta", "timestamp": "2026-01-01T00:00:00Z",
         "payload": {"id": "thread-1", "model": "priced-model"}},
        {"type": "token_usage_record", "timestamp": "2026-01-01T00:01:00Z",
         "payload": {"turn_id": "turn-1", "usage": {"total_tokens": 100}}},
        {"type": "token_usage_record", "timestamp": "2026-01-01T00:02:00Z",
         "payload": {"turn_id": "turn-1", "usage": {"total_tokens": 300}}},
    ]
    _consume_session_bytes(
        ledger,
        "".join(json.dumps(item) + "\n" for item in records).encode("utf-8"),
    )
    turn = ledger["turns"].get("turn-1") or {}
    checks.append((
        "cumulative token snapshot",
        len(ledger["turns"]) == 1 and (turn.get("usage") or {}).get("total_tokens") == 300,
    ))
    settings_record = {
        "type": "event_msg",
        "timestamp": "2026-01-01T00:03:00Z",
        "payload": {
            "type": "thread_settings_applied",
            "thread_settings": {"model": "switched-model"},
        },
    }
    _consume_session_bytes(
        ledger,
        (json.dumps(settings_record) + "\n").encode("utf-8"),
    )
    switched_thread = {"updated_at": "2026-01-01T00:02:00+00:00", "model": "priced-model"}
    _apply_model_change(switched_thread, ledger)
    checks.append((
        "model switch applies before usage",
        ledger["last_model"] == "switched-model"
        and switched_thread["model"] == "switched-model",
    ))

    failed = 0
    for name, okay in checks:
        print(("PASS  " if okay else "FAIL  ") + name)
        failed += 0 if okay else 1
    print("%d/%d passed" % (len(checks) - failed, len(checks)))
    return 0 if failed == 0 else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description="API balance MCP server")
    parser.add_argument(
        "cmd", nargs="?", default="serve",
        choices=["serve", "balance", "aliyun", "coupons", "diagnose", "doctor",
                 "widget-open", "widget-close", "widget-status", "selftest"],
    )
    parser.add_argument("--status", default="Available")
    args = parser.parse_args(argv)
    pricing_catalog.ensure_fresh()

    if args.cmd == "selftest":
        return selftest()
    if args.cmd == "serve":
        return serve()

    try:
        if args.cmd == "balance":
            print(report_balance()[0])
        elif args.cmd == "aliyun":
            print(json.dumps(aliyun_balance(), ensure_ascii=False, indent=2))
        elif args.cmd == "coupons":
            print(json.dumps(aliyun_coupons(args.status), ensure_ascii=False, indent=2))
        elif args.cmd == "diagnose":
            cfg = relay_config()
            print(json.dumps(probe_relay(cfg)[1] if cfg else "no relay config",
                             ensure_ascii=False, indent=2))
        elif args.cmd == "doctor":
            cfg = relay_config()
            aliyun = aliyun_config()
            print("python      :", sys.version.split()[0])
            print("relay       :", "configured" if cfg else "MISSING")
            if cfg:
                key = cfg["api_key"]
                print("  base_url  :", cfg["base_url"])
                print("  key       :", key[:8] + "..." + key[-4:])
                print("  cached    :", _read_cache().get("endpoint") or "-")
            print("aliyun      :", "configured" if aliyun else "not configured (optional)")
            print("widget      :", "running" if widget_running() else "stopped")
            print("config file :", os.path.join(HERE, "config.json"))
        elif args.cmd == "widget-open":
            print(open_widget()[1])
        elif args.cmd == "widget-close":
            print(close_widget()[1])
        elif args.cmd == "widget-status":
            running, state = widget_state()
            print("running" if running else "stopped")
            print(json.dumps(state, ensure_ascii=False, indent=2))
    except BalanceError as exc:
        print("错误：%s" % exc.message, file=sys.stderr)
        print(json.dumps(exc.as_dict(), ensure_ascii=False, indent=2), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
