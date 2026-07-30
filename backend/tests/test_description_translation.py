import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app import description_translation
from app.database import Base
from app.description_translation import (
    _response_text,
    _validate_translation_text,
    auto_translate_anime_description,
    translate_description_text,
)
from app.errors import AppError
from app.models import Anime, AppSetting
from app.routers import anime as anime_router


def test_response_text_extracts_plain_and_fenced_content() -> None:
    assert _response_text({"choices": [{"message": {"content": " 中文简介 "}}]}) == "中文简介"
    assert (
        _response_text({"choices": [{"message": {"content": "```text\n中文简介\n```"}}]})
        == "中文简介"
    )


def test_response_text_rejects_invalid_payload() -> None:
    with pytest.raises(AppError) as caught:
        _response_text({"choices": []})

    assert caught.value.code == "TRANSLATION_INVALID_RESPONSE"


@pytest.mark.parametrize(
    "text",
    [
        "抱歉，我无法翻译涉及可能未成年角色的露骨性描写内容。",
        "对不起，不能协助翻译该内容。",
        "Sorry, I can't translate this content.",
        "I am unable to help translate this content.",
    ],
)
def test_translation_refusal_is_not_accepted_as_description(text: str) -> None:
    with pytest.raises(AppError) as caught:
        _validate_translation_text(text)

    assert caught.value.code == "TRANSLATION_REFUSED"


@pytest.mark.asyncio
async def test_translate_description_requires_complete_configuration() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as db, pytest.raises(AppError) as caught:
        await translate_description_text(db, "作品紹介")

    assert caught.value.code == "TRANSLATION_NOT_CONFIGURED"


@pytest.mark.asyncio
async def test_translate_description_uses_openai_compatible_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    captured: dict = {}

    async def fake_post(self, url, *, headers, json):
        captured.update(url=url, headers=headers, json=json)
        return httpx.Response(
            200,
            request=httpx.Request("POST", url),
            json={"choices": [{"message": {"content": "翻译后的简介"}}]},
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    with Session(engine) as db:
        for key, value in {
            "translation_base_url": "https://example.test/v1/",
            "translation_api_key": "secret",
            "translation_model": "qwen-max",
            "translation_timeout_seconds": 15,
        }.items():
            db.add(AppSetting(key=key, value=value))
        db.commit()

        result = await translate_description_text(db, "作品紹介")

    assert result == "翻译后的简介"
    assert captured["url"] == "https://example.test/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer secret"
    assert captured["json"]["messages"][1]["content"] == "作品紹介"
    assert captured["json"]["enable_thinking"] is False


@pytest.mark.asyncio
async def test_translate_description_dispatches_to_tmt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    captured: dict = {}

    def fake_translate(description, secret_id, secret_key, region):
        captured.update(
            description=description,
            secret_id=secret_id,
            secret_key=secret_key,
            region=region,
        )
        return "腾讯云译文"

    monkeypatch.setattr(description_translation, "_translate_tmt_sync", fake_translate)

    with Session(engine) as db:
        for key, value in {
            "translation_provider": "tmt",
            "tmt_secret_id": "id",
            "tmt_secret_key": "key",
            "tmt_region": "ap-guangzhou",
        }.items():
            db.add(AppSetting(key=key, value=value))
        db.commit()

        result = await translate_description_text(db, "作品紹介")

    assert result == "腾讯云译文"
    assert captured == {
        "description": "作品紹介",
        "secret_id": "id",
        "secret_key": "key",
        "region": "ap-guangzhou",
    }


@pytest.mark.asyncio
async def test_translate_description_endpoint_persists_and_protects_translation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    async def fake_translate(_db, description):
        assert description == "作品紹介"
        return "中文简介"

    monkeypatch.setattr(anime_router, "translate_description_text", fake_translate)

    with Session(engine) as db:
        row = Anime(title="作品", description="作品紹介")
        db.add(row)
        db.commit()
        anime_id = row.id

        result = await anime_router.translate_description(anime_id, db)

        assert result.description == "中文简介"
        assert "description" in result.manual_fields
        assert result.field_provenance["description"] == "translation"


@pytest.mark.asyncio
async def test_refused_auto_translation_preserves_original_description(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    async def refuse_translation(_db, _description):
        raise AppError("TRANSLATION_REFUSED", "翻译服务拒绝了该内容")

    monkeypatch.setattr(
        description_translation,
        "translate_description_text",
        refuse_translation,
    )

    with Session(engine) as db:
        db.add(AppSetting(key="auto_translate_description", value=True))
        anime = Anime(
            title="作品",
            description="Original description",
            field_provenance={"description": "anidb"},
        )
        db.add(anime)
        db.commit()

        result = await auto_translate_anime_description(db, anime)

        assert result["status"] == "failed"
        assert result["code"] == "TRANSLATION_REFUSED"
        assert anime.description == "Original description"
        assert anime.field_provenance["description"] == "anidb"
        assert "description" not in anime.manual_fields
