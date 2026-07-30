from __future__ import annotations

from pathlib import Path

from .models import LibraryRoot


def configured_library_paths(root: LibraryRoot) -> tuple[Path, ...]:
    """返回媒体库允许读取文件的主目录和可选扫描目录。"""
    paths = [Path(root.path).absolute()]
    if root.scan_path:
        scan_path = Path(root.scan_path).absolute()
        if scan_path not in paths:
            paths.append(scan_path)
    return tuple(paths)


def containing_library_path(path: Path, root: LibraryRoot) -> Path | None:
    """确定文件属于哪个已配置目录；不访问文件系统，便于 NAS 离线时校验。"""
    candidate = path.absolute()
    return next(
        (
            configured
            for configured in configured_library_paths(root)
            if candidate.is_relative_to(configured)
        ),
        None,
    )


def paths_overlap(first: Path, second: Path) -> bool:
    """主目录和扫描目录不能重叠，否则独立扫描无法可靠标记缺失文件。"""
    first = first.absolute()
    second = second.absolute()
    return first.is_relative_to(second) or second.is_relative_to(first)
