from __future__ import annotations

import argparse
import json
import queue
import threading
import tkinter as tk
from tkinter import font as tkfont
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


STATUS_TEXT = {
    "searching_book": "等待识别书籍",
    "book_confirmed": "书籍已识别，请打开书本",
    "turning": "检测到翻页",
    "recognizing": "正在确认当前页",
    "stable": "当前页已确认",
    "offline": "识别程序已停止",
}


class VirtualScreen:
    def __init__(self, root: tk.Tk, api_url: str):
        self.root = root
        self.api_url = api_url.rstrip("/")
        self.results: queue.Queue = queue.Queue()
        self.poll_running = False
        self.last_revision = None
        self.current_status = ""
        self.current_has_comment = False

        root.title("Library Terra - 虚拟 ESP32 屏幕")
        root.geometry("480x320")
        root.minsize(420, 280)
        root.configure(bg="#f4f0e6")

        title_font = tkfont.Font(family="Microsoft YaHei UI", size=12, weight="bold")
        page_font = tkfont.Font(family="Microsoft YaHei UI", size=23, weight="bold")
        body_font = tkfont.Font(family="Microsoft YaHei UI", size=12)
        small_font = tkfont.Font(family="Microsoft YaHei UI", size=9)

        self.title_label = tk.Label(
            root,
            text="尚未识别书籍",
            anchor="w",
            bg="#253238",
            fg="white",
            padx=16,
            pady=10,
            font=title_font,
        )
        self.title_label.pack(fill="x")

        self.page_label = tk.Label(root, text="--", bg="#f4f0e6", fg="#172226", pady=12, font=page_font)
        self.page_label.pack()

        self.message_label = tk.Label(
            root,
            text="请先启动书籍识别程序",
            justify="center",
            wraplength=420,
            bg="#f4f0e6",
            fg="#334247",
            font=body_font,
        )
        self.message_label.pack(fill="both", expand=True, padx=24)

        buttons = tk.Frame(root, bg="#f4f0e6")
        buttons.pack(pady=(4, 8))
        self.agree_button = tk.Button(
            buttons,
            text="赞同",
            width=12,
            command=lambda: self.send_feedback("agree"),
            state="disabled",
        )
        self.agree_button.pack(side="left", padx=8)
        self.disagree_button = tk.Button(
            buttons,
            text="不赞同",
            width=12,
            command=lambda: self.send_feedback("disagree"),
            state="disabled",
        )
        self.disagree_button.pack(side="left", padx=8)

        self.connection_label = tk.Label(
            root,
            text="正在连接……",
            anchor="e",
            bg="#f4f0e6",
            fg="#657278",
            padx=12,
            pady=3,
            font=small_font,
        )
        self.connection_label.pack(fill="x")
        self.root.after(20, self.schedule_poll)

    def schedule_poll(self) -> None:
        if not self.poll_running:
            self.poll_running = True
            threading.Thread(target=self.fetch_state, daemon=True).start()
        self.consume_results()
        self.root.after(500, self.schedule_poll)

    def fetch_state(self) -> None:
        try:
            with urlopen(f"{self.api_url}/state", timeout=1.5) as response:
                state = json.loads(response.read().decode("utf-8"))
            self.results.put(("state", state))
        except (HTTPError, URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
            self.results.put(("error", str(exc)))
        finally:
            self.poll_running = False

    def consume_results(self) -> None:
        while True:
            try:
                kind, value = self.results.get_nowait()
            except queue.Empty:
                return
            if kind == "state":
                self.show_state(value)
            elif kind == "feedback":
                self.connection_label.configure(text="反馈已记录", fg="#24723d")
                self.restore_feedback_buttons()
            elif kind == "feedback_error":
                self.connection_label.configure(text=f"反馈失败：{value}", fg="#a43d35")
                self.restore_feedback_buttons()
            else:
                self.connection_label.configure(text="未连接到识别程序，正在重试", fg="#a43d35")

    def show_state(self, state: dict) -> None:
        revision = state.get("revision")
        self.connection_label.configure(text=f"已连接 · 状态 #{revision}", fg="#657278")
        if revision == self.last_revision:
            return
        self.last_revision = revision

        title = state.get("title") or "尚未识别书籍"
        pages = state.get("pages")
        comment = state.get("comment")
        status = str(state.get("status", ""))
        self.current_status = status
        self.current_has_comment = isinstance(comment, dict) and bool(comment.get("text"))
        self.title_label.configure(text=title)
        if isinstance(pages, list) and len(pages) == 2:
            self.page_label.configure(text=f"P{pages[0]} – P{pages[1]}")
            if self.current_has_comment:
                text = str(comment.get("text", "")).strip()
                author = str(comment.get("author") or "匿名读者")
                comment_page = comment.get("page")
                attribution = f"— {author}" + (f" · P{comment_page}" if comment_page else "")
                if status == "stable":
                    message = f"“{text}”\n\n{attribution}"
                else:
                    message = f"{STATUS_TEXT.get(status, '正在更新阅读状态')}……\n\n“{text}”"
            elif status == "stable":
                message = "本页暂时没有批注"
            else:
                message = STATUS_TEXT.get(status, "正在更新阅读状态")
            button_state = "normal" if status == "stable" and self.current_has_comment else "disabled"
        else:
            self.page_label.configure(text="--")
            message = STATUS_TEXT.get(status, "等待阅读状态")
            button_state = "disabled"
        self.message_label.configure(text=message)
        self.agree_button.configure(state=button_state)
        self.disagree_button.configure(state=button_state)

    def restore_feedback_buttons(self) -> None:
        button_state = "normal" if self.current_status == "stable" and self.current_has_comment else "disabled"
        self.agree_button.configure(state=button_state)
        self.disagree_button.configure(state=button_state)

    def send_feedback(self, action: str) -> None:
        self.agree_button.configure(state="disabled")
        self.disagree_button.configure(state="disabled")

        def submit() -> None:
            payload = json.dumps({"action": action, "device_id": "virtual-screen"}).encode("utf-8")
            request = Request(
                f"{self.api_url}/feedback",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                with urlopen(request, timeout=1.5) as response:
                    json.loads(response.read().decode("utf-8"))
                self.results.put(("feedback", None))
            except (HTTPError, URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
                self.results.put(("feedback_error", str(exc)))

        threading.Thread(target=submit, daemon=True).start()


def main() -> int:
    parser = argparse.ArgumentParser(description="Library Terra virtual ESP32 display")
    parser.add_argument("--api", default="http://127.0.0.1:8765")
    args = parser.parse_args()
    root = tk.Tk()
    VirtualScreen(root, args.api)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
