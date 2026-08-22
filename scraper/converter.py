"""PDF変換（設計6.6）。拡張子に応じて変換器をディスパッチする。"""

from __future__ import annotations

import subprocess
from pathlib import Path

from PIL import Image
from pypdf import PdfReader
from pypdf.errors import PdfReadError

from . import config, winenv
from .models import Document, Status
from .report import Logger

#: 変換不要 / LibreOffice / Pillow のどれで処理するか
_READY_STATUSES = {Status.DOWNLOADED, Status.EXTRACTED}


class Converter:
    """soffice が None の場合、Office文書は変換せず失敗として記録する。"""

    def __init__(self, soffice: Path | None, work_dir: Path, log: Logger) -> None:
        self.soffice = soffice
        self.log = log
        self.out_dir = work_dir / "converted"
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.profile_dir = work_dir / "lo_profile"
        self.profile_dir.mkdir(parents=True, exist_ok=True)

    # -- 全体 ---------------------------------------------------------------

    def convert_all(self, docs: list[Document], progress) -> None:
        targets = [d for d in docs
                   if d.status in _READY_STATUSES and d.ext not in config.EXT_ARCHIVE]
        total = len(targets)
        for index, doc in enumerate(sorted(targets, key=lambda d: d.order), start=1):
            try:
                self._convert_one(doc)
            except Exception as exc:                      # 想定外でも全体は止めない
                doc.status = Status.FAILED_CONVERT
                doc.error = f"変換中の予期しないエラー: {exc.__class__.__name__}: {exc}"
                self.log.write(f"失敗:変換 {doc.label} — {doc.error}")
            progress(index, total)

        self._validate_all(docs)

    def _convert_one(self, doc: Document) -> None:
        assert doc.local_path is not None
        if doc.ext in config.EXT_PDF:
            doc.pdf_path = doc.local_path
        elif doc.ext in config.EXT_IMAGE:
            doc.pdf_path = self._convert_image(doc)
        else:
            doc.pdf_path = self._convert_office(doc)

        if doc.pdf_path is None and doc.status != Status.FAILED_CONVERT:
            doc.status = Status.FAILED_CONVERT
            doc.error = doc.error or "変換に失敗した"

    # -- LibreOffice --------------------------------------------------------

    def _convert_office(self, doc: Document) -> Path | None:
        """Office文書・テキストをPDF化する。

        -env:UserInstallation は必須。指定しないと、利用者が通常のLibreOfficeを
        開いている間はプロファイルのロック競合でヘッドレス変換が失敗する。
        """
        if self.soffice is None:
            doc.status = Status.FAILED_CONVERT
            doc.error = "LibreOffice未導入のため変換できない"
            self.log.write(f"失敗:変換 {doc.label} — {doc.error}")
            return None

        src = doc.local_path
        assert src is not None
        expected = self.out_dir / (src.stem + ".pdf")
        expected.unlink(missing_ok=True)

        command = [
            str(self.soffice),
            "--headless", "--norestore", "--nolockcheck", "--nodefault",
            f"-env:UserInstallation={self.profile_dir.resolve().as_uri()}",
            "--convert-to", "pdf",
            "--outdir", str(self.out_dir),
            str(src),
        ]
        try:
            proc = subprocess.run(
                command, capture_output=True, timeout=config.SOFFICE_TIMEOUT,
                creationflags=winenv.subprocess_flags(),
            )
        except subprocess.TimeoutExpired:
            doc.status = Status.FAILED_CONVERT
            doc.error = f"LibreOfficeがタイムアウト({config.SOFFICE_TIMEOUT}秒)"
            self.log.write(f"失敗:変換 {doc.label} — {doc.error}")
            return None
        except OSError as exc:
            doc.status = Status.FAILED_CONVERT
            doc.error = f"LibreOfficeを起動できない: {exc}"
            self.log.write(f"失敗:変換 {doc.label} — {doc.error}")
            return None

        # 正常終了しても出力が生成されない場合があるため、存在確認が必須
        if not expected.exists() or expected.stat().st_size == 0:
            detail = (proc.stderr or proc.stdout or b"").decode("utf-8", "replace").strip()
            doc.status = Status.FAILED_CONVERT
            doc.error = f"LibreOfficeがPDFを出力しなかった{': ' + detail[:120] if detail else ''}"
            self.log.write(f"失敗:変換 {doc.label} — {doc.error}")
            return None
        return expected

    # -- 画像 ---------------------------------------------------------------

    def _convert_image(self, doc: Document) -> Path | None:
        """画像をPDF化する。

        LibreOfficeは画像をDraw文書として扱うため余白付加と用紙サイズへの強制
        縮尺が入り原寸が保たれない。Pillowなら画像1枚をそのままの寸法で1ページの
        PDFにできる（設計6.6.2）。
        """
        src = doc.local_path
        assert src is not None
        dest = self.out_dir / (src.stem + ".pdf")
        try:
            with Image.open(src) as image:
                frames = self._frames(image)
                if not frames:
                    raise ValueError("ページを取り出せない")
                dpi = image.info.get("dpi") or (config.IMAGE_DEFAULT_DPI,) * 2
                frames[0].save(
                    dest, "PDF", save_all=True, append_images=frames[1:],
                    resolution=float(dpi[0]) or config.IMAGE_DEFAULT_DPI,
                )
        except Exception as exc:
            dest.unlink(missing_ok=True)
            doc.status = Status.FAILED_CONVERT
            doc.error = f"画像を変換できない: {exc.__class__.__name__}: {exc}"
            self.log.write(f"失敗:変換 {doc.label} — {doc.error}")
            return None
        return dest

    @staticmethod
    def _frames(image: Image.Image) -> list[Image.Image]:
        """マルチページTIFF/GIFを全ページ取り出し、RGBへ揃える。"""
        pages: list[Image.Image] = []
        count = getattr(image, "n_frames", 1)
        for index in range(count):
            if count > 1:
                image.seek(index)
            pages.append(Converter._to_rgb(image.copy()))
        return pages

    @staticmethod
    def _to_rgb(image: Image.Image) -> Image.Image:
        """PDFはアルファチャンネルを持てないため白背景に合成する。"""
        if image.mode in ("RGBA", "LA", "P", "PA"):
            image = image.convert("RGBA")
            canvas = Image.new("RGB", image.size, (255, 255, 255))
            canvas.paste(image, mask=image.split()[-1])
            return canvas
        if image.mode != "RGB":
            return image.convert("RGB")
        return image

    # -- 健全性検証 ---------------------------------------------------------

    def _validate_all(self, docs: list[Document]) -> None:
        """結合前に全PDFを開けることを確認する（設計6.6.3）。"""
        for doc in docs:
            if doc.pdf_path is None or doc.status.is_failure or doc.status.is_skip:
                continue
            try:
                reader = PdfReader(str(doc.pdf_path), strict=False)
                if reader.is_encrypted:
                    # 印刷・編集制限のみのPDFは空パスワードで開ける
                    if not reader.decrypt(""):
                        raise PdfReadError("パスワード保護されている")
                pages = len(reader.pages)
                if pages == 0:
                    raise PdfReadError("ページが1枚もない")
            except Exception as exc:
                doc.status = Status.FAILED_CONVERT
                doc.error = f"PDFを読めない: {exc.__class__.__name__}: {exc}"
                doc.pdf_path = None
                self.log.write(f"失敗:変換 {doc.label} — {doc.error}")
                continue

            doc.page_count = pages
            doc.pdf_size = doc.pdf_path.stat().st_size
            doc.status = Status.CONVERTED
