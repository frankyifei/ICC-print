"""System-tray icon backed by pystray (separate thread from the Toga UI).

Menu actions are pushed onto a command queue that the GUI drains on its own
event loop, so we never touch Toga widgets from the pystray thread.
"""
from __future__ import annotations

import queue
import threading

from .i18n import tr

_ICON_SIZE = 64


def _make_image(running: bool):
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (_ICON_SIZE, _ICON_SIZE), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    body = (46, 139, 87, 255) if running else (150, 150, 150, 255)
    d.ellipse((4, 4, _ICON_SIZE - 4, _ICON_SIZE - 4), fill=body, outline=(40, 40, 40, 255), width=3)
    d.rectangle((20, 22, 44, 34), fill=(255, 255, 255, 255))
    d.rectangle((20, 40, 44, 48), fill=(255, 255, 255, 255))
    return img


class TrayIcon:
    def __init__(self, commands: "queue.Queue[str]"):
        self._q = commands
        self._icon = None
        self._thread: threading.Thread | None = None

    def available(self) -> bool:
        try:
            import pystray  # noqa: F401
            return True
        except Exception:
            return False

    def _menu(self):
        import pystray
        return pystray.Menu(
            pystray.MenuItem(tr('Show Settings'), lambda *a: self._q.put("show"), default=True),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(tr('Start Service'), lambda *a: self._q.put("start")),
            pystray.MenuItem(tr('Stop Service'), lambda *a: self._q.put("stop")),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(tr('Open Log'), lambda *a: self._q.put("open-log")),
            pystray.MenuItem(tr('Exit'), lambda *a: self._q.put("quit")),
        )

    def start(self) -> bool:
        try:
            import pystray
        except Exception:
            return False
        self._icon = pystray.Icon("icc_spooler", _make_image(False), tr('ICC Spooler — Stopped'),
                                  menu=self._menu())
        self._thread = threading.Thread(target=self._icon.run, name="icc-spooler-tray",
                                        daemon=True)
        self._thread.start()
        return True

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def set_running(self, running: bool) -> None:
        if self._icon is None:
            return
        try:
            self._icon.icon = _make_image(running)
            self._icon.title = "ICC Spooler — " + (tr('Running') if running else tr('Stopped'))
            self._icon.update_menu()
        except Exception:  # noqa: BLE001
            pass

    def notify(self, message: str) -> None:
        if self._icon is None:
            return
        try:
            self._icon.notify(message, "ICC Spooler")
        except Exception:  # noqa: BLE001
            pass

    def stop(self) -> None:
        if self._icon is not None:
            try:
                self._icon.stop()
            except Exception:  # noqa: BLE001
                pass
        self._icon = None
        self._thread = None
