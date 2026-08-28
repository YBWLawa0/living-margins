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
    def __init__(
        self,
        root: tk.Tk,
        state_api_url: str,
        web_api_url: str,
        machine_code: str,
    ):
        self.root = root
        self.state_api_url = state_api_url.rstrip("/")
        self.web_api_url = web_api_url.rstrip("/")
        self.machine_code = machine_code.strip().upper()
        self.results: queue.Queue = queue.Queue()
        self.poll_running = False
        self.last_revision = None
        self.current_status = ""
        self.current_has_comment = False
        self.current_comment: dict | None = None
        self.current_book_id: str | None = None
        self.current_feedback: str | None = None
        self.feedback_available = False
        self.feedback_error: str | None = None

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
            with urlopen(f"{self.state_api_url}/state", timeout=1.5) as response:
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
            elif kind == "feedback_state":
                current_id = self.current_comment.get("id") if self.current_comment else None
                if value.get("comment_id") != current_id:
                    continue
                feedback = value.get("feedback")
                self.feedback_available = True
                self.feedback_error = None
                self.current_feedback = feedback.get("action") if isinstance(feedback, dict) else None
                self.connection_label.configure(text="屏幕已绑定 · 用户反馈已同步", fg="#24723d")
                self.update_feedback_buttons()
            elif kind == "feedback_state_error":
                current_id = self.current_comment.get("id") if self.current_comment else None
                if value.get("comment_id") != current_id:
                    continue
                self.feedback_available = False
                self.feedback_error = str(value.get("message"))
                self.connection_label.configure(text=f"反馈不可用：{self.feedback_error}", fg="#a43d35")
                self.update_feedback_buttons()
            elif kind == "feedback":
                current_id = self.current_comment.get("id") if self.current_comment else None
                if value.get("comment_id") != current_id:
                    continue
                response = value.get("response") or {}
                feedback = response.get("feedback")
                outcome = response.get("outcome")
                self.feedback_available = True
                self.feedback_error = None
                self.current_feedback = feedback.get("action") if isinstance(feedback, dict) else None
                message = {
                    "created": "反馈已记录",
                    "changed": "选择已更改",
                    "unchanged": "已经是这个选择",
                }.get(outcome, "反馈已记录")
                self.connection_label.configure(text=message, fg="#24723d")
                self.update_feedback_buttons()
            elif kind == "feedback_error":
                current_id = self.current_comment.get("id") if self.current_comment else None
                if value.get("comment_id") != current_id:
                    continue
                self.connection_label.configure(text=f"反馈失败：{value.get('message')}", fg="#a43d35")
                self.update_feedback_buttons()
            else:
                self.connection_label.configure(text="未连接到识别程序，正在重试", fg="#a43d35")

    def show_state(self, state: dict) -> None:
        revision = state.get("revision")
        if self.feedback_error:
            self.connection_label.configure(text=f"反馈不可用：{self.feedback_error}", fg="#a43d35")
        else:
            self.connection_label.configure(text=f"已连接 · 状态 #{revision}", fg="#657278")
        if revision == self.last_revision:
            return
        self.last_revision = revision

        title = state.get("title") or "尚未识别书籍"
        pages = state.get("pages")
        comment = state.get("comment")
        self.current_book_id = state.get("book_id")
        status = str(state.get("status", ""))
        self.current_status = status
        self.current_has_comment = isinstance(comment, dict) and bool(comment.get("text"))
        previous_comment_id = (
            self.current_comment.get("id") if isinstance(self.current_comment, dict) else None
        )
        self.current_comment = comment if isinstance(comment, dict) else None
        current_comment_id = self.current_comment.get("id") if self.current_comment else None
        if current_comment_id != previous_comment_id:
            self.current_feedback = None
            self.feedback_available = False
            self.feedback_error = None
            if current_comment_id:
                threading.Thread(
                    target=self.fetch_feedback_state,
                    args=(str(current_comment_id),),
                    daemon=True,
                ).start()
        self.title_label.configure(text=title)
        if isinstance(pages, list) and len(pages) == 2:
            self.page_label.configure(text=f"P{pages[0]} – P{pages[1]}")
            if self.current_has_comment:
                text = str(comment.get("text", "")).strip()
                author = str(comment.get("author") or "匿名读者")
                comment_page = comment.get("page")
                comment_page_end = comment.get("page_end")
                if comment_page and comment_page_end and comment_page_end != comment_page:
                    page_text = f"P{comment_page}–P{comment_page_end}"
                else:
                    page_text = f"P{comment_page}" if comment_page else ""
                attribution = f"— {author}" + (f" · {page_text}" if page_text else "")
                if status == "stable":
                    message = f"“{text}”\n\n{attribution}"
                else:
                    message = f"{STATUS_TEXT.get(status, '正在更新阅读状态')}……\n\n“{text}”"
            elif status == "stable":
                message = "本页暂时没有批注"
            else:
                message = STATUS_TEXT.get(status, "正在更新阅读状态")
        else:
            self.page_label.configure(text="--")
            message = STATUS_TEXT.get(status, "等待阅读状态")
        self.message_label.configure(text=message)
        self.update_feedback_buttons()

    def update_feedback_buttons(self) -> None:
        button_state = (
            "normal"
            if self.current_status == "stable"
            and self.current_has_comment
            and self.feedback_available
            else "disabled"
        )
        self.agree_button.configure(
            state=button_state,
            text="✓ 赞同" if self.current_feedback == "agree" else "赞同",
            relief="sunken" if self.current_feedback == "agree" else "raised",
        )
        self.disagree_button.configure(
            state=button_state,
            text="✓ 不赞同" if self.current_feedback == "disagree" else "不赞同",
            relief="sunken" if self.current_feedback == "disagree" else "raised",
        )

    @staticmethod
    def _http_error_message(error: HTTPError) -> str:
        try:
            value = json.loads(error.read().decode("utf-8"))
            return str(value.get("error") or error.reason)
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
            return str(error.reason)

    def fetch_feedback_state(self, comment_id: str) -> None:
        payload = json.dumps(
            {"machine_code": self.machine_code, "comment_id": comment_id}
        ).encode("utf-8")
        request = Request(
            f"{self.web_api_url}/api/device/feedback/current",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=1.5) as response:
                value = json.loads(response.read().decode("utf-8"))
            self.results.put(
                (
                    "feedback_state",
                    {"comment_id": comment_id, "feedback": value.get("feedback")},
                )
            )
        except HTTPError as exc:
            self.results.put(
                (
                    "feedback_state_error",
                    {"comment_id": comment_id, "message": self._http_error_message(exc)},
                )
            )
        except (URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
            self.results.put(
                (
                    "feedback_state_error",
                    {"comment_id": comment_id, "message": str(exc)},
                )
            )

    def send_feedback(self, action: str) -> None:
        if not self.current_comment or not self.current_comment.get("id"):
            return
        comment = dict(self.current_comment)
        comment_id = str(comment["id"])
        book_id = self.current_book_id
        self.agree_button.configure(state="disabled")
        self.disagree_button.configure(state="disabled")

        def submit() -> None:
            payload = json.dumps(
                {
                    "machine_code": self.machine_code,
                    "comment_id": comment_id,
                    "book_id": book_id,
                    "page": comment.get("page"),
                    "action": action,
                }
            ).encode("utf-8")
            request = Request(
                f"{self.web_api_url}/api/device/feedback",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                with urlopen(request, timeout=1.5) as response:
                    value = json.loads(response.read().decode("utf-8"))
                self.results.put(
                    ("feedback", {"comment_id": comment_id, "response": value})
                )
            except HTTPError as exc:
                self.results.put(
                    (
                        "feedback_error",
                        {"comment_id": comment_id, "message": self._http_error_message(exc)},
                    )
                )
            except (URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
                self.results.put(
                    (
                        "feedback_error",
                        {"comment_id": comment_id, "message": str(exc)},
                    )
                )

        threading.Thread(target=submit, daemon=True).start()


def main() -> int:
    parser = argparse.ArgumentParser(description="Library Terra virtual ESP32 display")
    parser.add_argument("--api", default="http://127.0.0.1:8765")
    parser.add_argument("--web-api", default="http://127.0.0.1:8780")
    parser.add_argument("--machine-code", default="LM-DEMO-0001")
    args = parser.parse_args()
    root = tk.Tk()
    VirtualScreen(root, args.api, args.web_api, args.machine_code)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
