from app.main import app


EXPECTED_OPERATIONS = {
    ("GET", "/api/health"),
    ("GET", "/api/dashboard"),
    ("GET", "/api/library-roots"),
    ("POST", "/api/library-roots"),
    ("PATCH", "/api/library-roots/{root_id}"),
    ("POST", "/api/library-roots/{root_id}/scan"),
    ("GET", "/api/tasks/{task_id}"),
    ("GET", "/api/tasks/{task_id}/events"),
    ("PATCH", "/api/media-files/{media_id}"),
    ("GET", "/api/media-files/{media_id}/stream"),
    ("GET", "/api/sources/getchu/{source_id}/cover"),
    ("POST", "/api/sources/anidb/titles/refresh"),
    ("GET", "/api/match-groups"),
    ("POST", "/api/match-groups/bulk-search-confirm"),
    ("GET", "/api/match-groups/{group_id}"),
    ("PATCH", "/api/match-groups/{group_id}"),
    ("POST", "/api/match-groups/{group_id}/search"),
    ("PUT", "/api/match-groups/{group_id}/selections"),
    ("POST", "/api/match-groups/{group_id}/confirm"),
    ("POST", "/api/match-groups/{group_id}/bind-existing"),
    ("GET", "/api/anime"),
    ("GET", "/api/anime/{anime_id}"),
    ("PATCH", "/api/anime/{anime_id}"),
    ("POST", "/api/anime/{anime_id}/refresh"),
    ("POST", "/api/anime/{anime_id}/rename-preview"),
    ("POST", "/api/anime/{anime_id}/rename"),
    ("POST", "/api/anime/rename-preview"),
    ("POST", "/api/anime/rename"),
    ("POST", "/api/anime/artifacts-preview"),
    ("POST", "/api/anime/artifacts"),
    ("GET", "/api/anime/{anime_id}/export-preview"),
    ("POST", "/api/anime/{anime_id}/export"),
    ("GET", "/api/settings"),
    ("PATCH", "/api/settings"),
}


def test_api_operation_contract_is_preserved() -> None:
    schema = app.openapi()
    actual = {
        (method.upper(), path)
        for path, operations in schema["paths"].items()
        for method in operations
    }

    assert actual == EXPECTED_OPERATIONS
