"""官方模型价格目录。

从阿里云百炼、DeepSeek、火山方舟的公开价格页解析单价与忙闲时段，
缓存到 pricing_cache.json；解析失败时保留上一份快照。
"""
from __future__ import annotations

import html
import json
import os
import re
import threading
import time
import urllib.request
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "pricing_cache.json")
TTL = 12 * 3600

PROVIDERS = {
    "dashscope": {
        "label": "阿里云百炼",
        "hosts": ["dashscope.aliyuncs.com", "dashscope-intl.aliyuncs.com"],
        "suffixes": [".maas.aliyuncs.com"],
        "source": "https://help.aliyun.com/zh/model-studio/model-pricing",
        "schedule": "忙时 08:00-22:00，闲时 22:00-08:00",
        "idle": {"windows": [{"start": "22:00", "end": "08:00"}], "weekend_all_day": False},
    },
    "deepseek": {
        "label": "DeepSeek",
        "hosts": ["api.deepseek.com"],
        "suffixes": [],
        "source": "https://api-docs.deepseek.com/zh-cn/quick_start/pricing",
        "schedule": "工作日 09:00-12:00、14:00-18:00 高峰，其余闲时",
        "idle": {
            "windows": [
                {"start": "00:00", "end": "09:00"},
                {"start": "12:00", "end": "14:00"},
                {"start": "18:00", "end": "24:00"},
            ],
            "weekend_all_day": True,
            "holidays": True,
        },
    },
    "ark": {
        "label": "火山方舟",
        "hosts": ["ark.cn-beijing.volces.com", "ark.cn-shanghai.volces.com"],
        "suffixes": [".volces.com", ".volcengineapi.com"],
        "source": "https://www.volcengine.com/docs/82379/1099320",
        "schedule": "工作日 09:00-12:00、14:00-18:00 高峰，其余闲时",
        "idle": {
            "windows": [
                {"start": "00:00", "end": "09:00"},
                {"start": "12:00", "end": "14:00"},
                {"start": "18:00", "end": "24:00"},
            ],
            "weekend_all_day": True,
        },
    },
    "custom": {
        "label": "自定义",
        "hosts": [],
        "suffixes": [],
        "source": "",
        "schedule": "",
        "idle": {"windows": [], "weekend_all_day": False},
    },
}


def log(message):
    print("[pricing] %s" % message)


def _fetch(url, timeout=12):
    request = urllib.request.Request(url, headers={
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
        "Accept-Language": "zh-CN,zh;q=0.9",
    })
    with urllib.request.urlopen(request, timeout=timeout) as response:
        page = response.read().decode("utf-8", "replace")
    if "_____tmd_____" in page:  # 官网反爬验证页
        raise OSError("官网限速，稍后重试")
    return page


def _text(fragment):
    value = re.sub(r"(?is)<[^>]+>", " ", fragment)
    return " ".join(html.unescape(value).replace("\u200b", " ").split())


def _tables(page):
    result = []
    for block in re.findall(r"(?is)<table\b.*?</table>", page):
        rows = []
        for row in re.findall(r"(?is)<tr\b.*?</tr>", block):
            cells = [_text(cell) for cell in re.findall(r"(?is)<t[dh]\b[^>]*>(.*?)</t[dh]>", row)]
            if cells:
                rows.append(cells)
        if rows:
            result.append(rows)
    return result


def _money(value):
    try:
        number = float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None
    return number if 0 <= number < 10000 else None


def _first_number(text):
    found = re.search(r"(\d+(?:\.\d+)?)", text or "")
    return _money(found.group(1)) if found else None


def _prices(cell):
    """'忙时 2 元 闲时 1 元' -> (2.0, 1.0)；'12 元' -> (12.0, None)。"""
    peak = re.search(r"忙时\s*([\d.]+)|高峰时段\s*([\d.]+)", cell)
    idle = re.search(r"闲时\s*([\d.]+)|空闲时段\s*([\d.]+)", cell)
    if peak or idle:
        peak_value = _money((peak.group(1) or peak.group(2)) if peak else None)
        idle_value = _money((idle.group(1) or idle.group(2)) if idle else None)
        return peak_value, idle_value
    return _first_number(cell), None


def _cache_rate(cell):
    return 0.1 if "上下文缓存" in cell else None


def _upto(text, unit=1000):
    """'输入长度 (32, 128]' -> 128000；'0<Token≤1M' -> 1000000。"""
    value = text or ""
    found = re.findall(r"(\d+(?:\.\d+)?)\s*([KMkm万]?)", value)
    if not found:
        return None
    number, suffix = found[-1]
    scale = {"K": 1000, "k": 1000, "M": 1000000, "m": 1000000, "万": 10000, "": unit}[suffix]
    return int(float(number) * scale)


def _entry(provider, peak, idle, model_id, note=""):
    entry = {
        "provider": provider,
        "input": peak[0],
        "cached": peak[1],
        "output": peak[2],
    }
    if idle and all(value is not None for value in idle):
        schedule = dict(PROVIDERS[provider]["idle"])
        schedule.update({"enabled": True, "input": idle[0], "cached": idle[1], "output": idle[2]})
        entry["idle"] = schedule
    if note:
        entry["note"] = note
    return entry


def _finish(provider, local, found):
    """同一张表内重复的模型行按阶梯合并；先出现的表优先（对应北京/常规推理）。"""
    for model_id, slot in local.items():
        if model_id in found:
            continue
        tiers = slot["tiers"]
        base = slot["peak"] or (tiers[0][1] if tiers else None)
        if base is None:
            continue
        entry = _entry(provider, base, slot["idle"], model_id)
        if len(tiers) > 1:
            entry["tiers"] = [
                {"upto": limit, "input": values[0], "cached": values[1], "output": values[2]}
                for limit, values, _flag in tiers
            ]
        if tiers and tiers[0][2]:
            entry["note"] = "缓存价按输入价 10% 估算"
        found[model_id] = entry


def parse_ali(page):
    """百炼表格列常有合并单元格，按“元”价格列位置取输入/输出；缓存列只有表头声明时才算数。"""
    found = {}
    for table in _tables(page):
        if not any("模型 ID" in " ".join(row) for row in table[:3]):
            continue
        header = next(row for row in table[:3] if any("模型 ID" in cell for cell in row))
        has_cache = any("缓存" in cell for cell in header)
        local = {}
        for row in table:
            model_id = row[0].split(" ")[0]
            if not re.fullmatch(r"[a-z0-9][a-z0-9._:-]{2,}", model_id):
                continue
            cells = [cell for cell in row[1:] if "元" in cell and re.search(r"\d", cell)]
            if len(cells) < 2:
                continue
            peak_in, idle_in = _prices(cells[0])
            peak_out, idle_out = _prices(cells[-1])
            if peak_in is None or peak_out is None:
                continue
            estimated = not has_cache
            if estimated:
                rate = _cache_rate(row[0]) or 0.1
                peak_cached = round(peak_in * rate, 6) if rate else None
                idle_cached = round(idle_in * rate, 6) if (rate and idle_in is not None) else None
            else:
                peak_cached, idle_cached = _prices(cells[1])
            slot = local.setdefault(model_id, {"peak": None, "idle": None, "tiers": []})
            limit = next((_upto(cell, 1) for cell in row if re.search(r"Token\s*[≤<]", cell)), None)
            slot["tiers"].append((limit, (peak_in, peak_cached, peak_out), estimated))
            if idle_in is not None and idle_out is not None:
                slot["idle"] = (idle_in, idle_cached, idle_out)
        _finish("dashscope", local, found)
    for model_id, cached in ali_cache_prices(page).items():
        entry = found.get(model_id)
        if not entry:
            continue
        entry["cached"] = cached
        if isinstance(entry.get("idle"), dict):
            entry["idle"]["cached"] = cached
        entry.pop("note", None)
    return found


def parse_ark(page):
    found = {}
    for table in _tables(page):
        header = None
        for row in table[:3]:
            if any("非音频" in cell and "输入" in cell for cell in row):
                header = row
                break
        if header is None:
            continue
        columns = {}
        for idx, cell in enumerate(header):
            if "非音频" in cell and "输入" in cell:
                columns.setdefault("input", idx)
            elif "非音频" in cell and "缓存" in cell:
                columns.setdefault("cached", idx)
            elif "输出" in cell:
                columns.setdefault("output", idx)
        if set(columns) != {"input", "cached", "output"}:
            continue
        local = {}
        last_model = ""
        for row in table[table.index(header) + 1:]:
            model_id = row[0].split(" ")[0]
            stage = row[1] if len(row) > 1 else ""
            if any(word in " ".join(row[:2]) for word in ("经济版", "预览版", "正式版")):
                last_model = ""
                continue
            if re.fullmatch(r"[a-z0-9][a-z0-9._:-]{2,}", model_id):
                last_model = model_id
            elif model_id in ("空闲时段", "高峰时段"):
                stage, model_id = model_id, last_model
            elif row[0] == "" and last_model:
                model_id = last_model
            else:
                if row[0]:
                    last_model = ""
                continue
            shift = len(row) - len(header)
            indexes = [columns[key] + shift for key in ("input", "cached", "output")]
            if min(indexes) < 1:
                continue
            values = tuple(_money(row[idx]) if idx < len(row) else None for idx in indexes)
            if values[0] is None or values[2] is None:
                continue
            slot = local.setdefault(model_id, {"peak": None, "idle": None, "tiers": []})
            if stage in ("空闲时段", "高峰时段"):
                slot["idle" if stage == "空闲时段" else "peak"] = values
                continue
            slot["tiers"].append((_upto(stage, 1000), values, False))
        _finish("ark", local, found)
    return found


def parse_deepseek(page):
    found = {}
    for table in _tables(page):
        header = next((row for row in table if any(c.startswith("deepseek-") for c in row)), None)
        if not header:
            continue
        models = [cell.split(" ")[0] for cell in header if cell.startswith("deepseek-")]
        values = {model: {} for model in models}
        metric = None
        for row in table:
            joined = " ".join(row)
            if "百万tokens输入" in joined:
                metric = "cached" if "缓存命中" in joined else "input"
            elif "百万tokens输出" in joined:
                metric = "output"
            stage = "idle" if "空闲" in joined else ("peak" if "高峰" in joined else None)
            if not (metric and stage):
                continue
            numbers = [_money(value) for value in re.findall(r"([\d.]+)\s*元", joined)]
            numbers = [value for value in numbers if value is not None]
            if len(numbers) != len(models):
                continue
            for model, value in zip(models, numbers):
                values[model]["%s.%s" % (stage, metric)] = value
        for model, data in values.items():
            peak = tuple(data.get("peak." + key) for key in ("input", "cached", "output"))
            idle = tuple(data.get("idle." + key) for key in ("input", "cached", "output"))
            if None in peak:
                continue
            found[model] = _entry("deepseek", peak, None if None in idle else idle, model)
    return found


PARSERS = {
    "dashscope": parse_ali,
    "deepseek": parse_deepseek,
    "ark": parse_ark,
}

# 百炼总价格表多数模型只有输入/输出两列，缓存命中价要去各自的模型文档页取
DASH_SCOPE_DOC = "https://help.aliyun.com/zh/model-studio/"
_CACHE_SECTION = re.compile(
    r"(?is)<p><strong>\s*([^<]+?)\s*—\s*缓存单价（每百万\s*Token）\s*</strong></p>\s*<table\b.*?</table>")


def ali_cache_prices(page):
    """总价格页里 “xxx — 缓存单价” 小表：模型 -> 缓存命中单价。"""
    prices = {}
    for match in _CACHE_SECTION.finditer(page):
        tables = _tables(match.group(0))
        if not tables or len(tables[0]) < 2:
            continue
        header, body = tables[0][0], tables[0][1]
        idx = next((i for i, cell in enumerate(header) if "缓存命中" in cell), None)
        if idx is None:
            continue
        value = _first_number(body[idx] if idx < len(body) else "")
        if value is not None:
            prices[match.group(1).strip()] = value
    return prices


def _doc_prices(page):
    """模型文档首张价格表（中国内地）的输入/缓存命中价，忙闲分档时取忙时。"""
    for table in _tables(page):
        rows = {row[0]: row[1] for row in table if len(row) > 1 and row[0]}
        peak = _money(rows.get("输入")) or _money(rows.get("输入（忙时）"))
        cached = (_money(rows.get("输入（缓存命中）"))
                  or _money(rows.get("输入（缓存命中，忙时）"))
                  or _money(rows.get("显式缓存命中")))
        if cached is None:
            continue
        return peak, cached, _money(rows.get("输入（缓存命中，闲时）"))
    return None, None, None


def _doc_slugs(model_id):
    """模型 id -> 文档路径候选；带版本的去掉厂商前缀/日期再试。"""
    base = model_id.replace(".", "-").lower()
    names = [base.replace("/", "-"), base.split("/")[-1]]
    for name in list(names):
        names.append(re.sub(r"-(?:\d{8}|\d{4}|\d{3,4})$", "", name))
    return list(dict.fromkeys(name for name in names if name))


def _apply_resolved(parsed, resolved):
    for model_id, entry in parsed.items():
        value = resolved.get(model_id)
        if not value or not entry.get("note"):
            continue
        entry["cached"] = value[0]
        if isinstance(entry.get("idle"), dict) and value[1] is not None:
            entry["idle"]["cached"] = value[1]
        entry.pop("note", None)


_PENDING = set()
_FAILED = {}
RETRY_AFTER = 12 * 3600
_DOC_LOCK = threading.Lock()


def resolve_async(model_id, input_price):
    """按需解析单个模型的缓存命中价，串行请求，结果写进 pricing_cache.json。"""
    with _LOCK:
        if model_id in _PENDING or time.time() - _FAILED.get(model_id, 0) < RETRY_AFTER:
            return
        _PENDING.add(model_id)

    def run():
        found = False
        try:
            with _DOC_LOCK:
                for slug in _doc_slugs(model_id):
                    try:
                        peak, cached, idle = _doc_prices(_fetch(DASH_SCOPE_DOC + slug, timeout=10))
                    except Exception:  # noqa: BLE001
                        continue
                    if cached is None or (peak is not None and abs(peak - input_price) > 1e-6):
                        continue
                    _store(model_id, cached, idle)
                    found = True
                    break
                time.sleep(2)  # 同一站点连续请求容易触发反爬
        finally:
            with _LOCK:
                _PENDING.discard(model_id)
                if found:
                    _FAILED.pop(model_id, None)
                else:
                    _FAILED[model_id] = time.time()

    threading.Thread(target=run, daemon=True).start()


def _store(model_id, cached, idle_cached):
    data = load()
    prices = dict(data.get("cache_prices") or {})
    prices[model_id] = [cached, idle_cached]
    data["cache_prices"] = prices
    entry = (data.get("models") or {}).get("dashscope:" + model_id)
    if entry:
        entry["cached"] = cached
        if isinstance(entry.get("idle"), dict):
            entry["idle"]["cached"] = cached if idle_cached is None else idle_cached
        entry.pop("note", None)
    _save(data)


# 同一模型在不同平台的命名差异
ALIASES = {
    "deepseek": {
        "deepseek-v4.1-flash-1017": "deepseek-flash",
        "deepseek-v4-flash-1017": "deepseek-flash",
        "deepseek-v4.1-flash": "deepseek-flash",
        "deepseek-v4-flash": "deepseek-flash",
    },
    "ark": {
        "deepseek-v4.1-flash-1017": "deepseek-v4-1-flash",
        "deepseek-v4.1-flash": "deepseek-v4-1-flash",
    },
}


def _holidays():
    year = datetime.now().year
    try:
        payload = json.loads(_fetch("https://timor.tech/api/holiday/year/%d" % year, timeout=8))
        days = payload.get("holiday") or {}
        return sorted("%d-%s" % (year, key) for key, item in days.items() if item.get("holiday"))
    except Exception as exc:  # noqa: BLE001
        log("holidays unavailable: %s" % exc)
        return []


def refresh():
    catalog = load()
    models = dict(catalog.get("models") or {})
    resolved = dict(catalog.get("cache_prices") or {})
    error = []
    for key, provider in PROVIDERS.items():
        if key not in PARSERS:
            continue
        try:
            parsed = PARSERS[key](_fetch(provider["source"]))
        except Exception as exc:  # noqa: BLE001
            parsed, reason = {}, str(exc)
            error.append("%s: %s" % (provider["label"], reason))
        if len(parsed) < 2:
            error.append("%s: 解析不足，保留旧快照" % provider["label"])
            continue
        if key == "dashscope":
            _apply_resolved(parsed, resolved)
        for model_id, entry in parsed.items():
            models["%s:%s" % (key, model_id)] = entry
    holidays = _holidays()
    if holidays:
        for key, entry in models.items():
            idle = entry.get("idle")
            if entry.get("provider") == "deepseek" and idle and idle.get("holidays"):
                idle["days"] = holidays
    catalog = {
        "fetched_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "providers": {key: {k: v for k, v in provider.items()
                            if k in ("label", "source", "schedule")}
                      for key, provider in PROVIDERS.items()},
        "models": models,
        "cache_prices": resolved,
        "error": error,
    }
    _save(catalog)
    return catalog


def _save(catalog):
    temp = CACHE + ".tmp"
    with open(temp, "w", encoding="utf-8") as handle:
        json.dump(catalog, handle, ensure_ascii=False, indent=1)
    os.replace(temp, CACHE)


def load():
    try:
        with open(CACHE, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {}


def stale(catalog=None):
    data = catalog if catalog is not None else load()
    if not data.get("models"):
        return True
    try:
        fetched = datetime.fromisoformat(data.get("fetched_at") or "")
    except ValueError:
        return True
    return (datetime.now().astimezone() - fetched).total_seconds() > TTL


_LOCK = threading.Lock()
_STATE = {"busy": False, "at": 0.0, "error": []}


def sync_state():
    with _LOCK:
        return {
            "busy": bool(_STATE["busy"]),
            "at": float(_STATE["at"] or 0.0),
            "error": list(_STATE["error"] or []),
        }


def ensure_fresh(force=False):
    """后台同步一次，不阻塞调用方。"""
    with _LOCK:
        if _STATE["busy"]:
            return False
        if not force and not stale():
            return False
        _STATE["busy"] = True

    def run():
        error = []
        try:
            error = refresh().get("error") or []
        except Exception as exc:  # noqa: BLE001
            log("refresh failed: %s" % exc)
            error = [str(exc)]
        finally:
            with _LOCK:
                _STATE["busy"] = False
                _STATE["at"] = time.time()
                _STATE["error"] = error

    threading.Thread(target=run, name="pricing-refresh", daemon=True).start()
    return True


def detect(base_url):
    host = (base_url or "").split("//")[-1].split("/")[0].split(":")[0].lower()
    if not host:
        return ""
    for key, provider in PROVIDERS.items():
        if host in provider["hosts"] or any(host.endswith(suffix) for suffix in provider["suffixes"]):
            return key
    return ""


def detect_hint(base_url):
    """中转站域名不可信，只做名称提示；无把握时返回空。"""
    host = (base_url or "").split("//")[-1].split("/")[0].split(":")[0].lower()
    if not host:
        return ""
    if any(token in host for token in ("dashscope", "aliyun", "bailian", "qianwen")):
        return "dashscope"
    if "deepseek" in host:
        return "deepseek"
    first = host.split(".")[0]
    if "volces" in host or "volcengine" in host or first == "ark" or first.startswith("ark-"):
        return "ark"
    return ""


def models_for(catalog, provider):
    prefix = provider + ":"
    models = {
        key[len(prefix):]: entry
        for key, entry in (catalog.get("models") or {}).items()
        if key.startswith(prefix)
    }
    for alias, target in (ALIASES.get(provider) or {}).items():
        if target in models and alias not in models:
            models[alias] = models[target]
    return models


if __name__ == "__main__":
    started = time.time()
    data = refresh()
    counts = {}
    for key in data["models"]:
        provider = key.split(":")[0]
        counts[provider] = counts.get(provider, 0) + 1
    print("models:", counts, "耗时 %.1fs" % (time.time() - started))
    for key in ("dashscope:deepseek-v4.1-flash-1017", "deepseek:deepseek-flash",
                "ark:doubao-seed-2.1-lite", "ark:deepseek-v4-1-flash"):
        entry = data["models"].get(key)
        print(key, "->", json.dumps(entry, ensure_ascii=False) if entry else "MISSING")
    if data["error"]:
        print("errors:", data["error"])
