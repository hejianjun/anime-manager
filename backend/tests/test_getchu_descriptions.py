import asyncio
from pathlib import Path

from lxml import etree
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models import Anime, LibraryRoot, MediaFile, SourceSnapshot, TaskRecord
from app.scrapers import Candidate, SCRAPERS, SourceMetadata
from app.scrapers.base import store_source_metadata
from app.services import getchu_descriptions as service


def session_factory():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)


def add_anime(db: Session, root: LibraryRoot, path: Path, title: str) -> Anime:
    path.write_bytes(b"video")
    anime = Anime(title=title, original_title=title, media_type="OVA")
    db.add(anime)
    db.flush()
    db.add(
        MediaFile(
            library_root=root,
            anime=anime,
            path=str(path),
            relative_path=path.name,
            size=5,
            modified_ns=1,
            parsed_title=title,
            episode="1",
            status="present",
        )
    )
    db.flush()
    return anime


class StreamingGetchuStub:
    source = "getchu"

    def __init__(self) -> None:
        self.second_started = asyncio.Event()
        self.release_second = asyncio.Event()

    async def search(self, _db: Session, keyword: str) -> list[Candidate]:
        if keyword == "Second":
            self.second_started.set()
            await self.release_second.wait()
        return [
            Candidate(
                source="getchu",
                source_id="100" if keyword == "First" else "200",
                title=keyword,
                score=1,
            )
        ]

    async def detail(self, _db: Session, source_id: str) -> SourceMetadata:
        return SourceMetadata(
            source="getchu",
            source_id=source_id,
            title="商品标题",
            description="作品の紹介です。",
        )


def test_merge_plot_preserves_other_xml_content() -> None:
    original = b'<?xml version="1.0" encoding="UTF-8"?><!--before--><tvshow><!--keep--><title>A</title><plot>old</plot><custom value="1" /></tvshow>'

    merged = service._merge_plot(original, "tvshow", "new description")
    root = etree.fromstring(merged)

    assert root.findtext("plot") == "new description"
    assert root.find("custom").get("value") == "1"
    assert b"<!--keep-->" in merged
    assert b"<!--before-->" in merged


def test_xml_local_name_ignores_comments() -> None:
    assert service._xml_local_name(etree.Comment("keep")) is None
    assert service._xml_local_name(etree.Element("plot")) == "plot"


def test_source_snapshot_store_upserts_after_stale_cache_lookup() -> None:
    factory = session_factory()
    first = SourceMetadata(source="getchu", source_id="100", title="first")
    second = SourceMetadata(source="getchu", source_id="100", title="second")

    with factory() as db:
        store_source_metadata(db, "getchu", "100", {"version": 1}, first, None)
        db.commit()
    # Simulate another request that looked up the snapshot before the first
    # transaction committed and therefore still passes cached=None.
    with factory() as db:
        store_source_metadata(db, "getchu", "100", {"version": 2}, second, None)
        db.commit()
        snapshots = db.query(SourceSnapshot).all()

    assert len(snapshots) == 1
    assert snapshots[0].raw_payload == {"version": 2}
    assert snapshots[0].normalized_payload["title"] == "second"


def test_nfo_bundle_restores_previous_files_when_one_write_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    factory = session_factory()
    with factory() as db:
        root = LibraryRoot(path=str(tmp_path))
        db.add(root)
        db.flush()
        anime = add_anime(db, root, tmp_path / "Example.mkv", "Example")
        anime.description = "new"
        originals = {
            "tvshow.nfo": b"<tvshow><plot>show old</plot></tvshow>",
            "season.nfo": b"<season><plot>season old</plot></season>",
            "Example.nfo": b"<episodedetails><plot>episode old</plot></episodedetails>",
        }
        for name, content in originals.items():
            (tmp_path / name).write_bytes(content)
        db.commit()

        real_write = service._atomic_write
        calls = 0

        def fail_second(path: Path, content: bytes, overwrite: bool) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("simulated NAS failure")
            real_write(path, content, overwrite)

        monkeypatch.setattr(service, "_atomic_write", fail_second)
        try:
            service._write_nfo_bundle(anime)
        except Exception as exc:
            assert getattr(exc, "code", None) == "GETCHU_NFO_WRITE_FAILED"
        else:
            raise AssertionError("expected bundle write to fail")

        for name, content in originals.items():
            assert (tmp_path / name).read_bytes() == content


async def test_preview_publishes_first_row_before_later_search_finishes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    factory = session_factory()
    stub = StreamingGetchuStub()
    monkeypatch.setattr(service, "SessionLocal", factory)
    monkeypatch.setitem(SCRAPERS, "getchu", stub)
    with factory() as db:
        root = LibraryRoot(path=str(tmp_path))
        db.add(root)
        db.flush()
        first_dir = tmp_path / "First"
        second_dir = tmp_path / "Second"
        first_dir.mkdir()
        second_dir.mkdir()
        first = add_anime(db, root, first_dir / "First.mkv", "First")
        second = add_anime(db, root, second_dir / "Second.mkv", "Second")
        rows = service.build_initial_rows([first, second])
        task = TaskRecord(
            kind=service.PREVIEW_KIND,
            result={"processed": 0, "total": 2, "items": rows},
        )
        db.add(task)
        db.commit()
        task_id = task.id

    running = asyncio.create_task(service.run_getchu_description_preview(task_id))
    await asyncio.wait_for(stub.second_started.wait(), timeout=2)
    with factory() as db:
        task = db.get(TaskRecord, task_id)
        assert task.status == "running"
        assert task.result["items"][0]["search_status"] == "ready"
        assert task.result["items"][1]["search_status"] == "searching"
    stub.release_second.set()
    await running
    with factory() as db:
        task = db.get(TaskRecord, task_id)
        assert task.status == "completed"
        assert task.result["processed"] == 2


async def test_confirmed_item_writes_while_preview_is_still_running(
    tmp_path: Path,
    monkeypatch,
) -> None:
    factory = session_factory()
    stub = StreamingGetchuStub()
    monkeypatch.setattr(service, "SessionLocal", factory)
    monkeypatch.setitem(SCRAPERS, "getchu", stub)

    async def translate(_db: Session, anime: Anime) -> dict[str, str]:
        anime.description = "这是翻译后的中文简介。"
        provenance = dict(anime.field_provenance or {})
        provenance["description"] = "translation"
        anime.field_provenance = provenance
        return {"status": "translated"}

    monkeypatch.setattr(service, "auto_translate_anime_description", translate)
    with factory() as db:
        root = LibraryRoot(path=str(tmp_path))
        db.add(root)
        db.flush()
        anime = add_anime(db, root, tmp_path / "Example.mkv", "Example")
        (tmp_path / "tvshow.nfo").write_text(
            "<tvshow><!--keep--><title>Example</title><plot>old</plot></tvshow>",
            encoding="utf-8",
        )
        (tmp_path / "season.nfo").write_text("<season><plot>old</plot></season>", encoding="utf-8")
        (tmp_path / "Example.nfo").write_text(
            "<episodedetails><plot>old</plot></episodedetails>", encoding="utf-8"
        )
        rows = service.build_initial_rows([anime])
        rows[0].update(
            {
                "search_status": "ready",
                "candidates": [
                    {
                        "source_id": "100",
                        "title": "Example",
                        "year": None,
                        "cover_url": None,
                        "score": 1,
                    }
                ],
            }
        )
        preview = TaskRecord(
            kind=service.PREVIEW_KIND,
            status="running",
            result={"processed": 1, "total": 1, "items": rows},
        )
        db.add(preview)
        db.flush()
        child = TaskRecord(
            parent_task_id=preview.id,
            kind=service.WRITE_KIND,
            result={"anime_id": anime.id, "source_id": "100"},
        )
        db.add(child)
        db.commit()
        anime_id = anime.id
        child_id = child.id

    await service.run_getchu_description_write(child_id)

    with factory() as db:
        child = db.get(TaskRecord, child_id)
        anime = db.get(Anime, anime_id)
        assert child.status == "completed"
        assert child.result["translation"] == {"status": "translated"}
        assert anime.description == "这是翻译后的中文简介。"
        assert anime.field_provenance["description"] == "translation"
    for name in ("tvshow.nfo", "season.nfo", "Example.nfo"):
        assert "这是翻译后的中文简介。" in (tmp_path / name).read_text(encoding="utf-8")
        assert list(tmp_path.glob(f"{name}.*.bak"))
    assert "<!--keep-->" in (tmp_path / "tvshow.nfo").read_text(encoding="utf-8")
