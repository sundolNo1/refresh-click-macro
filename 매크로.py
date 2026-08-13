#!/usr/bin/env python3
"""주기적 새로고침 + 좌표 클릭 매크로 (버튼형 GUI).

명령어를 몰라도 창에서 버튼만 누르면 되는 쉬운 버전.
"""
from __future__ import annotations

import calendar
import hashlib
import hmac
import json
import math
import os
import platform
import socket
import struct
import subprocess
import threading
import time
import tkinter as tk
import urllib.request
from tkinter import messagebox, ttk

# 한국 표준시(KST) = UTC + 9시간
KST_OFFSET = 9 * 3600

# ===== 사용 기한 =====
# 2026년 10월 30일 00시 00분 (KST) 이 되면 만료.
EXPIRY_TEXT = "2026-10-30 00:00"
EXPIRY_UNIX = calendar.timegm((2026, 10, 30, 0, 0, 0)) - KST_OFFSET
# NTP 시간 서버 목록 (앞에서부터 시도)
NTP_SERVERS = ("time.google.com", "time.windows.com", "kr.pool.ntp.org", "pool.ntp.org")
# NTP(UDP 123)가 막힌 환경을 위한 예비 수단: HTTPS 응답의 Date 헤더
HTTP_TIME_URLS = ("https://www.google.com/generate_204", "https://www.cloudflare.com/")

# 사용 기록 파일 — 시계 되돌리기를 막기 위해 '지금까지 본 가장 늦은 시각'을 저장한다.
STATE_KEY = b"refresh-click-macro/v1/state"
STATE_DIRNAME = ".rcmacro"
STATE_FILENAME = "state.dat"
# 저장된 시각보다 이만큼 넘게 과거로 돌아가면 시계 조작으로 본다.
ROLLBACK_TOLERANCE = 300.0  # 초

APP_TITLE = "자동 새로고침 · 클릭 매크로"
# ⑥ 상태창 줄 수. 화면이 낮으면 _fit_window() 가 MIN 까지 자동으로 줄인다.
STATUS_ROWS = 8
STATUS_ROWS_MIN = 4


def parse_seconds(text: str) -> float:
    """시간 입력을 초(float)로 변환한다.

    콜론 형식은 '초:백분의일초' 로 해석한다.
      - "59:85" → 59.85초
      - "0:05"  → 0.05초
    콜론이 없으면 일반 숫자로 본다. ("59.85", "60")
    """
    text = text.strip()
    if ":" in text:
        left, right = text.split(":", 1)
        whole = int(left.strip() or "0")
        # 콜론 뒤는 백분의 일초(2자리) 로 취급: "85"→0.85, "05"→0.05
        frac = int(right.strip() or "0") / 100.0
        if whole < 0 or frac < 0:
            raise ValueError
        return whole + frac
    return float(text)


def align_grid(interval: float) -> float:
    """주기를 '00초에 떨어지는 값'으로 보정한다.

    새로고침이 매번 분의 00초(또는 그 약수 격자)에 오도록,
    입력한 주기보다 절대 빨라지지 않는 선에서 가장 가까운 값을 고른다.
      59.85 → 60.0    45 → 60.0    40 → 60.0
      30    → 30.0    20 → 20.0    90 → 120.0
    """
    if interval <= 0:
        return 60.0
    if interval > 60.0:
        # 1분 넘는 주기: 분 단위로 올림 (2분마다 00초, 3분마다 00초 …)
        return math.ceil(interval / 60.0) * 60.0
    # 1분 이하: 60을 나눠떨어지게 하는 격자 중 주기보다 같거나 큰 것
    return 60.0 / math.floor(60.0 / interval)


# 격자보다 이만큼(초)까지 짧은 주기는 '일부러 앞당긴 값'으로 보고 그 차이를 유지한다.
LEAD_MAX = 2.0


def next_grid_target(now: float, grid: float, lead: float) -> float:
    """now 이후에 오는 첫 발사 시각. 격자(00초)보다 lead 초 앞선 지점들이 대상이다.

    grid=60, lead=0.15 이면 …:59.85 시각들 중 now 다음 것을 돌려준다.
    """
    return math.ceil((now + lead) / grid) * grid - lead


def fetch_ntp_offset(timeout: float = 3.0) -> float | None:
    """NTP 서버에서 정확한 시각을 받아 (표준시각 - 내컴퓨터시각) 오차(초)를 반환.

    성공 시 오차(초, 양수면 내 시계가 느림), 실패 시 None.
    표준 라이브러리만 사용한다.
    """
    ntp_delta = 2208988800  # 1900-01-01 → 1970-01-01 초 차이
    packet = b"\x1b" + 47 * b"\0"
    for host in NTP_SERVERS:
        sock = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(timeout)
            t0 = time.time()
            sock.sendto(packet, (host, 123))
            data, _ = sock.recvfrom(48)
            t3 = time.time()
            secs = struct.unpack("!I", data[40:44])[0] - ntp_delta
            frac = struct.unpack("!I", data[44:48])[0] / 2 ** 32
            server_time = secs + frac
            # 왕복 지연의 절반을 보정
            offset = server_time + (t3 - t0) / 2 - t3
            return offset
        except Exception:
            continue
        finally:
            if sock is not None:
                sock.close()
    return None


def fetch_http_offset(timeout: float = 4.0) -> float | None:
    """HTTPS 응답의 Date 헤더로 표준시각 오차(초)를 구한다. NTP가 막혔을 때의 예비 수단.

    초 단위까지만 정확하지만, 만료 판정(날짜 단위)에는 충분하다.
    """
    import email.utils

    for url in HTTP_TIME_URLS:
        try:
            req = urllib.request.Request(url, method="HEAD")
            t0 = time.time()
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                date_header = resp.headers.get("Date")
            t3 = time.time()
            if not date_header:
                continue
            server_time = email.utils.parsedate_to_datetime(date_header).timestamp()
            return server_time + (t3 - t0) / 2 - t3
        except Exception:
            continue
    return None


def fetch_trusted_offset() -> float | None:
    """인터넷에서 표준시각 오차를 구한다. NTP 먼저, 실패하면 HTTPS Date.

    둘 다 실패하면 None — 이때는 '시각을 확인할 수 없음'으로 보고 실행을 막는다.
    """
    offset = fetch_ntp_offset()
    if offset is None:
        offset = fetch_http_offset()
    return offset


# ---------- 사용 기록(시계 되돌리기 방지) ----------
def _state_path() -> str:
    """사용 기록 파일 경로. 윈도우는 LOCALAPPDATA, 그 외는 홈 디렉터리."""
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, STATE_DIRNAME, STATE_FILENAME)


def _sign(payload: str) -> str:
    """기록 파일 위조를 걸러내기 위한 서명."""
    return hmac.new(STATE_KEY, payload.encode("utf-8"), hashlib.sha256).hexdigest()


def load_state() -> dict:
    """저장된 사용 기록을 읽는다.

    없으면 빈 기록, 손상/위조되었으면 {"tampered": True} 를 돌려준다.
    """
    try:
        with open(_state_path(), "r", encoding="utf-8") as fp:
            raw = json.load(fp)
        payload, sig = raw["payload"], raw["sig"]
        if not hmac.compare_digest(_sign(payload), sig):
            return {"tampered": True}
        return json.loads(payload)
    except FileNotFoundError:
        return {}
    except Exception:
        return {"tampered": True}


def save_state(state: dict) -> None:
    """사용 기록을 서명과 함께 저장한다. 실패해도 프로그램은 계속 돈다."""
    try:
        path = _state_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        payload = json.dumps(state, sort_keys=True)
        with open(path, "w", encoding="utf-8") as fp:
            json.dump({"payload": payload, "sig": _sign(payload)}, fp)
    except Exception:
        pass


try:
    import pyautogui
    pyautogui.PAUSE = 0
    pyautogui.FAILSAFE = True
    _PYAUTO_OK = True
except Exception:  # pyautogui 미설치/환경 문제
    _PYAUTO_OK = False

try:
    from PIL import ImageChops, ImageStat
    _PIL_OK = True
except Exception:  # Pillow 미설치
    _PIL_OK = False

IS_MAC = platform.system() == "Darwin"
IS_WINDOWS = platform.system() == "Windows"

# ===== 보안문자 감시 =====
# 감시 영역을 이 크기(흑백)로 줄여서 비교한다. 작을수록 빠르고 잔떨림에 둔감.
WATCH_SIZE = (160, 120)
WATCH_PERIOD = 0.4      # 감시 간격(초)
WATCH_HITS = 2          # 이만큼 연속으로 달라져야 '떴다'고 판단 (깜빡임 오탐 방지)
ALERT_BEEPS = 12        # 알림음 최대 반복 횟수
ALERT_GAP = 1.2         # 알림음 간격(초)


def grab_region(region: tuple[int, int, int, int]):
    """화면의 지정 영역을 찍어 비교용 흑백 축소 이미지로 돌려준다."""
    shot = pyautogui.screenshot(region=region)
    return shot.convert("L").resize(WATCH_SIZE)


def image_diff(a, b) -> float:
    """두 비교용 이미지의 평균 밝기 차이를 0~100 으로 돌려준다."""
    stat = ImageStat.Stat(ImageChops.difference(a, b))
    return stat.mean[0] / 255.0 * 100.0


def play_alert_sound() -> None:
    """알림음을 한 번 낸다. (실패해도 조용히 넘어간다)"""
    try:
        if IS_MAC:
            subprocess.Popen(
                ["afplay", "/System/Library/Sounds/Glass.aiff"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        elif IS_WINDOWS:
            import winsound
            winsound.Beep(1200, 300)
            winsound.Beep(900, 300)
        else:
            print("\a", end="", flush=True)
    except Exception:
        pass

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
        # 각 창: (선택용 좌표, 클릭할 버튼 좌표)
        self.windows: list[tuple[tuple[int, int], tuple[int, int]]] = []
        self._stop = threading.Event()
        self._worker: threading.Thread | None = None
        self._count = 0
        self.time_offset = 0.0   # 표준시각 - 내컴퓨터시각 (초)
        self.synced = False
        self._expired = False

        # 보안문자 감시
        self.watch_region: tuple[int, int, int, int] | None = None
        self.watch_baseline = None          # 보안문자가 없을 때의 기준 화면
        self._watch_ready = threading.Event()  # 새로고침/클릭 중에는 감시를 쉰다
        self._alert_off = threading.Event()     # 알림음 중단 신호

        root.title(APP_TITLE)
        # 크기 고정은 위젯을 다 만든 뒤(_fit_window)에 한다.
        # 여기서 먼저 고정하면 내용 계산 전 크기에 묶여 아래·오른쪽이 잘린다.

        pad = {"padx": 14, "pady": 6}

        # 1. 클릭할 창 목록 (창마다 선택용 + 클릭용 좌표 2개)
        frm_pos = ttk.LabelFrame(root, text="① 클릭할 창 목록 (여러 창 동시 처리)")
        frm_pos.grid(row=0, column=0, sticky="ew", **pad)
        self.win_list = tk.Listbox(frm_pos, height=4, width=44)
        self.win_list.grid(row=0, column=0, rowspan=3, padx=(10, 6), pady=8)
        self.add_btn = ttk.Button(frm_pos, text="＋ 창 추가", command=self.capture_window)
        self.add_btn.grid(row=0, column=1, padx=6, pady=(8, 2), sticky="ew")
        ttk.Button(frm_pos, text="마지막 삭제", command=self.remove_last_window).grid(
            row=1, column=1, padx=6, pady=2, sticky="ew"
        )
        ttk.Button(frm_pos, text="모두 지우기", command=self.clear_windows).grid(
            row=2, column=1, padx=6, pady=2, sticky="ew"
        )
        self.capture_hint = ttk.Label(
            frm_pos,
            text="＋창 추가 → ①선택용 빈 곳 → ②클릭할 버튼 순으로 각각 3초 안에 마우스를 올려두세요.",
            foreground="#555", wraplength=420, justify="left",
        )
        self.capture_hint.grid(row=3, column=0, columnspan=2, padx=10, pady=(0, 8), sticky="w")

        # 2. 설정
        frm_set = ttk.LabelFrame(root, text="② 설정")
        frm_set.grid(row=1, column=0, sticky="ew", **pad)

        ttk.Label(frm_set, text="몇 초마다 새로고침  (예: 59:85 = 59.85초)").grid(row=0, column=0, padx=10, pady=8, sticky="w")
        self.interval_var = tk.StringVar(value="60.00")
        ttk.Spinbox(
            frm_set, from_=0.01, to=86400, increment=0.01, format="%.2f",
            textvariable=self.interval_var, width=10,
        ).grid(row=0, column=1, sticky="w")
        ttk.Label(frm_set, text="초").grid(row=0, column=2, sticky="w")

        ttk.Label(frm_set, text="새로고침 방식").grid(row=1, column=0, padx=10, pady=8, sticky="w")
        self.key_var = tk.StringVar(value=DEFAULT_KEY)
        ttk.Combobox(
            frm_set, textvariable=self.key_var, values=list(KEY_CHOICES.keys()),
            state="readonly", width=26,
        ).grid(row=1, column=1, columnspan=2, padx=(0, 10), sticky="w")

        ttk.Label(frm_set, text="새로고침 후 클릭까지 대기  (예: 0:05 = 0.05초)").grid(row=2, column=0, padx=10, pady=8, sticky="w")
        self.wait_var = tk.StringVar(value="2.00")
        ttk.Spinbox(
            frm_set, from_=0.0, to=86400, increment=0.01, format="%.2f",
            textvariable=self.wait_var, width=10,
        ).grid(row=2, column=1, sticky="w")
        ttk.Label(frm_set, text="초").grid(row=2, column=2, sticky="w")

        ttk.Label(frm_set, text="클릭 방식").grid(row=3, column=0, padx=10, pady=8, sticky="w")
        self.click_mode_var = tk.StringVar(value="싱글 클릭")
        ttk.Combobox(
            frm_set, textvariable=self.click_mode_var, values=["싱글 클릭", "더블 클릭"],
            state="readonly", width=12,
        ).grid(row=3, column=1, columnspan=2, sticky="w")

        # 3. 인터넷 표준시각(KST) 동기화
        frm_time = ttk.LabelFrame(root, text="③ 인터넷 표준시각 (KST)")
        frm_time.grid(row=2, column=0, sticky="ew", **pad)
        self.clock_label = ttk.Label(frm_time, text="현재 시각 --:--:--", font=("", 14))
        self.clock_label.grid(row=0, column=0, padx=10, pady=(8, 2), sticky="w")
        ttk.Button(frm_time, text="표준시각 동기화", command=self.sync_time).grid(
            row=0, column=1, padx=10, pady=8
        )
        # 시작하자마자 _startup_expiry_check() 가 확인하므로 '확인 중' 으로 시작한다.
        self.sync_label = ttk.Label(frm_time, text="표준시각 확인 중… 잠시만요", foreground="#e67e22")
        self.sync_label.grid(row=1, column=0, columnspan=2, padx=10, pady=(0, 4), sticky="w")
        self.align_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            frm_time, text="표준시각 00초에 맞추기 (예: 59.85 → 매분 59.85초)",
            variable=self.align_var,
        ).grid(row=2, column=0, columnspan=2, padx=10, pady=(0, 8), sticky="w")

        # 4. 보안문자 감시 (뜨면 알려주기 — 입력은 직접)
        frm_watch = ttk.LabelFrame(root, text="④ 보안문자 감시 (뜨면 알림 · 입력은 직접)")
        frm_watch.grid(row=3, column=0, sticky="ew", **pad)

        # 접기/펼치기 — 안 쓸 때는 접어서 창을 짧게 쓸 수 있다. (접어도 설정·동작은 그대로)
        self.watch_open = True
        self.watch_toggle = ttk.Button(
            frm_watch, text="▼ 접기", width=9, command=self.toggle_watch,
        )
        self.watch_toggle.grid(row=0, column=0, padx=10, pady=(6, 2), sticky="w")

        body = ttk.Frame(frm_watch)
        body.grid(row=1, column=0, sticky="ew")
        self.watch_body = body

        self.watch_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            body, text="보안문자 입력란이 뜨면 소리로 알림", variable=self.watch_var,
        ).grid(row=0, column=0, columnspan=3, padx=10, pady=(2, 2), sticky="w")

        ttk.Button(body, text="① 감시 영역 지정", command=self.capture_watch_region).grid(
            row=1, column=0, padx=(10, 4), pady=4, sticky="ew"
        )
        ttk.Button(body, text="② 기준 화면 저장", command=self.capture_watch_baseline).grid(
            row=1, column=1, padx=4, pady=4, sticky="ew"
        )
        ttk.Label(body, text="민감도").grid(row=1, column=2, padx=(8, 2), sticky="e")
        self.watch_sens_var = tk.StringVar(value="8")
        ttk.Spinbox(
            body, from_=1, to=60, increment=1, textvariable=self.watch_sens_var, width=5,
        ).grid(row=1, column=3, padx=(0, 10), sticky="w")

        self.watch_hint = ttk.Label(
            body,
            text="①영역: 보안문자가 나타나는 자리의 [왼쪽 위]→[오른쪽 아래] 모서리를 각 3초 안에 지정.\n"
                 "②기준: 보안문자가 '없는' 평소 화면 상태에서 눌러 저장하세요.",
            foreground="#555", wraplength=420, justify="left",
        )
        self.watch_hint.grid(row=2, column=0, columnspan=4, padx=10, pady=(2, 4), sticky="w")

        self.watch_pause_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            body, text="알림이 울리면 매크로 자동 정지 (새로고침으로 화면이 사라지지 않게)",
            variable=self.watch_pause_var,
        ).grid(row=3, column=0, columnspan=4, padx=10, pady=(0, 8), sticky="w")

        # 5. 시작 / 정지
        frm_run = ttk.Frame(root)
        frm_run.grid(row=4, column=0, sticky="ew", **pad)
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

        # 6. 상태
        self.status = tk.Text(
            root, height=STATUS_ROWS, width=46, state="disabled", bg="#1e1e1e", fg="#dcdcdc"
        )
        self.status.grid(row=5, column=0, padx=14, pady=(4, 4))
        ttk.Label(
            root, text="정지: [정지] 버튼 · 또는 마우스를 화면 맨 왼쪽 위 모서리로",
            foreground="#555",
        ).grid(row=6, column=0, pady=(0, 4))
        self.expiry_label = ttk.Label(
            root, text=f"사용 가능 기간: ~{EXPIRY_TEXT} (KST) · 기간 확인 중…", foreground="#888"
        )
        self.expiry_label.grid(row=7, column=0, pady=(0, 10))

        if not _PYAUTO_OK:
            self.log("⚠ pyautogui 가 설치되지 않아 동작할 수 없습니다.")
            self.log("   터미널에서:  pip install pyautogui")
            self.start_btn.config(state="disabled")
        elif IS_MAC:
            self.log("맥 사용 시: 시스템 설정 > 개인정보 보호 및 보안 >")
            self.log("  '손쉬운 사용' 에 이 앱(또는 터미널)을 허용해야 합니다.")
        if _PYAUTO_OK and not _PIL_OK:
            self.log("⚠ Pillow 가 없어 보안문자 감시는 쓸 수 없습니다. (pip install pillow)")

        # 모든 위젯을 올린 뒤 실제로 필요한 크기를 계산해 창을 그 크기로 맞춘다.
        # (이 계산 전에 크기를 고정하면 아래쪽 ③④⑤ 영역이 잘려 보인다)
        # 기본은 ④ 보안문자 감시를 접은 채로 시작한다. (필요할 때 펼쳐 쓰면 된다)
        # toggle_watch() 안에서 _fit_window() 가 불려 창 크기도 함께 맞춰진다.
        self.toggle_watch()

        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self._tick()  # 실시간 시계 시작
        self._startup_expiry_check()  # 사용 기한 확인 (인터넷 시간 기준)
        self._schedule_recheck()  # 켜 둔 채로 만료일이 지나가는 경우 대비

    def toggle_watch(self) -> None:
        """④ 보안문자 감시 영역을 접거나 편다. (설정값과 감시 동작에는 영향 없음)"""
        if self.watch_open:
            self.watch_body.grid_remove()
            self.watch_toggle.config(text="▶ 펼치기")
        else:
            self.watch_body.grid()
            self.watch_toggle.config(text="▼ 접기")
        self.watch_open = not self.watch_open
        self._fit_window()

    def _fit_window(self) -> None:
        """내용이 전부 보이도록 창 크기를 맞춘다. 화면보다 크면 화면 안으로 줄인다."""
        root = self.root
        # 이전에 걸어 둔 최소 크기·크기 고정을 먼저 풀어야 접었을 때 다시 줄어든다.
        root.resizable(True, True)
        root.minsize(1, 1)
        # 상태창은 늘 기본 줄 수에서 시작한다. (접었다 펴며 줄었던 것을 되돌린다)
        self.status.config(height=STATUS_ROWS)
        root.update_idletasks()

        # 화면이 낮아 다 안 들어가면 ⑥ 상태창 줄 수부터 줄인다.
        # (그냥 잘라 내면 맨 아래 '사용 가능 기간' 줄이 화면 밖으로 밀린다)
        # 쓸 수 있는 높이는 화면 높이에서 빼는 식으로는 알 수 없다.
        # (맥은 독·메뉴막대, 윈도우는 작업 표시줄만큼 창을 알아서 깎는다)
        # 그래서 원하는 크기를 한 번 요청해 보고 실제로 받은 높이를 기준으로 삼는다.
        rows = STATUS_ROWS
        for _ in range(STATUS_ROWS - STATUS_ROWS_MIN + 1):
            w, h = root.winfo_reqwidth(), root.winfo_reqheight()
            root.geometry(f"{w}x{h}")
            root.update()
            got = root.winfo_height()
            if got >= h or got < 100 or rows <= STATUS_ROWS_MIN:
                break  # 다 들어갔다 (또는 더 줄일 수 없다 / 아직 창이 안 떴다)
            rows -= 1
            self.status.config(height=rows)
            root.update_idletasks()

        w, h = root.winfo_reqwidth(), root.winfo_reqheight()
        if root.winfo_height() > 100:
            h = min(h, root.winfo_height())
        root.geometry(f"{w}x{h}")
        root.minsize(w, h)
        # 내용에 맞는 크기를 잡은 뒤에 고정한다 (기존과 같은 '크기 고정' 창).
        root.resizable(False, h < root.winfo_reqheight())

    # ---------- 사용 기한 ----------
    def _startup_expiry_check(self) -> None:
        """시작 시 인터넷 표준시각을 받아 만료 여부를 확인한다.

        인터넷으로 시각을 확인하지 못하면 실행을 막는다. (컴퓨터 시계는 믿지 않는다)
        """
        self.start_btn.config(state="disabled")
        self._verify_expiry_async(first_run=True)

    def _verify_expiry_async(self, first_run: bool = False) -> None:
        """백그라운드에서 인터넷 표준시각을 다시 받아 만료 여부를 재확인한다."""
        def worker() -> None:
            offset = fetch_trusted_offset()

            def apply() -> None:
                self._show_sync(offset)  # ③ 줄도 같이 갱신 (버튼을 안 눌러도)
                if offset is None:
                    self.synced = False
                    self._block("인터넷 연결이 필요합니다 (시각 확인 실패)")
                    if first_run:
                        self.log("인터넷으로 현재 시각을 확인하지 못했습니다.")
                        self.log("  연결을 확인한 뒤 [표준시각 동기화] 를 눌러주세요.")
                    return
                self.time_offset = offset
                self.synced = True
                self._evaluate_expiry()
            self.root.after(0, apply)

        threading.Thread(target=worker, daemon=True).start()

    def _schedule_recheck(self) -> None:
        """10분마다 만료 여부를 다시 확인한다. (오래 켜 두는 경우 대비)"""
        def again() -> None:
            self._verify_expiry_async()
            self.root.after(600_000, again)
        self.root.after(600_000, again)

    def _show_sync(self, offset: float | None) -> None:
        """③ 표준시각 줄을 갱신한다.

        시작할 때 자동으로 하는 확인과 [표준시각 동기화] 버튼이 모두 이리로 온다.
        (예전에는 버튼을 눌렀을 때만 갱신돼서, 자동 확인이 성공했는데도
         '동기화 안 됨' 이 빨간 글씨로 남아 있었다)
        """
        if offset is None:
            self.sync_label.config(
                text="동기화 실패 — 인터넷 연결을 확인하세요", foreground="#c0392b"
            )
        else:
            self.sync_label.config(
                text=f"동기화 완료 · 내 시계와 오차 {offset:+.3f}초", foreground="#27ae60"
            )

    def _show_expiry(self, text: str, color: str) -> None:
        """기간 안내를 맨 아래 줄과 제목 표시줄에 함께 쓴다.

        화면이 낮은 컴퓨터에서는 맨 아래 줄이 창 밖으로 밀릴 수 있어서,
        제목 표시줄에도 같은 내용을 남겨 둔다. (제목은 창 크기에 영향 없음)
        """
        self.expiry_label.config(text=text, foreground=color)
        self.root.title(f"{APP_TITLE} — {text}")

    def _block(self, reason: str) -> None:
        """실행을 막고, 돌고 있으면 정지시킨 뒤 사유를 화면에 표시한다."""
        self._expired = True
        self.start_btn.config(state="disabled")
        self._show_expiry(reason, "#c0392b")
        if not self._stop.is_set():
            self._stop.set()

    def _evaluate_expiry(self) -> None:
        """확인된 표준시각으로 만료 여부를 판정하고 화면을 갱신한다."""
        if not self.synced:
            self._block("인터넷 연결이 필요합니다 (시각 확인 실패)")
            return

        state = load_state()
        if state.get("tampered") or state.get("expired"):
            self._block(f"사용할 수 없습니다 (만료일 {EXPIRY_TEXT} KST)")
            self.log("사용 기록이 만료 상태이거나 손상되었습니다.")
            return

        now = self.accurate_time()
        max_seen = state.get("max_seen", 0.0)
        if now < max_seen - ROLLBACK_TOLERANCE:
            # 이전에 확인했던 시각보다 과거 → 시계/시간서버 조작으로 본다.
            save_state({"max_seen": max_seen, "expired": True})
            self._block("시각이 비정상입니다 — 사용할 수 없습니다")
            self.log("이전 실행보다 시각이 과거로 되돌아갔습니다. 사용이 중지됩니다.")
            return

        if now >= EXPIRY_UNIX:
            save_state({"max_seen": max(now, max_seen), "expired": True})
            self._block(f"사용 기간이 만료되었습니다 (만료일 {EXPIRY_TEXT} KST)")
            self.log(f"사용 기간 만료됨 — 인터넷 표준시각 기준. (만료일 {EXPIRY_TEXT})")
            return

        save_state({"max_seen": max(now, max_seen), "expired": False})
        self._expired = False
        days_left = (EXPIRY_UNIX - now) / 86400
        self._show_expiry(
            f"사용 가능 기간: ~{EXPIRY_TEXT} (KST) · 약 {days_left:.1f}일 남음", "#888"
        )
        if _PYAUTO_OK:
            self.start_btn.config(state="normal")

    # ---------- 표준시각 ----------
    def accurate_time(self) -> float:
        """표준시각 기준의 현재 유닉스 시각(초). 동기화 안 됐으면 내 컴퓨터 시각."""
        return time.time() + self.time_offset

    def _fmt_kst(self, unix_time: float) -> str:
        """유닉스 시각을 KST 시:분:초.백분의일 문자열로."""
        kst = time.gmtime(unix_time + KST_OFFSET)
        hundredths = int((unix_time % 1) * 100)
        return time.strftime("%H:%M:%S", kst) + f".{hundredths:02d}"

    def _tick(self) -> None:
        """0.05초마다 현재 표준시각을 화면에 갱신한다."""
        self.clock_label.config(text=f"현재 시각 {self._fmt_kst(self.accurate_time())}")
        self.root.after(50, self._tick)

    def sync_time(self) -> None:
        """NTP 서버에서 표준시각을 받아 오차를 보정한다. (백그라운드 스레드)"""
        self.sync_label.config(text="동기화 중… 잠시만요", foreground="#e67e22")

        def worker() -> None:
            offset = fetch_trusted_offset()

            def apply() -> None:
                self._show_sync(offset)
                if offset is None:
                    self.synced = False
                    self.log("표준시각 동기화 실패 — 인터넷 연결을 확인하세요.")
                    self._block("인터넷 연결이 필요합니다 (시각 확인 실패)")
                else:
                    self.time_offset = offset
                    self.synced = True
                    self.log(f"표준시각 동기화 완료 (오차 {offset:+.3f}초)")
                    self._evaluate_expiry()
            self.root.after(0, apply)

        threading.Thread(target=worker, daemon=True).start()

    # ---------- 유틸 ----------
    def log(self, msg: str) -> None:
        """상태 창에 한 줄 기록한다. (스레드에서 호출 안전)"""
        def _append() -> None:
            stamp = self._fmt_kst(self.accurate_time())
            self.status.config(state="normal")
            self.status.insert("end", f"{stamp}  {msg}\n")
            self.status.see("end")
            self.status.config(state="disabled")
        self.root.after(0, _append)

    # ---------- 창 좌표 캡처 ----------
    def _refresh_win_list(self) -> None:
        """목록 상자를 현재 창 목록으로 다시 그린다."""
        self.win_list.delete(0, "end")
        for i, (focus, target) in enumerate(self.windows, 1):
            self.win_list.insert(
                "end", f"{i}번 창  선택{focus} → 클릭{target}"
            )

    def capture_window(self) -> None:
        """3초씩 두 번 카운트다운하며 [선택용]→[클릭용] 좌표를 캡처해 창 하나를 추가한다."""
        if not _PYAUTO_OK:
            return
        self.add_btn.config(state="disabled")
        self.start_btn.config(state="disabled")
        captured: list[tuple[int, int]] = []
        steps = [
            ("① 선택용 빈 곳", "#e67e22"),
            ("② 클릭할 버튼", "#2980b9"),
        ]

        def do_step(step: int, n: int) -> None:
            name, color = steps[step]
            if n > 0:
                self.capture_hint.config(text=f"{name} 위에 마우스를 올리세요…  {n}", foreground=color)
                self.root.after(1000, do_step, step, n - 1)
                return
            x, y = pyautogui.position()
            captured.append((int(x), int(y)))
            self.log(f"{name} 좌표: ({x}, {y})")
            if step + 1 < len(steps):
                self.root.after(300, do_step, step + 1, 3)
            else:
                self.windows.append((captured[0], captured[1]))
                self._refresh_win_list()
                self.capture_hint.config(
                    text=f"{len(self.windows)}번 창 추가됨! 다른 창도 ＋창 추가로 등록하세요.",
                    foreground="#27ae60",
                )
                self.add_btn.config(state="normal")
                self.start_btn.config(state="normal")

        do_step(0, 3)

    def remove_last_window(self) -> None:
        """마지막으로 추가한 창을 목록에서 뺀다."""
        if self.windows:
            self.windows.pop()
            self._refresh_win_list()
            self.log("마지막 창 삭제")

    def clear_windows(self) -> None:
        """창 목록을 모두 비운다."""
        self.windows.clear()
        self._refresh_win_list()
        self.log("창 목록 모두 지움")

    # ---------- 보안문자 감시 ----------
    def capture_watch_region(self) -> None:
        """[왼쪽 위]→[오른쪽 아래] 모서리를 3초씩 카운트다운하며 감시 영역으로 지정한다."""
        if not _PYAUTO_OK:
            return
        corners: list[tuple[int, int]] = []
        steps = [("① 왼쪽 위 모서리", "#e67e22"), ("② 오른쪽 아래 모서리", "#2980b9")]

        def do_step(step: int, n: int) -> None:
            name, color = steps[step]
            if n > 0:
                self.watch_hint.config(text=f"{name} 에 마우스를 올리세요…  {n}", foreground=color)
                self.root.after(1000, do_step, step, n - 1)
                return
            x, y = pyautogui.position()
            corners.append((int(x), int(y)))
            if step + 1 < len(steps):
                self.root.after(300, do_step, step + 1, 3)
                return
            (x1, y1), (x2, y2) = corners
            left, top = min(x1, x2), min(y1, y2)
            width, height = abs(x2 - x1), abs(y2 - y1)
            if width < 10 or height < 10:
                self.watch_hint.config(
                    text="영역이 너무 좁습니다. 두 모서리를 더 넓게 잡아 다시 지정하세요.",
                    foreground="#c0392b",
                )
                return
            self.watch_region = (left, top, width, height)
            self.watch_baseline = None
            self.watch_hint.config(
                text=f"감시 영역 지정됨 ({width}×{height}). 이제 보안문자가 없는 상태에서 "
                     "[② 기준 화면 저장] 을 누르세요.",
                foreground="#27ae60",
            )
            self.log(f"감시 영역: ({left}, {top}) 크기 {width}×{height}")

        do_step(0, 3)

    def capture_watch_baseline(self) -> None:
        """지금 화면을 '보안문자가 없는 평소 상태' 기준으로 저장한다."""
        if not _PYAUTO_OK or not _PIL_OK:
            return
        if self.watch_region is None:
            self.watch_hint.config(text="먼저 [① 감시 영역 지정] 을 해주세요.", foreground="#c0392b")
            return
        try:
            self.watch_baseline = grab_region(self.watch_region)
        except Exception as exc:  # noqa: BLE001
            self.watch_hint.config(text=f"기준 화면 저장 실패: {exc}", foreground="#c0392b")
            return
        self.watch_hint.config(
            text="기준 화면 저장 완료. 이 화면과 달라지면 알림이 울립니다.", foreground="#27ae60"
        )
        self.log("보안문자 감시 기준 화면 저장됨")

    def _watch_loop(self, region: tuple[int, int, int, int], baseline, threshold: float) -> None:
        """감시 영역이 기준 화면과 달라지면 알린다. (매크로와 함께 도는 감시 스레드)"""
        hits = 0
        while not self._stop.is_set():
            # 새로고침/클릭 동작 중에는 화면이 요동치므로 감시를 쉰다.
            if not self._watch_ready.is_set():
                if self._stop.wait(timeout=0.2):
                    return
                hits = 0
                continue
            try:
                diff = image_diff(baseline, grab_region(region))
            except Exception as exc:  # noqa: BLE001
                self.log(f"감시 중 오류: {exc}")
                return
            hits = hits + 1 if diff >= threshold else 0
            if hits >= WATCH_HITS:
                self.root.after(0, self._captcha_alert, diff)
                return
            if self._stop.wait(timeout=WATCH_PERIOD):
                return

    def _captcha_alert(self, diff: float) -> None:
        """보안문자로 보이는 변화를 감지했을 때: 소리 + 창 띄우기 (+ 자동 정지)."""
        self.log(f"⚠ 보안문자로 보이는 화면 변화 감지 (차이 {diff:.1f})")
        if self.watch_pause_var.get() and not self._stop.is_set():
            self._stop.set()
            self.log("매크로 자동 정지 — 보안문자를 직접 입력하세요.")

        # 창을 앞으로 끌어올려 눈에 띄게
        try:
            self.root.deiconify()
            self.root.lift()
            self.root.attributes("-topmost", True)
            self.root.after(3000, lambda: self.root.attributes("-topmost", False))
        except Exception:
            pass

        # 알림음 반복 (정지 버튼을 누르거나 횟수를 다 채우면 멈춘다)
        self._alert_off.clear()

        def beeper() -> None:
            for _ in range(ALERT_BEEPS):
                if self._alert_off.is_set():
                    return
                play_alert_sound()
                if self._alert_off.wait(timeout=ALERT_GAP):
                    return

        threading.Thread(target=beeper, daemon=True).start()
        messagebox.showinfo(
            "보안문자 확인",
            "보안문자 입력란이 뜬 것 같습니다.\n브라우저 창에서 직접 입력해 주세요.",
        )
        self._alert_off.set()

    # ---------- 실행 ----------
    def start(self) -> None:
        """입력값을 검증하고 매크로 스레드를 시작한다."""
        # 실행 순간에도 만료를 한 번 더 확인 (반드시 인터넷 표준시각 기준)
        if not self.synced:
            messagebox.showwarning(
                "인터넷 연결 필요",
                "현재 시각을 인터넷으로 확인해야 실행할 수 있습니다.\n"
                "연결을 확인한 뒤 [표준시각 동기화] 를 눌러주세요.",
            )
            self._verify_expiry_async()
            return
        self._evaluate_expiry()
        if self._expired:
            messagebox.showwarning("사용 기간 만료", f"사용 기간이 만료되었습니다.\n만료일: {EXPIRY_TEXT} (KST)")
            return
        if not self.windows:
            messagebox.showwarning("확인", "먼저 ① 에서 ＋창 추가로 창을 1개 이상 등록하세요.")
            return
        try:
            interval = parse_seconds(self.interval_var.get())
            wait = parse_seconds(self.wait_var.get())
            if interval <= 0 or wait < 0:
                raise ValueError
        except ValueError:
            messagebox.showwarning(
                "확인",
                "시간을 올바르게 입력하세요.\n"
                "예) 59.85  또는  59:85 (= 59.85초)\n"
                "     0.05   또는  0:05  (= 0.05초)",
            )
            return

        watch = self.watch_var.get()
        if watch:
            if not _PIL_OK:
                messagebox.showwarning(
                    "확인",
                    "보안문자 감시에는 Pillow 가 필요합니다.\n터미널에서:  pip install pillow",
                )
                return
            if self.watch_region is None or self.watch_baseline is None:
                messagebox.showwarning(
                    "확인",
                    "④ 보안문자 감시를 켜려면\n"
                    "[① 감시 영역 지정] 과 [② 기준 화면 저장] 을 먼저 해주세요.",
                )
                return
            try:
                threshold = float(self.watch_sens_var.get())
            except ValueError:
                messagebox.showwarning("확인", "감시 민감도는 숫자로 입력하세요. (기본 8)")
                return

        keys = KEY_CHOICES[self.key_var.get()]
        clicks = 2 if self.click_mode_var.get() == "더블 클릭" else 1
        align = self.align_var.get()
        self._stop.clear()
        self._alert_off.set()
        self._watch_ready.clear()
        self._count = 0
        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        windows = list(self.windows)  # 실행 중 목록 변경 대비 복사
        self.log(
            f"시작 — 창 {len(windows)}개, {interval:.2f}초마다 새로고침 후 "
            f"{self.click_mode_var.get()}"
        )
        self._worker = threading.Thread(
            target=self._loop, args=(interval, wait, keys, clicks, align, windows), daemon=True
        )
        self._worker.start()

        if watch:
            self.log(f"보안문자 감시 켜짐 (민감도 {threshold:g})")
            threading.Thread(
                target=self._watch_loop,
                args=(self.watch_region, self.watch_baseline, threshold),
                daemon=True,
            ).start()

    def _loop(
        self, interval: float, wait: float, keys: list[str], clicks: int, align: bool,
        windows: list[tuple[tuple[int, int], tuple[int, int]]],
    ) -> None:
        """여러 창을 [모두 새로고침] → 대기 → [모두 클릭] 반복 (드리프트 방지 스케줄링).

        각 창은 '선택용 좌표'를 눌러 활성화한 뒤 새로고침하고,
        대기 후 '클릭용 좌표'를 누른다. align=True 면 표준시각 정각에 맞춘다.
        """
        key_label = "+".join(keys)
        gap = 0.05  # 창 사이 최소 간격(초)
        next_run = time.perf_counter()
        next_target = 0.0
        grid = interval
        lead = 0.0
        if align:
            # 새로고침 간격은 '00초 격자'(60초·30초 …)에 맞춰 드리프트를 없애고,
            # 입력한 주기가 격자보다 조금 짧으면 그 차이(lead)만큼 매번 앞당겨 누른다.
            #   59.85초 → 60초 격자, 0.15초 먼저 = 매분 59.85초에 새로고침
            #   30초    → 30초 격자, 앞당김 없음 = 00초·30초에 새로고침
            #   45초    → 60초 격자, 차이가 커서 앞당김 없이 매분 00초에 새로고침
            grid = align_grid(interval)
            lead = grid - interval
            if lead > LEAD_MAX:
                self.log(
                    f"정각 맞춤 — 주기를 {interval:.2f}초 → {grid:.2f}초 로 보정해 00초에 맞춥니다."
                )
                lead = 0.0
            elif lead > 1e-9:
                self.log(
                    f"정각 맞춤 — {grid:.2f}초 간격으로, 00초보다 {lead:.2f}초 먼저"
                    f"(매 {interval:.2f}초 지점) 새로고침합니다."
                )
            else:
                self.log(f"정각 맞춤 — {grid:.2f}초 간격으로 00초에 새로고침합니다.")
            next_target = next_grid_target(self.accurate_time(), grid, lead)
            delay = next_target - self.accurate_time()
            self.log(f"표준시각 {self._fmt_kst(next_target)} 에 첫 새로고침 예정 ({delay:.2f}초 대기)")
            if self._stop.wait(timeout=max(0.0, delay)):
                self.root.after(0, self._reset_buttons)
                return
        try:
            while not self._stop.is_set():
                self._count += 1
                self._watch_ready.clear()  # 동작 중에는 화면이 요동치므로 감시를 잠시 멈춘다

                # 새로고침 라운드: 각 창을 선택 → 새로고침 (빠르게 연속)
                for focus, _target in windows:
                    pyautogui.click(x=focus[0], y=focus[1])   # 빈 곳 눌러 창 활성화
                    time.sleep(gap)
                    if len(keys) == 1:
                        pyautogui.press(keys[0])
                    else:
                        pyautogui.hotkey(*keys)
                    time.sleep(gap)
                self.log(f"[{self._count}] 창 {len(windows)}개 새로고침({key_label})")

                if self._stop.wait(timeout=wait):
                    break

                # 클릭 라운드: 각 창의 버튼 클릭 (빠르게 연속)
                mode = "더블클릭" if clicks == 2 else "클릭"
                for _focus, target in windows:
                    pyautogui.click(x=target[0], y=target[1], clicks=clicks, interval=0.05)
                    time.sleep(gap)
                self.log(f"[{self._count}] 창 {len(windows)}개 {mode} 완료")

                # 화면이 안정될 시간을 준 뒤 감시 재개
                if self._stop.wait(timeout=0.6):
                    break
                self._watch_ready.set()

                if align:
                    # 다음 발사 지점까지 대기 (밀렸으면 그 다음 지점으로 건너뛴다)
                    next_target += grid
                    now = self.accurate_time()
                    if next_target < now:
                        next_target = next_grid_target(now, grid, lead)
                    delay = next_target - now
                    if self._stop.wait(timeout=max(0.0, delay)):
                        break
                else:
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
        """매크로를 정지한다. (울리고 있던 알림음도 끈다)"""
        self._stop.set()
        self._alert_off.set()
        self._watch_ready.clear()
        self.log("정지 요청됨.")

    def on_close(self) -> None:
        self._stop.set()
        self._alert_off.set()
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    MacroApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
