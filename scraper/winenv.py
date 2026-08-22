"""Windows固有の環境処理。

- デスクトップパスの解決（OneDriveリダイレクト対応）
- LibreOffice(soffice)の探索
- コンソール出力の文字化け・例外対策

Windows以外でもimport・実行できるよう、全ての分岐にフォールバックを持たせている
（開発時の単体テストのため）。
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

IS_WINDOWS = os.name == "nt"

#: Windowsの予約デバイス名。拡張子を除いた名前がこれに一致するファイルは作成できない
RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

_SOFFICE_CANDIDATES = [
    r"C:\Program Files\LibreOffice\program\soffice.exe",
    r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
    "/Applications/LibreOffice.app/Contents/MacOS/soffice",
    "/usr/bin/soffice",
    "/usr/local/bin/soffice",
]


def setup_console() -> None:
    """コンソール出力でUnicodeEncodeErrorが起きないようにする。

    日本語Windowsのコマンドプロンプトの既定コードページはCP932であり、CP932で
    表現できない文字を含むファイル名を print すると UnicodeEncodeError で異常
    終了する。エンコーディング自体は変更せず（utf-8にするとCP932コンソールでは
    全ての日本語が文字化けする）、エラーハンドラのみを replace に差し替える。
    表現できない文字は '?' になるが、完全な情報はUTF-8のlog.txtに残る。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(errors="replace")
        except (ValueError, OSError):
            pass


def desktop_dir() -> Path:
    """デスクトップの実パスを返す。

    %USERPROFILE%\\Desktop の直接参照は誤りになりうる。日本国内のWindowsでは
    OneDriveによりデスクトップが %USERPROFILE%\\OneDrive\\デスクトップ に
    リダイレクトされている環境が一般的で、その場合 ~/Desktop は存在しないか、
    実際に画面に表示されるデスクトップとは別のフォルダになる。
    Known Folder API で正しく解決する。
    """
    if IS_WINDOWS:
        path = _known_folder_desktop()
        if path is not None:
            return path
    return Path.home() / "Desktop"


def _known_folder_desktop() -> Path | None:
    import ctypes
    from ctypes import wintypes

    class GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", ctypes.c_ulong),
            ("Data2", ctypes.c_ushort),
            ("Data3", ctypes.c_ushort),
            ("Data4", ctypes.c_ubyte * 8),
        ]

    # FOLDERID_Desktop = {B4BFCC3A-DB2C-424C-B029-7FE99A87C641}
    folder_id = GUID(
        0xB4BFCC3A, 0xDB2C, 0x424C,
        (ctypes.c_ubyte * 8)(0xB0, 0x29, 0x7F, 0xE9, 0x9A, 0x87, 0xC6, 0x41),
    )

    try:
        shell32 = ctypes.windll.shell32
        ole32 = ctypes.windll.ole32
    except (AttributeError, OSError):
        return None

    shell32.SHGetKnownFolderPath.argtypes = [
        ctypes.POINTER(GUID), wintypes.DWORD, wintypes.HANDLE,
        ctypes.POINTER(ctypes.c_wchar_p),
    ]
    shell32.SHGetKnownFolderPath.restype = ctypes.HRESULT

    buf = ctypes.c_wchar_p()
    try:
        shell32.SHGetKnownFolderPath(ctypes.byref(folder_id), 0, None, ctypes.byref(buf))
    except OSError:
        return None
    if not buf.value:
        return None
    try:
        return Path(buf.value)
    finally:
        ole32.CoTaskMemFree(buf)


def find_soffice() -> Path | None:
    """LibreOfficeの実行ファイルを探す。見つからなければ None。"""
    for name in ("soffice", "soffice.exe"):
        found = shutil.which(name)
        if found:
            return Path(found)
    for candidate in _SOFFICE_CANDIDATES:
        path = Path(candidate)
        if path.exists():
            return path
    return None


def subprocess_flags() -> int:
    """子プロセス起動時にコンソールウィンドウを表示させないフラグ。"""
    if IS_WINDOWS:
        return getattr(__import__("subprocess"), "CREATE_NO_WINDOW", 0)
    return 0


def is_writable_dir(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".__write_test__"
        probe.write_bytes(b"")
        probe.unlink()
        return True
    except OSError:
        return False
