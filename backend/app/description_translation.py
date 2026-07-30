from __future__ import annotations

import asyncio
import json
import re
from typing import Any

import httpx
from sqlalchemy.orm import Session

from .errors import AppError
from .models import Anime, AppSetting

TRANSLATION_REFUSAL_PATTERNS = (
    re.compile(r"^\s*(?:抱歉|对不起)[，,。\s]*(?:我)?(?:无法|不能)(?:协助)?翻译"),
    re.compile(
        r"^\s*(?:sorry[,\s]+)?(?:i\s+(?:cannot|can't|am unable to)|i'm unable to)"
        r"\s+(?:help\s+)?translate",
        re.IGNORECASE,
    ),
)


def _setting(db: Session, key: str, default: Any = None) -> Any:
    row = db.get(AppSetting, key)
    return row.value if row else default


def _response_text(payload: Any) -> str:
    try:
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise AppError(
            "TRANSLATION_INVALID_RESPONSE",
            "翻译服务返回了无法识别的响应",
            details=str(exc),
            retryable=True,
            status_code=502,
        ) from exc
    if not isinstance(content, str) or not content.strip():
        raise AppError(
            "TRANSLATION_EMPTY_RESPONSE",
            "翻译服务未返回有效简介",
            retryable=True,
            status_code=502,
        )
    text = content.strip()
    if text.startswith("```") and text.endswith("```"):
        lines = text.splitlines()
        text = "\n".join(lines[1:-1]).strip()
    return text


def _validate_translation_text(text: str) -> str:
    """拒绝说明不是译文，调用方收到错误后必须保留抓取到的原简介。"""
    normalized = text.strip()
    if len(normalized) <= 500 and any(
        pattern.search(normalized) for pattern in TRANSLATION_REFUSAL_PATTERNS
    ):
        raise AppError(
            "TRANSLATION_REFUSED",
            "翻译服务拒绝了该内容，已保留原简介",
            retryable=False,
            status_code=422,
        )
    return normalized


def auto_translation_enabled(db: Session) -> bool:
    return bool(_setting(db, "auto_translate_description", False))


def description_needs_translation(anime: Anime) -> bool:
    """判断抓取简介是否需要自动翻译；人工或已翻译字段始终保留。"""
    description = (anime.description or "").strip()
    if not description or "description" in (anime.manual_fields or []):
        return False
    if (anime.field_provenance or {}).get("description") == "translation":
        return False
    # 包含汉字且没有日文假名时，按已有中文简介处理，避免重复计费。
    has_han = bool(re.search(r"[\u3400-\u9fff]", description))
    has_kana = bool(re.search(r"[\u3040-\u30ff]", description))
    return not (has_han and not has_kana)


def apply_description_translation(anime: Anime, translated: str) -> None:
    anime.description = translated
    manual = set(anime.manual_fields or [])
    manual.add("description")
    anime.manual_fields = sorted(manual)
    provenance = dict(anime.field_provenance or {})
    provenance["description"] = "translation"
    anime.field_provenance = provenance


async def auto_translate_anime_description(db: Session, anime: Anime) -> dict[str, Any]:
    if not auto_translation_enabled(db):
        return {"status": "disabled"}
    if not description_needs_translation(anime):
        return {"status": "skipped"}
    try:
        translated = await translate_description_text(db, anime.description or "")
        apply_description_translation(anime, translated)
        return {"status": "translated"}
    except Exception as exc:
        return {
            "status": "failed",
            "code": getattr(exc, "code", "TRANSLATION_FAILED"),
            "message": getattr(exc, "message", str(exc)),
        }


async def _translate_openai(db: Session, description: str) -> str:
    api_key = str(_setting(db, "translation_api_key", "") or "").strip()
    model = str(_setting(db, "translation_model", "") or "").strip()
    base_url = str(_setting(db, "translation_base_url", "") or "").strip().rstrip("/")
    timeout = float(_setting(db, "translation_timeout_seconds", 60) or 60)
    proxy = str(_setting(db, "proxy_url", "") or "").strip()

    missing = [
        label
        for label, value in (("API Key", api_key), ("模型", model), ("Base URL", base_url))
        if not value
    ]
    if missing:
        raise AppError(
            "TRANSLATION_NOT_CONFIGURED",
            f"请先在设置页配置简介翻译的{'、'.join(missing)}",
        )

    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "你是动漫元数据翻译助手。请把用户提供的作品简介翻译为自然、准确的简体中文，"
                    "保留人名、作品名和原有段落，不要补充、删减或解释内容，只输出译文。"
                ),
            },
            {"role": "user", "content": description},
        ],
        "stream": False,
    }
    # 与 nas-tool 的字幕翻译行为一致，关闭 Qwen 思考输出以获得纯译文。
    if "qwen" in model.lower():
        payload["enable_thinking"] = False

    try:
        async with httpx.AsyncClient(
            proxy=proxy or None,
            timeout=timeout,
            follow_redirects=True,
        ) as client:
            response = await client.post(
                f"{base_url}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json=payload,
            )
            response.raise_for_status()
            return _response_text(response.json())
    except AppError:
        raise
    except (httpx.HTTPError, ValueError) as exc:
        raise AppError(
            "TRANSLATION_REQUEST_FAILED",
            "简介翻译请求失败",
            details=str(exc),
            retryable=True,
            status_code=502,
        ) from exc


def _translate_tmt_sync(
    description: str,
    secret_id: str,
    secret_key: str,
    region: str,
) -> str:
    # 延迟导入 SDK，避免未选择 TMT 时增加应用启动开销。
    from tencentcloud.common import credential
    from tencentcloud.common.profile.client_profile import ClientProfile
    from tencentcloud.common.profile.http_profile import HttpProfile
    from tencentcloud.tmt.v20180321 import models, tmt_client

    cred = credential.Credential(secret_id, secret_key)
    http_profile = HttpProfile()
    http_profile.endpoint = "tmt.tencentcloudapi.com"
    client_profile = ClientProfile()
    client_profile.httpProfile = http_profile
    client = tmt_client.TmtClient(cred, region, client_profile)

    request = models.TextTranslateRequest()
    request.from_json_string(
        json.dumps(
            {
                "SourceText": description,
                "Source": "auto",
                "Target": "zh",
                "ProjectId": 0,
            },
            ensure_ascii=False,
        )
    )
    response = client.TextTranslate(request)
    translated = getattr(response, "TargetText", "")
    if not isinstance(translated, str) or not translated.strip():
        raise AppError(
            "TRANSLATION_EMPTY_RESPONSE",
            "腾讯云 TMT 未返回有效简介",
            retryable=True,
            status_code=502,
        )
    return translated.strip()


async def _translate_tmt(db: Session, description: str) -> str:
    secret_id = str(_setting(db, "tmt_secret_id", "") or "").strip()
    secret_key = str(_setting(db, "tmt_secret_key", "") or "").strip()
    region = str(_setting(db, "tmt_region", "ap-guangzhou") or "").strip()
    missing = [
        label
        for label, value in (("SecretId", secret_id), ("SecretKey", secret_key), ("地域", region))
        if not value
    ]
    if missing:
        raise AppError(
            "TRANSLATION_NOT_CONFIGURED",
            f"请先在设置页配置腾讯云 TMT 的{'、'.join(missing)}",
        )
    try:
        return await asyncio.to_thread(
            _translate_tmt_sync,
            description,
            secret_id,
            secret_key,
            region,
        )
    except AppError:
        raise
    except Exception as exc:
        raise AppError(
            "TRANSLATION_REQUEST_FAILED",
            "腾讯云 TMT 简介翻译请求失败",
            details=str(exc),
            retryable=True,
            status_code=502,
        ) from exc


async def translate_description_text(db: Session, description: str) -> str:
    """使用设置页选择的 OpenAI 兼容接口或腾讯云 TMT 翻译作品简介。"""
    provider = str(_setting(db, "translation_provider", "openai") or "openai")
    if provider == "openai":
        translated = await _translate_openai(db, description)
    elif provider == "tmt":
        translated = await _translate_tmt(db, description)
    else:
        raise AppError("UNKNOWN_TRANSLATION_PROVIDER", f"不支持的简介翻译服务: {provider}")
    return _validate_translation_text(translated)
