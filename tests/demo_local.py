"""ローカルのテストサイト相手に main.py を通しで実行するデモ。

実サイトにアクセスせず、デスクトップも汚さずに動作を確認できる。
テスト用サイトは tests/test_e2e.py のものを使い、LibreOfficeが無い環境では
スタブで代替する。

    python tests/demo_local.py                # 出力は一時フォルダ
    python tests/demo_local.py --desktop      # 出力を実際のデスクトップに作る
    python tests/demo_local.py --interval 1.5 # 本番と同じ1.5秒間隔で実行する
"""

from __future__ import annotations

import argparse
import http.server
import io
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from test_e2e import _build_site, _Handler, _make_soffice_stub  # noqa: E402

from scraper import config, winenv  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="ローカルテストサイトでの動作確認")
    parser.add_argument("--desktop", action="store_true",
                        help="出力先を実際のデスクトップにする（既定は一時フォルダ）")
    parser.add_argument("--interval", type=float, default=0.0,
                        help="リクエスト間隔（秒）。既定0＝待たない")
    parser.add_argument("--dry-run", action="store_true",
                        help="下見モード（main.py の --dry-run を試す）")
    args = parser.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="saitama_demo_"))
    site = tmp / "site"
    site.mkdir()
    _build_site(site)

    if args.desktop:
        desktop = winenv.desktop_dir()
    else:
        desktop = tmp / "Desktop"
        desktop.mkdir()

    soffice = winenv.find_soffice()
    stubbed = soffice is None
    if stubbed:
        soffice = _make_soffice_stub(tmp)

    handler = lambda *a, **kw: _Handler(*a, directory=str(site), **kw)  # noqa: E731
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    config.REQUEST_INTERVAL = args.interval

    import main as app
    app.winenv.find_soffice = lambda: soffice
    app.winenv.desktop_dir = lambda: desktop

    print(f"テストサイト: http://127.0.0.1:{port}/docs/")
    print(f"出力先デスクトップ: {desktop}")
    if stubbed:
        print("LibreOffice が無いため、変換はスタブで代替します"
              "（Office文書の変換品質は確認できません）")
    print()

    argv = [f"http://127.0.0.1:{port}/docs/"]
    if args.dry_run:
        argv.append("--dry-run")

    stdin = sys.stdin
    sys.stdin = io.StringIO("")
    try:
        code = app.main(argv)
    finally:
        sys.stdin = stdin
        server.shutdown()

    print("\n" + "=" * 70)
    print("生成されたファイル")
    print("=" * 70)
    for path in sorted(desktop.rglob("*")):
        size = f"{path.stat().st_size:,} バイト" if path.is_file() else "<フォルダ>"
        print(f"  {path.relative_to(desktop)}  {size}")
    print(f"\n出力先を開く: open '{desktop}'")
    return code


if __name__ == "__main__":
    sys.exit(main())
