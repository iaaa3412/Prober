"""Middle-mouse-drag panning, for both kinds of map in this GUI."""
from __future__ import annotations


def bind_middle_pan_tk(canvas, on_pan=None):
    def press(event):
        if not canvas.cget("scrollregion"):
            bb = canvas.bbox("all")
            if bb:
                pad = 500
                canvas.configure(scrollregion=(bb[0] - pad, bb[1] - pad,
                                               bb[2] + pad, bb[3] + pad))
        canvas.scan_mark(event.x, event.y)
        return "break"

    def drag(event):
        canvas.scan_dragto(event.x, event.y, gain=1)
        if on_pan:
            on_pan()
        return "break"

    canvas.bind("<ButtonPress-2>", press, add="+")
    canvas.bind("<B2-Motion>", drag, add="+")


def bind_middle_pan_mpl(canvas, get_ax=None, on_pan=None):
    state: dict = {}

    def press(event):
        if event.button != 2 or event.inaxes is None:
            return
        ax = get_ax() if get_ax is not None else event.inaxes
        if ax is None or event.inaxes is not ax:
            return
        state["ax"] = ax
        state["xy"] = (event.x, event.y)
        state["xlim"], state["ylim"] = ax.get_xlim(), ax.get_ylim()

    def motion(event):
        if "xy" not in state:
            return
        ax = state.get("ax")
        if ax is None:
            return
        bbox = ax.get_window_extent()
        if not bbox.width or not bbox.height:
            return
        (px, py), (x0, x1), (y0, y1) = state["xy"], state["xlim"], state["ylim"]
        dx = (px - event.x) * (x1 - x0) / bbox.width
        dy = (py - event.y) * (y1 - y0) / bbox.height
        ax.set_xlim(x0 + dx, x1 + dx)
        ax.set_ylim(y0 + dy, y1 + dy)
        canvas.draw_idle()
        if on_pan:
            on_pan()

    def release(event):
        if event.button == 2:
            state.clear()

    canvas.mpl_connect("button_press_event", press)
    canvas.mpl_connect("motion_notify_event", motion)
    canvas.mpl_connect("button_release_event", release)
