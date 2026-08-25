from __future__ import annotations

import json
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

from library_terra.comments import CommentStore, PageComment


ROOT = Path(__file__).resolve().parent


def load_books(books_root: Path) -> list[tuple[str, str]]:
    books = []
    for metadata_path in sorted(books_root.glob("*/book.json")):
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            book_id = str(metadata["id"])
            title = str(metadata.get("title") or book_id)
            books.append((book_id, title))
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
    return books


class CommentEditor:
    def __init__(self, root: tk.Tk, books_root: Path):
        self.root = root
        self.store = CommentStore(books_root)
        self.books = load_books(books_root)
        self.book_labels = {f"{title}  [{book_id}]": book_id for book_id, title in self.books}
        self.comments: list[PageComment] = []
        self.selected_comment_id: str | None = None

        root.title("Library Terra - 页码批注管理")
        root.geometry("760x570")
        root.minsize(680, 520)

        outer = ttk.Frame(root, padding=16)
        outer.pack(fill="both", expand=True)

        ttk.Label(outer, text="书籍").grid(row=0, column=0, sticky="w")
        self.book_value = tk.StringVar()
        self.book_combo = ttk.Combobox(
            outer,
            textvariable=self.book_value,
            values=list(self.book_labels),
            state="readonly",
            width=68,
        )
        self.book_combo.grid(row=1, column=0, columnspan=4, sticky="ew", pady=(3, 12))
        self.book_combo.bind("<<ComboboxSelected>>", lambda _event: self.refresh_comments())

        ttk.Label(outer, text="页码").grid(row=2, column=0, sticky="w")
        self.page_value = tk.IntVar(value=1)
        ttk.Spinbox(outer, from_=1, to=99999, textvariable=self.page_value, width=10).grid(
            row=3, column=0, sticky="w", pady=(3, 10)
        )

        ttk.Label(outer, text="署名").grid(row=2, column=1, sticky="w", padx=(12, 0))
        self.author_value = tk.StringVar(value="匿名读者")
        ttk.Entry(outer, textvariable=self.author_value, width=22).grid(
            row=3, column=1, sticky="ew", padx=(12, 0), pady=(3, 10)
        )

        ttk.Label(outer, text="优先级").grid(row=2, column=2, sticky="w", padx=(12, 0))
        self.priority_value = tk.IntVar(value=0)
        ttk.Spinbox(outer, from_=-99, to=99, textvariable=self.priority_value, width=8).grid(
            row=3, column=2, sticky="w", padx=(12, 0), pady=(3, 10)
        )

        self.enabled_value = tk.BooleanVar(value=True)
        ttk.Checkbutton(outer, text="启用", variable=self.enabled_value).grid(
            row=3, column=3, sticky="e", padx=(12, 0), pady=(3, 10)
        )

        ttk.Label(outer, text="批注内容").grid(row=4, column=0, sticky="w")
        self.text_input = tk.Text(outer, height=7, wrap="word", font=("Microsoft YaHei UI", 11))
        self.text_input.grid(row=5, column=0, columnspan=4, sticky="nsew", pady=(3, 10))

        actions = ttk.Frame(outer)
        actions.grid(row=6, column=0, columnspan=4, sticky="ew", pady=(0, 12))
        ttk.Button(actions, text="保存批注", command=self.save).pack(side="left")
        ttk.Button(actions, text="新建/清空", command=self.clear_form).pack(side="left", padx=8)
        ttk.Button(actions, text="删除选中", command=self.delete).pack(side="left")

        ttk.Label(outer, text="已有批注（点击可编辑）").grid(row=7, column=0, sticky="w")
        self.comment_list = tk.Listbox(outer, height=11, font=("Microsoft YaHei UI", 10))
        self.comment_list.grid(row=8, column=0, columnspan=4, sticky="nsew", pady=(3, 8))
        self.comment_list.bind("<<ListboxSelect>>", self.load_selected)

        self.status_value = tk.StringVar(value="请选择一本书")
        ttk.Label(outer, textvariable=self.status_value).grid(row=9, column=0, columnspan=4, sticky="w")

        outer.columnconfigure(1, weight=1)
        outer.columnconfigure(3, weight=1)
        outer.rowconfigure(5, weight=1)
        outer.rowconfigure(8, weight=1)

        if self.book_labels:
            self.book_combo.current(0)
            self.refresh_comments()
        else:
            self.book_combo.configure(state="disabled")
            self.status_value.set("书库中没有已注册书籍，请先运行 add_book.bat")

    def current_book_id(self) -> str | None:
        return self.book_labels.get(self.book_value.get())

    def refresh_comments(self) -> None:
        book_id = self.current_book_id()
        self.comment_list.delete(0, tk.END)
        self.comments = self.store.list(book_id) if book_id else []
        for comment in self.comments:
            marker = "" if comment.enabled else "[停用] "
            preview = " ".join(comment.text.split())[:55]
            self.comment_list.insert(
                tk.END,
                f"{marker}P{comment.page}  |  {comment.author}  |  {preview}",
            )
        self.status_value.set(f"当前共有 {len(self.comments)} 条批注")

    def clear_form(self) -> None:
        self.selected_comment_id = None
        self.page_value.set(1)
        self.author_value.set("匿名读者")
        self.priority_value.set(0)
        self.enabled_value.set(True)
        self.text_input.delete("1.0", tk.END)
        self.comment_list.selection_clear(0, tk.END)
        self.status_value.set("正在新建批注")

    def load_selected(self, _event=None) -> None:
        selection = self.comment_list.curselection()
        if not selection:
            return
        comment = self.comments[selection[0]]
        self.selected_comment_id = comment.comment_id
        self.page_value.set(comment.page)
        self.author_value.set(comment.author)
        self.priority_value.set(comment.priority)
        self.enabled_value.set(comment.enabled)
        self.text_input.delete("1.0", tk.END)
        self.text_input.insert("1.0", comment.text)
        self.status_value.set(f"正在编辑 P{comment.page} 的批注")

    def save(self) -> None:
        book_id = self.current_book_id()
        if not book_id:
            messagebox.showerror("无法保存", "请先选择书籍")
            return
        try:
            comment = self.store.upsert(
                book_id,
                page=self.page_value.get(),
                text=self.text_input.get("1.0", tk.END).strip(),
                author=self.author_value.get(),
                priority=self.priority_value.get(),
                enabled=self.enabled_value.get(),
                comment_id=self.selected_comment_id,
            )
        except (OSError, ValueError) as exc:
            messagebox.showerror("无法保存", str(exc))
            return
        self.selected_comment_id = comment.comment_id
        self.refresh_comments()
        self.status_value.set(f"已保存 P{comment.page} 的批注；识别程序会自动刷新")

    def delete(self) -> None:
        book_id = self.current_book_id()
        if not book_id or not self.selected_comment_id:
            messagebox.showinfo("没有选中批注", "请先点击列表中的一条批注")
            return
        if not messagebox.askyesno("确认删除", "确定删除当前选中的批注吗？"):
            return
        try:
            deleted = self.store.delete(book_id, self.selected_comment_id)
        except (OSError, ValueError) as exc:
            messagebox.showerror("无法删除", str(exc))
            return
        self.clear_form()
        self.refresh_comments()
        self.status_value.set("批注已删除" if deleted else "批注已经不存在")


def main() -> int:
    root = tk.Tk()
    CommentEditor(root, ROOT / "books")
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
