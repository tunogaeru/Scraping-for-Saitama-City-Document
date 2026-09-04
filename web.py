"""ブラウザから使う簡易フロントエンド。

    python web.py

を実行するとローカルサーバーが起動し、既定のブラウザが開きます。
URLを入れて「URLのファイルを取得する」を押すだけで、コマンドプロンプト版
（main.py）と全く同じ処理が動きます。出力先も同じくデスクトップです。

構成:
  - Python標準ライブラリのみで動作する（新規ライブラリのインストール不要）
  - 127.0.0.1 にのみ待ち受ける（同じLANの他端末からは接続できない）
  - 処理本体は main.main() をそのまま呼ぶ。画面出力の差し替えは
    Printer を WebPrinter に置き換えることで行う（main.py 側の変更は1行）
"""

from __future__ import annotations

import ctypes
import json
import re
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import main as backend
from scraper import cli, urlutils

HOST = "127.0.0.1"
PORT_CANDIDATES = range(8000, 8010)

#: 「N) 名前」形式の選択肢行。キャビネット選択のボタン見出しに使う
_OPTION_RE = re.compile(r"^\s*(\d+)\)\s*(.+?)\s*$")


# --- 実行中のジョブ -------------------------------------------------------

class Job:
    """1回の収集処理。ワーカースレッドとブラウザの間の受け渡しを担う。

    ブラウザは1秒ごとに /status を叩いて未取得の行だけを受け取る。
    バックエンドが確認を求めたとき（confirm/choose）は ask() でワーカーを
    止め、ブラウザからの /answer で再開する。
    """

    def __init__(self, url: str) -> None:
        self.url = url
        self.lines: list[str] = []
        self.progress = ""
        self.question: dict | None = None
        self.done = False
        self.exit_code: int | None = None
        self.thread: threading.Thread | None = None
        self._answer: str | None = None
        self._answered = threading.Event()
        self._lock = threading.Lock()
        self._seq = 0

    # -- ワーカー側から呼ぶ ------------------------------------------------

    def add_line(self, text: str) -> None:
        with self._lock:
            self.progress = ""
            self.lines.append(text)

    def set_progress(self, text: str) -> None:
        with self._lock:
            self.progress = text

    def ask(self, payload: dict) -> str | None:
        """ブラウザに問いを出し、答えが返るまでワーカーを止める。"""
        self._answered.clear()
        with self._lock:
            self._answer = None
            self._seq += 1
            # 同じ文面の問いが2度出ることがある（中断確認など）。連番を付けて
            # 画面側が「前と同じ問い」と誤認しないようにする
            self.question = dict(payload, seq=self._seq)
        self._answered.wait()
        with self._lock:
            self.question = None
            return self._answer

    def options(self, maximum: int) -> dict[str, str]:
        """直前に出力された「N) 名前」行から選択肢の見出しを拾う。"""
        found: dict[str, str] = {}
        with self._lock:
            recent = self.lines[-(maximum + 10):]
        for line in recent:
            matched = _OPTION_RE.match(line)
            if matched and int(matched.group(1)) <= maximum:
                found[matched.group(1)] = matched.group(2)
        return found

    # -- ブラウザ側から呼ぶ ------------------------------------------------

    def answer(self, value: str) -> bool:
        with self._lock:
            if self.question is None:
                return False
            self._answer = value
        self._answered.set()
        return True

    def stop(self) -> None:
        """中止させる。コンソール版の Ctrl+C と同じ経路を通す。

        確認待ちで止まっている場合は「中止」を返して解放する。処理中の
        場合はワーカースレッドに KeyboardInterrupt を送り込む。クローラは
        これを捕捉して「収集済みの資料で続行しますか？」を聞いてくるため、
        そこまでの成果は失われない。
        """
        with self._lock:
            waiting = self.question is not None
        if waiting:
            self.answer("cancel")
            return
        thread = self.thread
        if thread is None or not thread.is_alive():
            return
        # スリープ・通信中は即座には効かない（最大で1リクエスト分の待ち）
        ctypes.pythonapi.PyThreadState_SetAsyncExc(
            ctypes.c_ulong(thread.ident), ctypes.py_object(KeyboardInterrupt))

    def snapshot(self, start: int) -> dict:
        with self._lock:
            return {
                "lines": self.lines[start:],
                "next": len(self.lines),
                "progress": self.progress,
                "question": self.question,
                "done": self.done,
                "exit_code": self.exit_code,
            }


class WebPrinter(cli.Printer):
    """コンソールではなくブラウザへ出力する Printer。"""

    def __init__(self, job: Job) -> None:
        super().__init__()
        self._job = job
        self._tty = False

    def line(self, text: str = "") -> None:
        self._job.add_line(text)

    def progress(self, text: str) -> None:
        self._job.set_progress(text)

    def _clear(self) -> None:
        self._job.set_progress("")

    def prompt_url(self) -> str | None:
        return None                     # URLは画面から渡されるため使わない

    def confirm(self, question: str) -> bool:
        answer = self._job.ask({"kind": "confirm", "text": question})
        yes = answer == "yes"
        self.line(f"{question} → " + ("はい" if yes else "いいえ"))
        return yes

    def choose(self, maximum: int, default: int = 0) -> int | None:
        answer = self._job.ask({
            "kind": "choose",
            "text": "番号を選んでください",
            "max": maximum,
            "default": default,
            "options": self._job.options(maximum),
        })
        if answer is None or not answer.isdigit():
            self.line("選択 → 中止")
            return None
        self.line(f"選択 → {answer}")
        return int(answer)

    def wait_exit(self) -> None:
        pass                            # ブラウザ版では画面が閉じないため不要


def _worker(job: Job) -> None:
    printer = WebPrinter(job)
    try:
        job.exit_code = backend.main([job.url], printer=printer)
    except KeyboardInterrupt:
        printer.error("処理が中断されました。")
        job.exit_code = 1
    except BaseException as exc:                # noqa: BLE001 - 最終防衛線
        printer.error(f"予期しないエラー: {exc.__class__.__name__}: {exc}")
        job.exit_code = 1
    finally:
        job.set_progress("")
        job.done = True


# --- HTTPサーバー ---------------------------------------------------------

_state_lock = threading.Lock()
_job: Job | None = None


def _start(url: str) -> dict:
    global _job
    normalized = urlutils.normalize(url)
    if normalized is None or not normalized.lower().startswith(("http://", "https://")):
        return {"ok": False, "error": "URLの形式が正しくありません。"
                                      "http:// または https:// から入力してください。"}
    with _state_lock:
        if _job is not None and not _job.done:
            return {"ok": False, "error": "すでに実行中です。"}
        job = Job(normalized)
        job.thread = threading.Thread(target=_worker, args=(job,), daemon=True)
        _job = job
    job.thread.start()
    return {"ok": True}


class Handler(BaseHTTPRequestHandler):
    server_version = "SaitamaDocCollector"

    def log_message(self, *args) -> None:
        pass                            # アクセスログは出さない

    def _json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        try:
            size = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(size) or b"{}")
        except (ValueError, TypeError):
            return {}

    def do_GET(self) -> None:            # noqa: N802 - BaseHTTPRequestHandler の規約
        path = urlparse(self.path).path
        if path == "/":
            body = PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/status":
            query = parse_qs(urlparse(self.path).query)
            try:
                start = int(query.get("from", ["0"])[0])
            except ValueError:
                start = 0
            with _state_lock:
                job = _job
            if job is None:
                self._json({"lines": [], "next": 0, "progress": "",
                            "question": None, "done": True, "exit_code": None,
                            "running": False})
                return
            payload = job.snapshot(start)
            payload["running"] = not job.done
            self._json(payload)
            return
        self.send_error(404)

    def do_POST(self) -> None:           # noqa: N802
        path = urlparse(self.path).path
        if path == "/start":
            self._json(_start(str(self._body().get("url", "")).strip()))
            return
        if path == "/answer":
            with _state_lock:
                job = _job
            value = str(self._body().get("value", ""))
            self._json({"ok": bool(job and job.answer(value))})
            return
        if path == "/stop":
            with _state_lock:
                job = _job
            if job is not None:
                job.stop()
            self._json({"ok": True})
            return
        self.send_error(404)


# --- 画面 -----------------------------------------------------------------

PAGE = """<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>さいたま市文書 収集ツール</title>
<style>
  :root {
    --bg: #f6f7f9; --fg: #1b1d21; --muted: #5f6672;
    --card: #ffffff; --border: #dfe3e8; --accent: #1a56a8;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #16181c; --fg: #e6e8ea; --muted: #9aa1ac;
      --card: #1e2126; --border: #333940; --accent: #4c8ae0;
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 32px 20px 48px; background: var(--bg); color: var(--fg);
    font-family: "Hiragino Kaku Gothic ProN", "Yu Gothic UI", "Meiryo",
                 system-ui, sans-serif;
    font-size: 15px; line-height: 1.7;
  }
  main { max-width: 840px; margin: 0 auto; }
  h1 { font-size: 20px; margin: 0 0 4px; }
  .sub { color: var(--muted); font-size: 13px; margin: 0 0 24px; }
  form { display: flex; gap: 10px; flex-wrap: wrap; }
  input[type=text] {
    flex: 1 1 320px; min-width: 0; padding: 11px 13px; font-size: 15px;
    font-family: inherit; color: var(--fg); background: var(--card);
    border: 1px solid var(--border); border-radius: 7px;
  }
  input[type=text]:focus { outline: 2px solid var(--accent); outline-offset: -1px; }
  button {
    padding: 11px 20px; font-size: 15px; font-family: inherit; font-weight: 600;
    color: #fff; background: var(--accent); border: 0; border-radius: 7px;
    cursor: pointer; white-space: nowrap;
  }
  button:hover { filter: brightness(1.08); }
  button:disabled { opacity: .45; cursor: default; filter: none; }
  button.sub-btn { background: transparent; color: var(--muted);
                   border: 1px solid var(--border); font-weight: 400; }
  #note { margin: 14px 0 0; color: var(--muted); font-size: 13px; }
  #ask {
    margin-top: 20px; padding: 16px 18px; background: var(--card);
    border: 1px solid var(--accent); border-radius: 9px;
  }
  #ask p { margin: 0 0 12px; font-weight: 600; }
  #ask .choices { display: flex; gap: 8px; flex-wrap: wrap; }
  #ask button { padding: 8px 16px; font-size: 14px; }
  #log {
    margin-top: 20px; padding: 16px 18px; min-height: 300px; max-height: 62vh;
    overflow: auto; background: #14161a; color: #d8dce2; border-radius: 9px;
    font-family: "SFMono-Regular", "Consolas", "BIZ UDGothic", monospace;
    font-size: 12.5px; line-height: 1.55; white-space: pre-wrap;
    word-break: break-word;
  }
  #log:empty::before { content: "実行するとここに進捗が表示されます。"; color: #6b7280; }
  #prog { color: #8ab4f8; }
  [hidden] { display: none !important; }
</style>
</head>
<body>
<main>
  <h1>さいたま市文書 収集ツール</h1>
  <p class="sub">URLを入力すると、その配下の資料を集めて1つのPDFに結合し、デスクトップに出力します。</p>

  <form id="form">
    <input id="url" type="text" placeholder="https://example.com/docs/"
           autocomplete="off" spellcheck="false" autofocus>
    <button id="go" type="submit">URLのファイルを取得する</button>
    <button id="stop" type="button" class="sub-btn" hidden>中止</button>
  </form>
  <p id="note">サーバー負荷を避けるため1リクエスト1.5秒間隔で進みます。資料が多いサイトでは数時間かかります。</p>

  <div id="ask" hidden><p id="askText"></p><div class="choices" id="askChoices"></div></div>

  <div id="log"></div>
</main>

<script>
const $ = (id) => document.getElementById(id);
let cursor = 0, polling = false;

function append(text) {
  const box = $("log");
  const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
  $("prog").remove();
  box.append(text);
  box.append(Object.assign(document.createElement("span"), {id: "prog"}));
  if (atBottom) box.scrollTop = box.scrollHeight;
}

function setProgress(text) {
  $("prog").textContent = text ? text + "\\n" : "";
}

function setRunning(on) {
  $("go").disabled = on;
  $("url").disabled = on;
  $("stop").hidden = !on;
}

async function post(path, body) {
  const res = await fetch(path, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(body || {}),
  });
  return res.json();
}

function renderAsk(q) {
  const box = $("ask");
  if (!q) { box.hidden = true; return; }
  // 回答直後はサーバー側の反映待ちで同じ問いが返るため、出し直さない
  const key = JSON.stringify(q);
  if (box.dataset.key === key) return;
  box.dataset.key = key;
  $("askText").textContent = q.text;
  const choices = $("askChoices");
  choices.textContent = "";
  const add = (label, value, sub) => {
    const b = document.createElement("button");
    b.textContent = label;
    if (sub) b.className = "sub-btn";
    b.onclick = () => { box.hidden = true; post("/answer", {value}); };
    b.type = "button";
    choices.append(b);
  };
  if (q.kind === "confirm") {
    add("はい", "yes");
    add("いいえ", "no", true);
  } else {
    for (let i = 0; i <= q.max; i++) {
      const name = (q.options || {})[String(i)];
      add(name ? i + ") " + name : String(i), String(i), i !== q.default);
    }
    add("中止", "cancel", true);
  }
  box.hidden = false;
}

async function poll() {
  let s;
  try {
    s = await (await fetch("/status?from=" + cursor)).json();
  } catch (e) {
    append("\\n[通信が切れました。サーバーが停止した可能性があります]\\n");
    setRunning(false); polling = false; return;
  }
  cursor = s.next;
  if (s.lines.length) append(s.lines.join("\\n") + "\\n");
  setProgress(s.progress);
  renderAsk(s.question);
  if (s.done) { setRunning(false); polling = false; return; }
  setTimeout(poll, 1000);
}

$("form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const url = $("url").value.trim();
  if (!url || polling) return;
  $("log").textContent = "";
  $("log").append(Object.assign(document.createElement("span"), {id: "prog"}));
  cursor = 0;
  setRunning(true);
  const res = await post("/start", {url});
  if (!res.ok) { append(res.error + "\\n"); setRunning(false); return; }
  polling = true;
  poll();
});

$("stop").addEventListener("click", () => {
  append("\\n[中止を要求しました。通信の待ち時間のぶん、反映まで数秒かかることがあります]\\n");
  post("/stop", {});
});
</script>
</body>
</html>
"""


def serve() -> None:
    for port in PORT_CANDIDATES:
        try:
            httpd = ThreadingHTTPServer((HOST, port), Handler)
            break
        except OSError:
            continue
    else:
        raise SystemExit(f"空きポートが見つかりません（{PORT_CANDIDATES[0]}〜"
                         f"{PORT_CANDIDATES[-1]} は全て使用中です）")

    url = f"http://{HOST}:{port}/"
    print("=" * 70)
    print(" さいたま市文書 収集ツール（ブラウザ版）")
    print("=" * 70)
    print(f"ブラウザで次のURLを開いてください: {url}")
    print("終了するには、この画面で Ctrl+C を押してください。")
    print()
    webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n終了しました。")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    serve()
