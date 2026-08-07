#!/usr/bin/env python3
"""주기적 새로고침 + 좌표 클릭 매크로 (버튼형 GUI).

명령어를 몰라도 창에서 버튼만 누르면 되는 쉬운 버전.
"""
from __future__ import annotations

import platform
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

try:
    import pyautogui
    pyautogui.PAUSE = 0
    pyautogui.FAILSAFE = True
    _PYAUTO_OK = True
except Exception:  # pyautogui 미설치/환경 문제
    _PYAUTO_OK = False

IS_MAC = platform.system() == "Darwin"

# 새로고침 키 선택지 (표시 이름 → pyautogui 키 조합)
KEY_CHOICES = {
    "새로고침 (Cmd+R · 맥 크롬)": ["command", "r"],
    "새로고침 (Ctrl+R · 윈도우 크롬)": ["ctrl", "r"],
    "새로고침 (F5)": ["f5"],
}
DEFAULT_KEY = "새로고침 (Cmd+R · 맥 크롬)" if IS_MAC else "새로고침 (Ctrl+R · 윈도우 크롬)"


class MacroApp:
    """매크로 GUI 애플리케이션."""

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.click_x: int | None = None
        self.click_y: int | None = None
        self._stop = threading.Event()
        self._worker: threading.Thread | None = None
        self._count = 0

        root.title("자동 새로고침 · 클릭 매크로")
        root.resizable(False, False)

        pad = {"padx": 14, "pady": 6}
        big_font = ("", 13)

        # 1. 클릭 위치
        frm_pos = ttk.LabelFrame(root, text="① 클릭할 위치")
        frm_pos.grid(row=0, column=0, sticky="ew", **pad)
        self.pos_label = ttk.Label(frm_pos, text="아직 지정 안 됨", font=big_font, foreground="#c0392b")
        self.pos_label.grid(row=0, column=0, padx=10, pady=8, sticky="w")
        ttk.Button(frm_pos, text="이 위치로 지정하기", command=self.capture_position).grid(
            row=0, column=1, padx=10, pady=8
        )
        ttk.Label(
            frm_pos,
            text="버튼을 누르고 3초 안에 마우스를 클릭할 곳에 올려두세요.",
            foreground="#555",
        ).grid(row=1, column=0, columnspan=2, padx=10, pady=(0, 8), sticky="w")

        # 2. 설정
        frm_set = ttk.LabelFrame(root, text="② 설정")
        frm_set.grid(row=1, column=0, sticky="ew", **pad)

        ttk.Label(frm_set, text="몇 초마다 새로고침").grid(row=0, column=0, padx=10, pady=8, sticky="w")
        self.interval_var = tk.StringVar(value="60")
        ttk.Entry(frm_set, textvariable=self.interval_var, width=8).grid(row=0, column=1, sticky="w")
        ttk.Label(frm_set, text="초").grid(row=0, column=2, sticky="w")

        ttk.Label(frm_set, text="새로고침 방식").grid(row=1, column=0, padx=10, pady=8, sticky="w")
        self.key_var = tk.StringVar(value=DEFAULT_KEY)
        ttk.Combobox(
            frm_set, textvariable=self.key_var, values=list(KEY_CHOICES.keys()),
            state="readonly", width=26,
        ).grid(row=1, column=1, columnspan=2, padx=(0, 10), sticky="w")

        ttk.Label(frm_set, text="새로고침 후 클릭까지 대기").grid(row=2, column=0, padx=10, pady=8, sticky="w")
        self.wait_var = tk.StringVar(value="2")
        ttk.Entry(frm_set, textvariable=self.wait_var, width=8).grid(row=2, column=1, sticky="w")
        ttk.Label(frm_set, text="초").grid(row=2, column=2, sticky="w")

        # 3. 시작 / 정지
        frm_run = ttk.Frame(root)
        frm_run.grid(row=2, column=0, sticky="ew", **pad)
        self.start_btn = tk.Button(
            frm_run, text="▶ 시작", font=("", 15, "bold"), bg="#27ae60", fg="white",
            activebackground="#219150", width=10, command=self.start,
        )
        self.start_btn.grid(row=0, column=0, padx=6, ipady=6)
        self.stop_btn = tk.Button(
            frm_run, text="■ 정지", font=("", 15, "bold"), bg="#c0392b", fg="white",
            activebackground="#a93226", width=10, command=self.stop, state="disabled",
        )
        self.stop_btn.grid(row=0, column=1, padx=6, ipady=6)

        # 4. 상태
        self.status = tk.Text(root, height=8, width=46, state="disabled", bg="#1e1e1e", fg="#dcdcdc")
        self.status.grid(row=3, column=0, padx=14, pady=(4, 4))
        ttk.Label(
            root, text="정지: [정지] 버튼 · 또는 마우스를 화면 맨 왼쪽 위 모서리로",
            foreground="#555",
        ).grid(row=4, column=0, pady=(0, 10))

        if not _PYAUTO_OK:
            self.log("⚠ pyautogui 가 설치되지 않아 동작할 수 없습니다.")
            self.log("   터미널에서:  pip install pyautogui")
            self.start_btn.config(state="disabled")
        elif IS_MAC:
            self.log("맥 사용 시: 시스템 설정 > 개인정보 보호 및 보안 >")
            self.log("  '손쉬운 사용' 에 이 앱(또는 터미널)을 허용해야 합니다.")

        root.protocol("WM_DELETE_WINDOW", self.on_close)

    # ---------- 유틸 ----------
    def log(self, msg: str) -> None:
        """상태 창에 한 줄 기록한다. (스레드에서 호출 안전)"""
        def _append() -> None:
            self.status.config(state="normal")
            self.status.insert("end", f"{time.strftime('%H:%M:%S')}  {msg}\n")
            self.status.see("end")
            self.status.config(state="disabled")
        self.root.after(0, _append)

    # ---------- 좌표 캡처 ----------
    def capture_position(self) -> None:
        """3초 카운트다운 후 현재 마우스 위치를 클릭 좌표로 저장한다."""
        if not _PYAUTO_OK:
            return
        self.start_btn.config(state="disabled")

        def countdown(n: int) -> None:
            if n > 0:
                self.pos_label.config(text=f"{n}초 후 현재 마우스 위치 저장…", foreground="#e67e22")
                self.root.after(1000, countdown, n - 1)
            else:
                x, y = pyautogui.position()
                self.click_x, self.click_y = int(x), int(y)
                self.pos_label.config(text=f"지정됨:  X={x}, Y={y}", foreground="#27ae60")
                self.log(f"클릭 위치 지정: ({x}, {y})")
                self.start_btn.config(state="normal")

        countdown(3)

    # ---------- 실행 ----------
    def start(self) -> None:
        """입력값을 검증하고 매크로 스레드를 시작한다."""
        if self.click_x is None:
            messagebox.showwarning("확인", "먼저 ① 에서 클릭할 위치를 지정하세요.")
            return
        try:
            interval = float(self.interval_var.get())
            wait = float(self.wait_var.get())
            if interval <= 0 or wait < 0:
                raise ValueError
        except ValueError:
            messagebox.showwarning("확인", "주기와 대기 시간은 0보다 큰 숫자로 입력하세요.")
            return

        keys = KEY_CHOICES[self.key_var.get()]
        self._stop.clear()
        self._count = 0
        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self.log(f"시작 — {interval}초마다 새로고침 후 ({self.click_x}, {self.click_y}) 클릭")

        self._worker = threading.Thread(
            target=self._loop, args=(interval, wait, keys), daemon=True
        )
        self._worker.start()

    def _loop(self, interval: float, wait: float, keys: list[str]) -> None:
        """새로고침 → 대기 → 클릭 반복 (드리프트 방지 스케줄링)."""
        key_label = "+".join(keys)
        next_run = time.perf_counter()
        try:
            while not self._stop.is_set():
                self._count += 1
                if len(keys) == 1:
                    pyautogui.press(keys[0])
                else:
                    pyautogui.hotkey(*keys)
                self.log(f"[{self._count}] 새로고침({key_label})")

                if self._stop.wait(timeout=wait):
                    break
                pyautogui.click(x=self.click_x, y=self.click_y)
                self.log(f"[{self._count}] 클릭 ({self.click_x}, {self.click_y})")

                next_run += interval
                sleep_for = next_run - time.perf_counter()
                if sleep_for < 0:
                    next_run = time.perf_counter()
                    continue
                if self._stop.wait(timeout=sleep_for):
                    break
        except pyautogui.FailSafeException:
            self.log("마우스 모서리 감지 — 정지합니다.")
        except Exception as exc:  # noqa: BLE001
            self.log(f"오류로 정지: {exc}")
        finally:
            self.root.after(0, self._reset_buttons)

    def _reset_buttons(self) -> None:
        self.start_btn.config(state="normal")
        self.stop_btn.config(state="disabled")

    def stop(self) -> None:
        """매크로를 정지한다."""
        self._stop.set()
        self.log("정지 요청됨.")

    def on_close(self) -> None:
        self._stop.set()
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    MacroApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
